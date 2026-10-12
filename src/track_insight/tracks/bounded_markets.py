"""Run-local, dual-recall comparison packages; recall never authorizes a merge."""

import heapq
import re
from collections import Counter

import numpy as np

from track_insight.tracks.contracts import CandidateTrackOutput
from track_insight.tracks.markets import (
    DirectMarketCatalog,
    MarketCatalog,
    MarketDecisions,
    normalize_markets,
)

CLUSTER_PARTITION_VERSION = "bounded-modularity-v1"


def identity_text(track):
    return f"{track.name}\n{track.offering_family}"


def _lexical_vectors(tracks):
    rows = []
    for track in tracks:
        text = re.sub(r"研发|制造|生产|高端|高性能|智能", " ", identity_text(track).casefold())
        grams = Counter(
            word[i : i + 2]
            for word in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]+", text)
            for i in range(len(word) - 1)
        )
        rows.append(grams)
    frequency = Counter(key for row in rows for key in row)
    vocabulary = {key: i for i, key in enumerate(frequency)}
    vectors = np.zeros((len(tracks), len(vocabulary)), dtype=np.float32)
    for i, row in enumerate(rows):
        for key, count in row.items():
            vectors[i, vocabulary[key]] = (1 + np.log(count)) * np.log(
                (len(tracks) + 1) / (frequency[key] + 1)
            )
    return vectors


def _unit(vectors):
    vectors = np.asarray(vectors, dtype=np.float32)
    return vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-9)


def comparison_packages(
    tracks,
    vectors,
    *,
    top_k=12,
    max_members=24,
    max_chars=18000,
    recall_routes=("lexical", "semantic"),
    lexical_quantile=0.995,
    semantic_quantile=0.995,
    lexical_top_k=None,
    semantic_top_k=None,
    lexical_floor=None,
    semantic_floor=None,
    lexical_margin=0.15,
    semantic_margin=0.10,
    partitioning="star",
):
    if len(tracks) != len(vectors):
        raise ValueError("Embedding count differs from candidate count")
    if max_members < 2 or top_k < 1:
        raise ValueError("Comparison bounds must permit at least one pair")
    if partitioning not in {"star", "cluster"}:
        raise ValueError("Unknown comparison partitioning")
    if not set(recall_routes) <= {"lexical", "semantic"} or not recall_routes:
        raise ValueError("Unknown or empty recall routes")
    if not all(0 <= q <= 1 for q in (lexical_quantile, semantic_quantile)):
        raise ValueError("Recall quantiles must lie between zero and one")
    if any(k is not None and k < 1 for k in (lexical_top_k, semantic_top_k)):
        raise ValueError("Per-route neighbor limits must be positive")
    lexical = _unit(_lexical_vectors(tracks))
    semantic = _unit(vectors)
    if not np.isfinite(semantic).all() or np.any(np.linalg.norm(semantic, axis=1) == 0):
        raise ValueError("Invalid identity embeddings")
    roles = np.array([str(track.supplier_role) for track in tracks])
    adjacency = [dict() for _ in tracks]
    thresholds = {}
    for route, matrix, quantile, override, quota, margin in (
        (
            "lexical",
            lexical @ lexical.T,
            lexical_quantile,
            lexical_floor,
            lexical_top_k or top_k,
            lexical_margin,
        ),
        (
            "semantic",
            semantic @ semantic.T,
            semantic_quantile,
            semantic_floor,
            semantic_top_k or top_k,
            semantic_margin,
        ),
    ):
        if route not in recall_routes:
            continue
        for role in sorted(set(roles)):
            ids = np.flatnonzero(roles == role)
            scores = matrix[np.ix_(ids, ids)]
            background = scores[np.triu_indices(len(ids), 1)]
            # Background is run-local and unlabeled; this is a recall gate, not a boundary.
            floor = float(np.quantile(background, quantile)) if len(background) else 1.0
            if override is not None:
                floor = override
            thresholds[f"{route}:{role}"] = floor
            for i in ids:
                neighbors = sorted(
                    (int(j) for j in ids if j != i and matrix[i, j] >= floor and matrix[i, j] > 0),
                    key=lambda j: (-float(matrix[i, j]), j),
                )[:quota]
                values = [float(matrix[i, j]) for j in neighbors]
                if values:
                    # A globally permissive background gate must not admit far weaker
                    # neighbors of a well-defined identity merely to fill top_k.
                    neighbors = [j for j in neighbors if matrix[i, j] >= values[0] - margin]
                    values = [float(matrix[i, j]) for j in neighbors]
                if len(values) > 2:
                    gaps = np.array(values[:-1]) - values[1:]
                    median = float(np.median(gaps))
                    mad = float(np.median(abs(gaps - median)))
                    cut = int(np.argmax(gaps))
                    if gaps[cut] > max(0.03, median + 3 * mad):
                        neighbors = neighbors[: cut + 1]
                for j in neighbors:
                    score = float(matrix[i, j])
                    strength = (score - floor) / max(1 - floor, 1e-6)
                    old = adjacency[i].get(j, (0, set()))
                    adjacency[i][j] = (max(old[0], strength), old[1] | {route})
    # Either direction is enough. Mutual-neighbor requirements lose parent/child recall.
    for i, neighbors in enumerate(adjacency):
        for j, value in list(neighbors.items()):
            old = adjacency[j].get(i, (0, set()))
            adjacency[j][i] = (max(old[0], value[0]), old[1] | value[1])
    edges = {(min(i, j), max(i, j)) for i, row in enumerate(adjacency) for j in row}
    if partitioning == "cluster":
        groups = _cluster_partition(tracks, adjacency, max_members, max_chars)
        owner = {member: index for index, group in enumerate(groups) for member in group}
        covered = sum(owner[i] == owner[j] for i, j in edges)
        packages = [group for group in groups if len(group) > 1]
        return packages, {
            "partitioning": CLUSTER_PARTITION_VERSION,
            "thresholds": thresholds,
            "recall_edges": len(edges),
            "covered_edges": covered,
            "cross_cluster_edges": len(edges) - covered,
            "recalled_candidates": len({i for edge in edges for i in edge}),
            "package_count": len(packages),
            "candidate_appearances": sum(map(len, packages)),
            "assigned_candidates": len(owner),
            "singleton_candidates": sum(len(group) == 1 for group in groups),
            "singleton_ids": [group[0] for group in groups if len(group) == 1],
            "max_package_members": max(map(len, packages), default=0),
            "max_package_chars": max(
                (sum(_row_chars(tracks[i], i) for i in group) for group in packages), default=0
            ),
            "route_counts": dict(Counter("+".join(sorted(adjacency[i][j][1])) for i, j in edges)),
            "edges": [list(edge) for edge in sorted(edges)],
        }
    uncovered = set(edges)
    packages = []
    # Broad hubs do not grow by transitive reachability; every member is a direct neighbor.
    for center in sorted(range(len(tracks)), key=lambda i: (-len(adjacency[i]), i)):
        neighbors = sorted(adjacency[center], key=lambda j: (-adjacency[center][j][0], j))
        remaining = [j for j in neighbors if (min(center, j), max(center, j)) in uncovered]
        while remaining:
            members, chars = [center], _row_chars(tracks[center], center)
            deferred = []
            for j in remaining:
                size = _row_chars(tracks[j], j)
                if len(members) < max_members and chars + size <= max_chars:
                    members.append(j)
                    chars += size
                else:
                    deferred.append(j)
            if len(members) == 1:
                raise ValueError("A recalled candidate pair exceeds the comparison input bound")
            packages.append(members)
            for i in members:
                for j in members:
                    uncovered.discard((min(i, j), max(i, j)))
            remaining = [j for j in deferred if (min(center, j), max(center, j)) in uncovered]
    return packages, {
        "thresholds": thresholds,
        "recall_edges": len(edges),
        "covered_edges": len(edges - uncovered),
        "recalled_candidates": len({i for edge in edges for i in edge}),
        "package_count": len(packages),
        "candidate_appearances": sum(map(len, packages)),
        "max_package_members": max(map(len, packages), default=0),
        "route_counts": dict(Counter("+".join(sorted(adjacency[i][j][1])) for i, j in edges)),
        "edges": [list(edge) for edge in sorted(edges)],
    }


def _cluster_partition(tracks, adjacency, max_members, max_chars):
    """Bounded greedy modularity communities on the unchanged dual-recall graph."""
    groups = {i: [i] for i in range(len(tracks))}
    chars = {i: _row_chars(track, i) for i, track in enumerate(tracks)}
    neighbors = {
        i: {j: max(value[0], 1e-6) for j, value in row.items()}
        for i, row in enumerate(adjacency)
    }
    degrees = {i: sum(row.values()) for i, row in neighbors.items()}
    totals = Counter()
    roles = {i: str(track.supplier_role) for i, track in enumerate(tracks)}
    for i, degree in degrees.items():
        totals[roles[i]] += degree
    queue = []

    def enqueue(a, b):
        a, b = sorted((a, b))
        if len(groups[a]) + len(groups[b]) > max_members or chars[a] + chars[b] > max_chars:
            return
        # Null expectation accounts for highly connected broad candidates.
        score = neighbors[a][b] - degrees[a] * degrees[b] / totals[roles[a]]
        if score <= 0:
            return
        heapq.heappush(queue, (-score, a, b))

    for i, row in neighbors.items():
        for j in sorted(row):
            if i < j:
                enqueue(i, j)
    next_id = len(tracks)
    while queue:
        _, a, b = heapq.heappop(queue)
        if a not in groups or b not in groups:
            continue
        left, right = neighbors.pop(a), neighbors.pop(b)
        linked = {
            other: left.get(other, 0) + right.get(other, 0)
            for other in left.keys() | right.keys()
            if other not in {a, b}
        }
        merged = next_id
        next_id += 1
        groups[merged] = sorted(groups.pop(a) + groups.pop(b))
        chars[merged] = chars.pop(a) + chars.pop(b)
        degrees[merged] = degrees.pop(a) + degrees.pop(b)
        roles[merged] = roles[a]
        neighbors[merged] = linked
        for other in sorted(linked):
            neighbors[other].pop(a, None)
            neighbors[other].pop(b, None)
            neighbors[other][merged] = linked[other]
            enqueue(merged, other)
    return sorted(groups.values(), key=lambda group: group[0])


def _row(track, i):
    return dict(
        track_ref=f"C{i}",
        name=track.name,
        offering_family=track.offering_family,
        supplier_role=track.supplier_role,
        definition=track.definition,
    )


def _row_chars(track, i):
    import json

    return len(json.dumps(_row(track, i), ensure_ascii=False)) + 2


def merge_packages(tracks, scope, packages, complete, *, max_members=24, max_chars=18000):
    """Propose locally, resolve only bounded conflicts, then audit bounded merge groups."""
    proposals = {}
    blocked_roles = 0
    for members in packages:
        payload = {"scope": scope, "tracks": [_row(tracks[i], i) for i in members]}
        catalog = complete("catalog", payload, MarketCatalog)
        for market in catalog.markets:
            ids = frozenset(int(ref[1:]) for ref in market.member_refs)
            if not ids <= set(members) or len(ids) != len(market.member_refs):
                raise ValueError("Proposal refers outside its comparison package")
            if any(tracks[i].supplier_role != market.supplier_role for i in ids):
                blocked_roles += 1
                continue
            proposals.setdefault(ids, market)
    # Connected components identify conflicting proposals only; they never authorize merging.
    pending = set(proposals)
    accepted = {}
    oversized = []
    conflicts = 0
    while pending:
        first = min(pending, key=lambda group: tuple(sorted(group)))
        component, union = {first}, set(first)
        pending.remove(first)
        while True:
            linked = {group for group in pending if union.intersection(group)}
            if not linked:
                break
            pending -= linked
            component |= linked
            union.update(i for group in linked for i in group)
        if len(component) == 1:
            accepted[first] = proposals[first]
            continue
        conflicts += 1
        ids = sorted(union)
        if len(ids) > max_members or sum(_row_chars(tracks[i], i) for i in ids) > max_chars:
            oversized.append(ids)
            continue
        payload = {"scope": scope, "tracks": [_row(tracks[i], i) for i in ids]}
        resolution = complete("catalog", payload, MarketCatalog)
        claimed = set()
        for market in resolution.markets:
            group = frozenset(int(ref[1:]) for ref in market.member_refs)
            if not group <= union or claimed.intersection(group):
                raise ValueError("Invalid conflict resolution membership")
            claimed.update(group)
            if all(tracks[i].supplier_role == market.supplier_role for i in group):
                accepted[group] = market
    markets = []
    for index, (group, market) in enumerate(
        sorted(accepted.items(), key=lambda item: min(item[0]))
    ):
        markets.append(
            market.model_copy(
                update={"market_ref": f"M{index}", "member_refs": [f"C{i}" for i in sorted(group)]}
            )
        )

    def bounded_complete(task, payload, schema):
        if task == "catalog":
            return MarketCatalog(markets=markets)
        decisions = []
        for group in payload["groups"]:
            # Each proposed group is already bounded by the package/conflict limits.
            result = complete("audit", {"scope": scope, "groups": [group]}, MarketDecisions)
            decisions.extend(result.decisions)
        return MarketDecisions(decisions=decisions)

    result, stats = normalize_markets(tracks, scope, bounded_complete)
    stats.update(
        conflict_components=conflicts,
        oversized_conflicts=oversized,
        recall_proposals=len(proposals),
        blocked_proposal_roles=blocked_roles,
    )
    return result, stats


def merge_packages_direct(tracks, scope, packages, complete, *, max_members=24, max_chars=18000):
    """Direct coarse outputs; shared provenance is not a reason to reject a partition."""
    proposals = {}
    blocked = 0
    for package_id, members in enumerate(packages):
        payload = {"scope": scope, "tracks": [_row(tracks[i], i) for i in members]}
        catalog = complete("direct_catalog", payload, DirectMarketCatalog)
        for market in catalog.markets:
            ids = frozenset(int(ref[1:]) for ref in market.member_refs)
            if not ids <= set(members) or len(ids) != len(market.member_refs):
                raise ValueError("Direct output refers outside its package")
            if any(tracks[i].supplier_role != market.supplier_role for i in ids):
                blocked += 1
                continue
            key = (ids, market.offering_family.replace(" ", "").casefold())
            if key not in proposals:
                proposals[key] = (market, {package_id})
            else:
                proposals[key][1].add(package_id)
    pending = set(proposals)
    resolved, oversized = [], []
    conflicts = 0
    while pending:
        first = min(pending, key=lambda k: (tuple(sorted(k[0])), k[1]))
        component, union = {first}, set(first[0])
        pending.remove(first)
        while True:
            # Sharing within one package is an intentional partition, not a conflict.
            linked = {
                key
                for key in pending
                if any(
                    key[0] & other[0] and proposals[key][1] != proposals[other][1]
                    for other in component
                )
            }
            if not linked:
                break
            pending -= linked
            component |= linked
            union.update(i for key in linked for i in key[0])
        if len(component) == 1 or len({frozenset(proposals[k][1]) for k in component}) == 1:
            resolved.extend(proposals[key][0] for key in sorted(component, key=lambda k: k[1]))
            continue
        conflicts += 1
        ids = sorted(union)
        if len(ids) > max_members or sum(_row_chars(tracks[i], i) for i in ids) > max_chars:
            oversized.append(ids)
            continue
        catalog = complete(
            "direct_catalog",
            {
                "scope": scope,
                "tracks": [_row(tracks[i], i) for i in ids],
                "previous_markets": [
                    proposals[key][0].model_dump(mode="json")
                    for key in sorted(component, key=lambda k: k[1])
                ],
            },
            DirectMarketCatalog,
        )
        for market in catalog.markets:
            group = {int(ref[1:]) for ref in market.member_refs}
            if not group <= union:
                raise ValueError("Invalid direct conflict resolution references")
            if all(tracks[i].supplier_role == market.supplier_role for i in group):
                resolved.append(market)
    canonical = {}
    for market in resolved:
        key = (market.offering_family.replace(" ", "").casefold(), market.supplier_role)
        if key in canonical:
            old = canonical[key]
            canonical[key] = old.model_copy(
                update={"member_refs": list(dict.fromkeys(old.member_refs + market.member_refs))}
            )
        else:
            canonical[key] = market
    resolved = list(canonical.values())
    consumed, result, memberships, reduction = set(), [], Counter(), 0
    for market in resolved:
        ids = sorted(int(ref[1:]) for ref in market.member_refs)
        core = list(dict.fromkeys(tid for i in ids for tid in tracks[i].topic_ids))
        support = list(
            dict.fromkeys(
                tid for i in ids for tid in tracks[i].supporting_topic_ids if tid not in core
            )
        )
        result.append(
            CandidateTrackOutput(
                name=market.name,
                offering_family=market.offering_family,
                supplier_role=market.supplier_role,
                definition=market.boundary,
                topic_ids=core,
                supporting_topic_ids=support,
                identity_event_ids=list(
                    dict.fromkeys(eid for i in ids for eid in tracks[i].identity_event_ids)
                ),
                confidence=min(tracks[i].confidence for i in ids),
                status="watchlist"
                if any(tracks[i].status == "watchlist" for i in ids)
                else "candidate",
            )
        )
        consumed.update(ids)
        memberships.update(ids)
    result.extend(track for i, track in enumerate(tracks) if i not in consumed)
    before = {tid for t in tracks for tid in [*t.topic_ids, *t.supporting_topic_ids]}
    after = {tid for t in result for tid in [*t.topic_ids, *t.supporting_topic_ids]}
    if before != after:
        raise ValueError("Direct merge changed source Topic accounting")
    reduction = len(tracks) - len(result)
    return result, dict(
        input_tracks=len(tracks),
        output_tracks=len(result),
        merged_groups=len(resolved),
        consumed_candidates=len(consumed),
        reduction=reduction,
        shared_source_candidates=sum(v > 1 for v in memberships.values()),
        conflict_components=conflicts,
        oversized_conflicts=oversized,
        blocked_proposal_roles=blocked,
        model_audit_calls=0,
        merge_groups=[market.model_dump(mode="json") for market in resolved],
    )
