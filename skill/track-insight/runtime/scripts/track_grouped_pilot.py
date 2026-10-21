"""Bounded production-contract replay on national Topics, without publishing Tracks."""

import argparse
import json
import random
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from scripts.event_stage_supervisor import client, save
from track_insight.core.fingerprints import fingerprint
from track_insight.infrastructure.database import session_scope
from track_insight.infrastructure.models import Event, PageAnalysis, ResearchPlan, Topic, TopicEvent
from track_insight.settings import load_poc_config
from track_insight.tracks.contracts import (
    CandidateTrackOutput,
    TrackDefinitionOutput,
)
from track_insight.tracks.markets import normalize_markets
from track_insight.tracks.preparation import (
    cached_embeddings,
    coalesce_compatible_names,
    expand_observation_output,
    group_output_model,
    prepare_observations,
    semantic_groups,
)
from track_insight.tracks.service import _combine_exact_track_identities, _track_scope_payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-id", type=uuid.UUID, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-topics", type=int, default=240)
    parser.add_argument("--frozen-input", type=Path)
    args = parser.parse_args()
    config = load_poc_config(Path("config/national.yaml"))
    root = args.output
    state = {"status": "preparing", "started_at": datetime.now(UTC).isoformat(), "calls": {}}
    save(root / "status.json", state)
    try:
        with session_scope() as session:
            topics = list(
                session.scalars(
                    select(Topic)
                    .where(
                        Topic.research_plan_id == args.plan_id,
                        Topic.status == "candidate",
                    )
                    .order_by(Topic.id)
                )
            )
            scope = _track_scope_payload(session.get(ResearchPlan, args.plan_id), config)
            all_observations = prepare_observations(session, topics)
            rng = random.Random(20261010)
            buckets = defaultdict(list)
            for topic in topics:
                buckets[tuple(sorted(topic.regions)) or ("unknown",)].append(topic)
            chosen = {}
            keys = sorted(buckets)
            rng.shuffle(keys)
            for bucket in buckets.values():
                rng.shuffle(bucket)
            while len(chosen) < min(args.sample_topics, len(topics)):
                for key in keys:
                    if buckets[key] and len(chosen) < args.sample_topics:
                        t = buckets[key].pop()
                        chosen[t.id] = t
            repeated = Counter(t.label for t in topics).most_common(3)
            for label, _ in repeated:
                for t in [t for t in topics if t.label == label][:20]:
                    chosen[t.id] = t
            selected = sorted(chosen.values(), key=lambda t: str(t.id))
            if args.frozen_input:
                frozen = json.loads(args.frozen_input.read_text(encoding="utf-8"))
                wanted = {uuid.UUID(t["id"]) for t in frozen["topics"]}
                selected = [t for t in topics if t.id in wanted]
                if len(selected) != len(wanted):
                    raise RuntimeError(
                        "Frozen Topic inputs are not all present in current snapshot"
                    )
            event_sources = dict(
                session.execute(
                    select(Event.id, PageAnalysis.page_capture_id)
                    .join(PageAnalysis, PageAnalysis.id == Event.page_analysis_id)
                    .join(TopicEvent, TopicEvent.event_id == Event.id)
                    .where(TopicEvent.topic_id.in_([t.id for t in selected]))
                ).all()
            )
            observations = prepare_observations(session, selected)
            session.expunge_all()
        refs = {f"T{i}": t.id for i, t in enumerate(selected, 1)}
        reverse = {value: key for key, value in refs.items()}
        manifest = {
            "plan_id": str(args.plan_id),
            "model": config.stage4.model,
            "full_topic_count": len(topics),
            "full_compact_count": len(all_observations),
            "input_topic_count": len(selected),
            "observation_count": len(observations),
            "selection": "fixed-seed region-stratified sample plus top-three repeated labels",
            "database_modified": False,
            "frozen_input": str(args.frozen_input) if args.frozen_input else None,
            "topics": [
                dict(id=str(t.id), label=t.label, definition=t.definition) for t in selected
            ],
            "observations": [
                dict(**o.payload, original_topic_ids=[str(i) for i in o.topic_ids])
                for o in observations
            ],
        }
        save(root / "inputs.json", manifest)
        active = client(config)
        try:
            vectors, usage = cached_embeddings(
                observations,
                active,
                config.stage4.grouping_embedding_model,
            )
        finally:
            active.close()
        observations, vectors = coalesce_compatible_names(observations, vectors)
        save(
            root / "compact_inputs.json",
            [
                dict(
                    **o.payload,
                    original_topic_ids=[str(i) for i in o.topic_ids],
                )
                for o in observations
            ],
        )
        groups = semantic_groups(
            observations,
            vectors,
            max_topics=config.stage4.generation_batch_topics,
            max_chars=config.stage4.generation_batch_chars,
        )
        save(root / "groups.json", [[o.ref for o in g] for g in groups])
        prompts = {
            "generation": config.stage4.prompts.track_generation.read_text(encoding="utf-8"),
            "definition": config.stage4.prompts.track_definition.read_text(encoding="utf-8"),
        }
        prompts.update(
            {
                key: getattr(config.stage4.prompts, f"track_market_{key}").read_text(
                    encoding="utf-8"
                )
                for key in ("catalog", "audit")
            }
        )

        def call(task, payload, prompt, schema):
            signature = fingerprint(
                config.stage4.model, prompt, payload, schema.model_json_schema()
            )
            path = root / "calls" / f"{task}.json"
            if path.exists():
                previous = json.loads(path.read_text(encoding="utf-8"))
                if previous.get("signature") == signature:
                    try:
                        return schema.model_validate(previous["output"])
                    except ValueError:
                        pass
            save(root / "calls" / f"{task}.input.json", {"prompt": prompt, "payload": payload})
            active = client(config)
            try:
                result = active.complete_structured(
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    output_model=schema,
                    model=config.stage4.model,
                    max_tokens=config.stage4.generation_max_output_tokens,
                    enable_thinking=False,
                )
            finally:
                active.close()
            save(
                path,
                {
                    "signature": signature,
                    "output": result.output.model_dump(mode="json"),
                    "usage": result.usage,
                    "call_count": result.call_count,
                    "recovery_used": result.recovery_used,
                },
            )
            return result.output

        state.update(status="generating", group_count=len(groups))
        save(root / "status.json", state)

        def generate(index):
            group = groups[index]
            output = call(
                f"generation_{index}",
                {"scope": scope, "topics": [o.payload for o in group]},
                prompts["generation"],
                group_output_model(group),
            )
            return index, expand_observation_output(output, group, refs)

        results = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            for future in as_completed([pool.submit(generate, i) for i in range(len(groups))]):
                index, output = future.result()
                results[index] = output
                state["calls"][f"generation_{index}"] = "completed"
                state["updated_at"] = datetime.now(UTC).isoformat()
                save(root / "status.json", state)
                print(f"generation {index}: completed", flush=True)
        raw = [t for i in sorted(results) for t in results[i].tracks]
        candidates = _combine_exact_track_identities(raw)
        excluded = [r for i in sorted(results) for r in results[i].excluded_refs]
        topic_payloads = {tid: o.payload for o in observations for tid in o.topic_ids}
        facts = {
            e["event_ref"]: e for o in observations for e in o.payload["representative_events"]
        }
        tracks = []
        state["status"] = "defining"
        save(root / "status.json", state)
        for start in range(0, len(candidates), 8):
            batch = candidates[start : start + 8]
            output = call(
                f"definition_{start}",
                {
                    "scope": scope,
                    "tracks": [
                        dict(
                            track_ref=f"R{i}",
                            name=t.name,
                            topics=list(
                                {
                                    refs[r]: dict(
                                        label=topic_payloads[refs[r]]["label"],
                                        representative_events=[
                                            e
                                            for e in topic_payloads[refs[r]][
                                                "representative_events"
                                            ]
                                            if e["event_ref"] in t.core_event_refs
                                        ],
                                    )
                                    for r in t.refs
                                }.values()
                            ),
                        )
                        for i, t in enumerate(batch)
                    ],
                },
                prompts["definition"],
                TrackDefinitionOutput,
            )
            definitions = {d.track_ref: d.definition for d in output.definitions}
            if len(output.definitions) != len(batch) or set(definitions) != {
                f"R{i}" for i in range(len(batch))
            }:
                raise RuntimeError("Incomplete definitions")
            for i, t in enumerate(batch):
                tracks.append(
                    CandidateTrackOutput(
                        name=t.name,
                        offering_family=t.offering_family,
                        supplier_role=t.supplier_role,
                        definition=definitions[f"R{i}"],
                        topic_ids=[refs[r] for r in t.refs],
                        supporting_topic_ids=[refs[r] for r in t.supporting_refs],
                        identity_event_ids=[
                            uuid.UUID(facts[r]["event_id"]) for r in t.core_event_refs
                        ],
                        status="candidate",
                        confidence=0.7,
                    )
                )

        def market_call(task, payload, schema):
            return call(f"market_{task}", payload, prompts[task], schema)

        state["status"] = "normalizing_markets"
        save(root / "status.json", state)
        with ThreadPoolExecutor(max_workers=4) as pool:
            final, market_stats = normalize_markets(
                tracks, scope, market_call, lambda fn, items: list(pool.map(fn, items))
            )

        core_memberships = Counter(tid for track in final for tid in track.topic_ids)

        def serialize(track):
            tids = set(track.topic_ids + track.supporting_topic_ids)
            included = [o for o in observations if tids.intersection(o.topic_ids)]
            shared = {tid for tid in track.topic_ids if core_memberships[tid] > 1}
            events = set().union(*(o.event_ids for o in included))
            if shared:
                shared_events = set().union(
                    *(o.event_ids for o in included if shared.intersection(o.topic_ids))
                )
                other_events = set().union(
                    *(o.event_ids for o in included if (tids - shared).intersection(o.topic_ids))
                )
                events = (events - shared_events) | other_events | set(track.identity_event_ids)
            return dict(
                name=track.name,
                offering_family=track.offering_family,
                supplier_role=track.supplier_role,
                definition=track.definition,
                refs=[reverse[x] for x in track.topic_ids],
                supporting_refs=[reverse[x] for x in track.supporting_topic_ids],
                observation_count=len(tids),
                unique_event_count=len(events),
                source_document_count=len({event_sources[eid] for eid in events}),
                identity_event_ids=[str(eid) for eid in track.identity_event_ids],
            )

        save(root / "local_tracks.json", [serialize(t) for t in tracks])
        save(root / "tracks.json", [serialize(t) for t in final])
        assigned = {x for t in final for x in t.topic_ids + t.supporting_topic_ids}
        missing = set(refs.values()) - assigned
        save(
            root / "unassigned_topics.json",
            [
                dict(ref=reverse[t.id], label=t.label, definition=t.definition)
                for t in selected
                if t.id in missing
            ],
        )
        responses = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in (root / "calls").glob("*.json")
            if not p.name.endswith(".input.json")
        ]
        metrics = dict(
            **market_stats,
            input_topics=len(selected),
            compact_observations=len(observations),
            group_count=len(groups),
            max_group_topics=max(map(len, groups)),
            max_group_chars=max(
                len(json.dumps([o.payload for o in g], ensure_ascii=False)) for g in groups
            ),
            local_tracks=len(tracks),
            final_tracks=len(final),
            assigned_topics=len(assigned),
            unassigned_topics=len(missing),
            generation_excluded_topics=len(excluded),
            embedding_usage=usage,
            api_calls=sum(x["call_count"] for x in responses),
            repair_calls=sum(x["recovery_used"] for x in responses),
            input_tokens=sum(x["usage"].get("prompt_tokens", 0) for x in responses),
            output_tokens=sum(x["usage"].get("completion_tokens", 0) for x in responses),
        )
        save(root / "metrics.json", metrics)
        state.update(status="completed", metrics=metrics)
        save(root / "status.json", state)
        print(json.dumps(metrics, ensure_ascii=False), flush=True)
    except Exception as error:
        state.update(status="failed", error=f"{type(error).__name__}: {error}")
        save(root / "status.json", state)
        raise


if __name__ == "__main__":
    main()
