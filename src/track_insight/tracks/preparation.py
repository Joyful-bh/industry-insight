"""Lossless evidence mappings and bounded semantic working groups, not final markets."""

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pydantic import model_validator
from sqlalchemy import select

from track_insight.core.errors import ContractError
from track_insight.core.fingerprints import fingerprint
from track_insight.infrastructure.models import Event, EventEvidence, PageAnalysis, TopicEvent
from track_insight.tracks.contracts import IdentifiedTrackBuildOutput

logger = logging.getLogger(__name__)


def normalized(text):
    return re.sub(r"\W+", "", text).casefold()


def event_ref(event_id):
    return "E" + str(event_id).replace("-", "")[:12]


def representative_event_key(event, quote=""):
    """Prefer literal product/activity evidence, not inferred Topic descriptions."""
    factual = "\n".join((event.title, event.summary, quote))
    generic = {"制造业", "先进制造业", "新材料", "新能源", "电子信息", "装备制造"}
    products = [
        obj
        for obj in event.industry_objects
        if len(obj) >= 2
        and obj not in generic
        and obj in factual
        and not any(word in obj for word in ("产业", "园区", "基地", "基金", "平台"))
    ]
    # A product mention alone is insufficient: funding and infrastructure also mention products.
    activities = r"主营|主要生产|生产|制造|研发|试制|产业化|投产|出货|年产|产能|布局|项目"
    linked = sum(
        bool(
            re.search(
                rf"(?:{activities})[^。；\n]{{0,60}}{re.escape(obj)}"
                rf"|{re.escape(obj)}[^。；\n]{{0,60}}(?:{activities})",
                factual,
            )
        )
        for obj in products
    )
    direct = bool(re.search(r"主营|主要生产|生产线|生产车间|出货|年产|量产", factual))
    return (
        int(linked > 0),
        int(direct and linked > 0),
        min(linked, 3),
        event.signal_date is not None,
        event.signal_date.toordinal() if event.signal_date else 0,
    )


def select_representative_events(events, source_by_event, quotes, *, limit=2):
    """Keep the existing input bound; source diversity breaks evidence-quality ties."""
    remaining = sorted(events, key=lambda e: str(e.id))
    selected, seen_sources = [], set()
    while remaining and len(selected) < limit:
        chosen = max(
            remaining,
            key=lambda e: (
                representative_event_key(e, quotes.get(e.id, ""))[:3],
                source_by_event[e.id] not in seen_sources,
                representative_event_key(e, quotes.get(e.id, ""))[3:],
            ),
        )
        selected.append(chosen)
        seen_sources.add(source_by_event[chosen.id])
        remaining.remove(chosen)
    return selected


@dataclass
class Observation:
    ref: str
    topic_ids: list
    event_ids: set
    source_ids: set
    regions: set
    payload: dict

    @property
    def embedding_text(self):
        return self.payload["label"] + "\n" + self.payload["definition"]


def prepare_observations(session, topics, *, prefer_identity_evidence=False):
    """Coalesce only identical normalized names AND definitions; retain other variants."""
    members = defaultdict(dict)
    source_by_event = {}
    for tid, event, source_id in session.execute(
        select(TopicEvent.topic_id, Event, PageAnalysis.page_capture_id)
        .join(Event, Event.id == TopicEvent.event_id)
        .join(PageAnalysis, PageAnalysis.id == Event.page_analysis_id)
        .where(TopicEvent.topic_id.in_([t.id for t in topics]))
    ):
        members[tid][event.id] = event
        source_by_event[event.id] = source_id
    event_ids = {eid for events in members.values() for eid in events}
    quotes = {}
    for eid, text in session.execute(
        select(EventEvidence.event_id, EventEvidence.evidence_text)
        .where(EventEvidence.event_id.in_(event_ids))
        .order_by(EventEvidence.id)
    ):
        quotes.setdefault(eid, text)
    buckets = defaultdict(list)
    for topic in sorted(topics, key=lambda t: (t.label, str(t.id))):
        buckets[(normalized(topic.label), normalized(topic.definition))].append(topic)
    observations = []
    for group in buckets.values():
        first = group[0]
        events = {eid: e for t in group for eid, e in members[t.id].items()}
        sources = {source_by_event[e.id] for e in events.values()}
        regions = {region for e in events.values() for region in e.regions}
        if prefer_identity_evidence:
            representative = select_representative_events(events.values(), source_by_event, quotes)
        else:
            ordered = sorted(
                events.values(),
                key=lambda e: (
                    -int(normalized(first.label).removesuffix("制造") in normalized(e.summary)),
                    e.signal_date is None,
                    -e.signal_date.toordinal() if e.signal_date else 0,
                    str(e.id),
                ),
            )
            representative = []
            seen_sources = set()
            for e in ordered:
                if source_by_event[e.id] not in seen_sources:
                    representative.append(e)
                    seen_sources.add(source_by_event[e.id])
                if len(representative) == 2:
                    break
        ref = f"O{len(observations) + 1}"
        payload = {
            "topic_ref": ref,
            "label": first.label,
            "definition": first.definition[:300],
            "observation_count": len(group),
            "unique_event_count": len(events),
            "source_document_count": len(sources),
            "region_count": len(regions),
            "representative_events": [
                dict(
                    event_id=str(e.id),
                    event_ref=event_ref(e.id),
                    event_type=e.event_type,
                    event_status=e.event_status,
                    signal_date=str(e.signal_date) if e.signal_date else None,
                    title=e.title[:180],
                    summary=e.summary[:500],
                    industry_objects=e.industry_objects[:16],
                    quote=quotes.get(e.id, "")[:500],
                )
                for e in representative
            ],
        }
        observations.append(
            Observation(
                ref,
                [t.id for t in group],
                set(events),
                sources,
                regions,
                payload,
            )
        )
    if sum(len(o.topic_ids) for o in observations) != len(topics):
        raise ContractError("Topic preparation lost input mappings")
    return observations


def semantic_groups(observations, vectors, *, max_topics=40, max_chars=30000):
    """Recursively bisect normalized vectors; cap BOTH member count and JSON characters."""
    if not observations:
        return []
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or len(matrix) != len(observations) or not np.isfinite(matrix).all():
        raise ContractError("Invalid or incomplete grouping embeddings")
    norms = np.linalg.norm(matrix, axis=1)
    if (norms == 0).any():
        raise ContractError("Zero grouping embedding")
    matrix /= norms[:, None]
    sizes = [len(json.dumps(o.payload, ensure_ascii=False)) + 2 for o in observations]
    if max(sizes) + 2 > max_chars:
        raise ContractError("One observation exceeds the configured input bound")
    groups = []

    def split(indices):
        if len(indices) <= max_topics and sum(sizes[i] for i in indices) + 2 <= max_chars:
            groups.append([observations[i] for i in indices])
            return
        data = matrix[indices]
        center = data.mean(axis=0)
        a = int(np.argmin(data @ center))
        b = int(np.argmin(data @ data[a]))
        centers = data[[a, b]].copy()
        mask = np.zeros(len(indices), dtype=bool)
        for _ in range(16):
            new = (data @ centers[1]) > (data @ centers[0])
            if not new.any() or new.all():
                break
            mask = new
            updated = np.stack([data[~mask].mean(axis=0), data[mask].mean(axis=0)])
            updated /= np.maximum(np.linalg.norm(updated, axis=1)[:, None], 1e-12)
            if np.allclose(centers, updated):
                break
            centers = updated
        left = [i for i, right in zip(indices, mask, strict=True) if not right]
        right = [i for i, right in zip(indices, mask, strict=True) if right]
        if not left or not right:
            middle = len(indices) // 2
            left, right = indices[:middle], indices[middle:]
        split(left)
        split(right)

    split(list(range(len(observations))))
    return groups


def coalesce_compatible_names(observations, vectors, *, minimum_similarity=0.93):
    """Compress close same-name variants for display; expose all boundaries to the model."""
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1)
    if (norms == 0).any() or not np.isfinite(matrix).all():
        raise ContractError("Invalid compatibility embeddings")
    matrix = matrix / norms[:, None]
    buckets = defaultdict(list)
    for i, o in enumerate(observations):
        buckets[normalized(o.payload["label"])].append(i)
    clusters = []
    for indices in buckets.values():
        local = []
        for i in indices:
            compatible = next(
                (
                    g
                    for g in local
                    if len(g) < 8
                    and all(float(matrix[i] @ matrix[j]) >= minimum_similarity for j in g)
                ),
                None,
            )
            if compatible is None:
                local.append([i])
            else:
                compatible.append(i)
        clusters.extend(local)
    result, result_vectors = [], []
    for indices in clusters:
        group = [observations[i] for i in indices]
        first = group[0]
        payload = dict(first.payload)
        ref = f"O{len(result) + 1}"
        payload["topic_ref"] = ref
        definitions = list(dict.fromkeys(o.payload["definition"] for o in group))
        if len(definitions) > 1:
            payload["definition_variants"] = definitions
        facts = {e["event_id"]: e for o in group for e in o.payload["representative_events"]}
        payload["representative_events"] = list(facts.values())[:3]
        tids = [tid for o in group for tid in o.topic_ids]
        events = set().union(*(o.event_ids for o in group))
        sources = set().union(*(o.source_ids for o in group))
        regions = set().union(*(o.regions for o in group))
        payload.update(
            observation_count=len(tids),
            unique_event_count=len(events),
            source_document_count=len(sources),
            region_count=len(regions),
        )
        result.append(Observation(ref, tids, events, sources, regions, payload))
        vector = matrix[indices].mean(axis=0)
        result_vectors.append((vector / np.linalg.norm(vector)).tolist())
    return result, result_vectors


def expand_observation_output(output, observations, topic_refs):
    """Program expands compact references back to every original Topic reference."""
    reverse = {topic_id: ref for ref, topic_id in topic_refs.items()}
    if any(tid not in reverse for o in observations for tid in o.topic_ids):
        raise ContractError("Observation contains an unknown original Topic")
    mapping = {o.ref: [reverse[tid] for tid in o.topic_ids] for o in observations}
    actual = [r for t in output.tracks for r in [*t.refs, *t.supporting_refs]]
    actual += output.excluded_refs
    if set(actual) != set(mapping):
        raise ContractError("Grouped generation must account for every observation")
    payload = output.model_dump()
    for track in payload["tracks"]:
        for key in ("refs", "supporting_refs"):
            track[key] = [ref for old in track[key] for ref in mapping[old]]
    payload["excluded_refs"] = [ref for old in payload["excluded_refs"] for ref in mapping[old]]
    return IdentifiedTrackBuildOutput.model_validate(payload)


def group_output_model(observations):
    expected = {o.ref for o in observations}
    facts = {
        o.ref: {
            e["event_ref"] for e in o.payload.get("representative_events", []) if "event_ref" in e
        }
        for o in observations
    }
    known_events = set().union(*facts.values())

    @model_validator(mode="before")
    @classmethod
    def normalize_evidence(cls, value):
        """Drop known misplaced evidence only when valid core evidence remains."""
        if not isinstance(value, dict) or not isinstance(value.get("tracks"), list):
            return value
        tracks = []
        for track in value["tracks"]:
            if not isinstance(track, dict):
                tracks.append(track)
                continue
            refs, events = track.get("refs"), track.get("core_event_refs")
            if not all(isinstance(items, list) for items in (refs, events)) or not all(
                isinstance(ref, str) for ref in [*refs, *events]
            ):
                tracks.append(track)
                continue
            allowed = set().union(*(facts.get(ref, set()) for ref in refs))
            misplaced = set(events) & (known_events - allowed)
            if misplaced and set(events) & allowed:
                track = dict(track)
                track["core_event_refs"] = [ref for ref in events if ref not in misplaced]
                # The original response is retained separately from the validated output.
                logger.warning(
                    "Track %s: removed misplaced core event refs %s; kept %s",
                    track.get("name"),
                    sorted(misplaced),
                    track["core_event_refs"],
                )
            tracks.append(track)
        return {**value, "tracks": tracks}

    @model_validator(mode="after")
    def verify_refs(self):
        actual = [r for t in self.tracks for r in [*t.refs, *t.supporting_refs]]
        actual += self.excluded_refs
        if set(actual) != expected:
            raise ValueError(
                f"Account for all topic_ref; missing={sorted(expected - set(actual))}, "
                f"unknown={sorted(set(actual) - expected)}"
            )
        if any(facts.values()):
            for track in self.tracks:
                allowed = set().union(*(facts[r] for r in track.refs))
                if not track.core_event_refs or not set(track.core_event_refs) <= allowed:
                    raise ValueError(
                        f"Track {track.name!r}: core_event_refs must be nonempty and come "
                        f"from refs' representative_events; "
                        f"invalid={sorted(set(track.core_event_refs) - allowed)}, "
                        f"allowed={sorted(allowed)}. Select evidence supporting the offering "
                        "and supplier role; if none exists, reconsider the Track assignment."
                    )
        return self

    return type(
        "IdentifiedTrackBuildOutput",
        (IdentifiedTrackBuildOutput,),
        {"normalize_evidence": normalize_evidence, "verify_refs": verify_refs},
    )


def cached_embeddings(observations, client, model, *, cache_dir=Path("data/embedding_cache")):
    """Each successful batch is cached independently; retries only request missing texts."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    texts = list(dict.fromkeys(o.embedding_text for o in observations))
    vectors = {}
    pending = []
    for text in texts:
        path = cache_dir / (fingerprint(model, 512, text) + ".json")
        if path.exists():
            vectors[text] = json.loads(path.read_text(encoding="utf-8"))["vector"]
        else:
            pending.append(text)
    usage = {"input_tokens": 0, "calls": 0}
    for start in range(0, len(pending), 10):
        batch = pending[start : start + 10]
        result = client.embed_texts(batch, model=model)
        usage["calls"] += 1
        usage["input_tokens"] += result["usage"].get("total_tokens", 0)
        for text, vector in zip(batch, result["vectors"], strict=True):
            vectors[text] = vector
            path = cache_dir / (fingerprint(model, 512, text) + ".json")
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"vector": vector}), encoding="utf-8")
            temporary.replace(path)
    return [vectors[o.embedding_text] for o in observations], usage
