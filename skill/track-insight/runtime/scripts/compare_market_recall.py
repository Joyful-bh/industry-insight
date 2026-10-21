"""Unpaid parameter sensitivity analysis on frozen candidates and cached embeddings."""

import json
import random
from collections import Counter
from itertools import combinations
from pathlib import Path

from scripts.event_stage_supervisor import save
from track_insight.core.fingerprints import fingerprint
from track_insight.tracks.bounded_markets import comparison_packages, identity_text
from track_insight.tracks.contracts import CandidateTrackOutput


def main():
    source = Path("data/track_quality/track_full/run_5/defined_tracks.json")
    root = Path("data/track_quality/bounded_market_pilot/run_1")
    output = Path("data/track_quality/bounded_market_recall_comparison/run_1")
    tracks = [
        CandidateTrackOutput.model_validate(row)
        for row in json.loads(source.read_text(encoding="utf-8"))["tracks"]
    ]
    texts = [identity_text(track) for track in tracks]
    vectors = []
    for i in range(0, len(texts), 10):
        key = fingerprint(dict(model="text-embedding-v4", texts=texts[i : i + 10]))
        data = json.loads((root / "cache" / f"embedding_{key}.json").read_text(encoding="utf-8"))
        vectors.extend(data["vectors"])
    # These are diagnostic comparisons, not trained labels or exhaustive truth.
    compare_pairs = [
        (383, 628),
        (325, 399),
        (676, 733),
        (676, 1094),
        (848, 872),
        (1164, 1310),
        (1046, 1053),
        (1313, 1322),
        (1312, 1323),
        (337, 351),
        (274, 284),
        (275, 277),
        (431, 1390),
        (315, 775),
        (821, 1134),
        (824, 1126),
        (92, 111),
        (39, 1253),
        (1161, 1236),
        (651, 1048),
    ]
    distinct_pairs = [
        (24, 22),
        (24, 1313),
        (274, 280),
        (383, 631),
        (119, 121),
        (821, 813),
        (821, 1129),
        (17, 39),
        (676, 1091),
        (871, 1178),
        (496, 493),
        (25, 79),
    ]
    trials = [
        ("baseline", {}),
        ("lexical_only", dict(recall_routes=("lexical",))),
        ("semantic_only", dict(recall_routes=("semantic",))),
        ("loose_q990", dict(lexical_quantile=0.99, semantic_quantile=0.99)),
        ("strict_q999", dict(lexical_quantile=0.999, semantic_quantile=0.999)),
        ("top4", dict(top_k=4)),
        ("top8", dict(top_k=8)),
        ("top24", dict(top_k=24)),
        ("lex8_sem4", dict(lexical_top_k=8, semantic_top_k=4)),
        ("lex4_sem8", dict(lexical_top_k=4, semantic_top_k=8)),
        ("tight_margin", dict(lexical_margin=0.08, semantic_margin=0.05)),
        ("loose_margin", dict(lexical_margin=0.25, semantic_margin=0.15)),
        ("fixed_045_075", dict(lexical_floor=0.45, semantic_floor=0.75)),
        ("fixed_060_085", dict(lexical_floor=0.60, semantic_floor=0.85)),
    ]
    prior_accepted = []
    for path in (root / "cache").glob("audit_*.json"):
        audit = json.loads(path.read_text(encoding="utf-8"))
        if audit["decisions"][0]["decision"] == "ACCEPT":
            key = path.stem.removeprefix("audit_")
            payload = json.loads((root / "cache" / f"input_{key}.json").read_text(encoding="utf-8"))
            market = payload["groups"][0]["proposed_market"]
            prior_accepted.append((market["name"], {int(ref[1:]) for ref in market["member_refs"]}))
    results = []
    baseline_edges = None
    baseline_copairs = None
    for name, params in trials:
        packages, stats = comparison_packages(tracks, vectors, **params)
        edges = {tuple(edge) for edge in stats["edges"]}
        copairs = {tuple(sorted(pair)) for pack in packages for pair in combinations(pack, 2)}
        if baseline_edges is None:
            baseline_edges, baseline_copairs = edges, copairs
        row = dict(
            name=name,
            parameters=params,
            **{k: v for k, v in stats.items() if k != "edges"},
            package_sizes=dict(Counter(map(len, packages))),
            co_package_pairs=len(copairs),
            diagnostic_compare_direct=sum(tuple(sorted(p)) in edges for p in compare_pairs),
            diagnostic_compare_copack=sum(tuple(sorted(p)) in copairs for p in compare_pairs),
            diagnostic_distinct_direct=sum(tuple(sorted(p)) in edges for p in distinct_pairs),
            diagnostic_distinct_copack=sum(tuple(sorted(p)) in copairs for p in distinct_pairs),
            missed_diagnostic_compare=[p for p in compare_pairs if tuple(sorted(p)) not in copairs],
            edges_removed_vs_baseline=len(baseline_edges - edges),
            edges_added_vs_baseline=len(edges - baseline_edges),
            prior_accepted_groups_in_one_package={
                title: any(ids <= set(pack) for pack in packages) for title, ids in prior_accepted
            },
        )
        results.append(row)
        # Fixed random comparisons of changes, for manual semantic review, not precision claims.
        removed = sorted(baseline_copairs - copairs)
        added = sorted(copairs - baseline_copairs)
        sample = {}
        for label, pairs in (("removed", removed), ("added", added)):
            selected = random.Random(20261011).sample(pairs, min(12, len(pairs)))
            sample[label] = [
                dict(ids=list(p), tracks=[tracks[i].model_dump(mode="json") for i in p])
                for p in selected
            ]
        save(
            output / f"{name}.json",
            dict(
                metrics=row,
                packages=packages,
                edges=sorted(edges),
                changed_comparison_sample=sample,
            ),
        )
        print(
            name,
            stats["recall_edges"],
            stats["package_count"],
            stats["candidate_appearances"],
            row["diagnostic_compare_copack"],
            row["diagnostic_distinct_copack"],
            flush=True,
        )
    save(
        output / "summary.json",
        dict(
            input_candidates=len(tracks),
            paid_calls=0,
            source_file=str(source),
            diagnostic_compare_pairs=compare_pairs,
            diagnostic_distinct_pairs=distinct_pairs,
            diagnostic_labels_are_not_ground_truth=True,
            interpretation="Compare whether pairs get an opportunity for judgment; do not equate "
            "recall with merging. These small diagnostic sets are not precision/recall estimates.",
            results=results,
        ),
    )


if __name__ == "__main__":
    main()
