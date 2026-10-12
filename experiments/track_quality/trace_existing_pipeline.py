"""Read existing replay artifacts and source DB in a read-only transaction. No APIs."""

import argparse
import hashlib
import json
import random
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from track_insight.infrastructure.database import get_engine
from track_insight.infrastructure.models import (
    Event,
    EventEvidence,
    PageAnalysis,
    PageCapture,
    TopicEvent,
)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def trace_run(root):
    manifest = read(root / "inputs.json")
    compact = read(root / "compact_inputs.json")
    local = read(root / "local_tracks.json")
    final = read(root / "tracks.json")
    generations = []
    for path in sorted((root / "calls").glob("generation_*.input.json")):
        result = read(path.with_name(path.name.replace(".input.json", ".json")))
        generations.append((path.name, read(path)["payload"]["topics"], result["output"]))
    traces = []
    for i, topic in enumerate(manifest["topics"], 1):
        ref = f"T{i}"
        observations = [o for o in compact if topic["id"] in o["original_topic_ids"]]
        orefs = {o["topic_ref"] for o in observations}
        calls = []
        for name, payload, output in generations:
            seen = [o for o in payload if o["topic_ref"] in orefs]
            if seen:
                calls.append(
                    dict(
                        input_file=f"calls/{name}",
                        input_observations=seen,
                        candidates=[
                            t
                            for t in output["tracks"]
                            if orefs.intersection(t["refs"] + t.get("supporting_refs", []))
                        ],
                        excluded_observations=sorted(orefs.intersection(output["excluded_refs"])),
                    )
                )
        traces.append(
            dict(
                topic_ref=ref,
                topic=topic,
                compact_observations=observations,
                generation_calls=calls,
                local_tracks=[t for t in local if ref in t["refs"] + t.get("supporting_refs", [])],
                final_tracks=[t for t in final if ref in t["refs"] + t.get("supporting_refs", [])],
            )
        )
    catalog = read(root / "calls/market_catalog.json")["output"]["markets"]
    catalog_input = read(root / "calls/market_catalog.input.json")["payload"]["tracks"]
    assignments = {}
    for path in sorted((root / "calls").glob("market_assignment_*.json")):
        if path.name.endswith(".input.json"):
            continue
        for item in read(path)["output"]["assignments"]:
            assignments.setdefault(item["destination"], []).append(item["track_ref"])
    for market in catalog:
        if "member_refs" not in market:
            market["member_refs"] = assignments.get(market["market_ref"], [])
    audits = {
        d["group_ref"]: d["decision"]
        for d in read(root / "calls/market_audit.json")["output"]["decisions"]
    }
    stages = {}
    for name, rows in (("local", local), ("final", final)):
        stages[name] = sorted({r for t in rows for r in t["refs"] + t.get("supporting_refs", [])})
    input_seen = Counter(o["topic_ref"] for _, rows, _ in generations for o in rows)
    before_events = {
        e["event_id"] for o in manifest["observations"] for e in o["representative_events"]
    }
    after_events = {e["event_id"] for o in compact for e in o["representative_events"]}
    return dict(
        root=str(root),
        traces=traces,
        structural_checks=dict(
            topic_count=len(traces),
            compact_count=len(compact),
            generation_calls=len(generations),
            missing_compact_topics=[
                t["topic_ref"] for t in traces if not t["compact_observations"]
            ],
            missing_generation_topics=[t["topic_ref"] for t in traces if not t["generation_calls"]],
            observations_not_seen_once={
                o["topic_ref"]: input_seen[o["topic_ref"]]
                for o in compact
                if input_seen[o["topic_ref"]] != 1
            },
            representative_events_before_coalescing=len(before_events),
            representative_events_after_coalescing=len(after_events),
            events_not_displayed_after_coalescing=sorted(before_events - after_events),
            local_tracks=len(local),
            final_tracks=len(final),
            assigned_local=len(stages["local"]),
            assigned_final=len(stages["final"]),
            lost_in_final=sorted(set(stages["local"]) - set(stages["final"])),
            added_in_final=sorted(set(stages["final"]) - set(stages["local"])),
        ),
        market_proposals=[
            dict(
                proposal=m,
                audit=audits.get(m["market_ref"]),
                input_candidates=[c for c in catalog_input if c["track_ref"] in m["member_refs"]],
            )
            for m in catalog
        ],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = Path("data/track_quality/track_grouped_pilot")
    runs = {name: trace_run(base / name) for name in ("run_3", "run_5")}
    latest = runs["run_5"]["traces"]
    known = {"T69", "T99", "T115", "T279"}
    wanted = (
        "传感器",
        "铝",
        "高空作业",
        "工程机械",
        "工业软件",
        "固态电池",
        "储能",
        "机器人",
        "中药",
        "磷酸铁锂",
        "电力物联网",
    )
    targeted = [
        t
        for t in latest
        if t["topic_ref"] in known or any(s in t["topic"]["label"] for s in wanted)
    ]
    rng = random.Random(20261010)
    covered = [t for t in latest if t["final_tracks"]]
    excluded = [t for t in latest if not t["final_tracks"]]
    sampled_covered = rng.sample(covered, 8)
    sampled_excluded = rng.sample(excluded, 8)
    selected = {t["topic"]["id"]: t for t in targeted + sampled_covered + sampled_excluded}
    sources = {}
    # Explicit PostgreSQL read-only mode; no session_scope commit or provider client.
    with get_engine().connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        with Session(bind=connection) as session:
            ids = [uuid.UUID(i) for i in selected]
            rows = session.execute(
                select(TopicEvent.topic_id, Event, PageCapture)
                .join(Event, Event.id == TopicEvent.event_id)
                .join(PageAnalysis, PageAnalysis.id == Event.page_analysis_id)
                .join(PageCapture, PageCapture.id == PageAnalysis.page_capture_id)
                .where(TopicEvent.topic_id.in_(ids))
                .order_by(TopicEvent.topic_id, Event.id)
            ).all()
            eids = {event.id for _, event, _ in rows}
            evidence = {}
            for row in session.scalars(
                select(EventEvidence).where(EventEvidence.event_id.in_(eids))
            ):
                evidence.setdefault(str(row.event_id), []).append(
                    dict(
                        text=row.evidence_text,
                        url=row.source_url,
                        start_offset=row.start_offset,
                        end_offset=row.end_offset,
                    )
                )
            terms = (
                "驱动",
                "惠科",
                "模具",
                "糖厂",
                "新能源装备",
                "智能工厂",
                "传感器",
                "铝",
                "固态",
                "储能",
                "机器人",
            )
            for tid, event, page in rows:
                body = page.content_text or ""
                windows = []
                for term in terms:
                    start = 0
                    for _ in range(3):
                        hit = body.find(term, start)
                        if hit < 0:
                            break
                        windows.append(
                            dict(term=term, offset=hit, text=body[max(0, hit - 140) : hit + 320])
                        )
                        start = hit + len(term)
                sources.setdefault(str(tid), []).append(
                    dict(
                        event_id=str(event.id),
                        title=event.title,
                        summary=event.summary,
                        industry_objects=event.industry_objects,
                        entities=event.entities,
                        evidence=evidence.get(str(event.id), []),
                        page_id=str(page.id),
                        url=page.final_url or page.requested_url,
                        page_title=page.title,
                        source_contexts=windows,
                        page_chars=len(body),
                    )
                )
        connection.rollback()
    result = dict(
        generated_at=datetime.now(UTC).isoformat(),
        api_calls=0,
        database_read_only=True,
        seed=20261010,
        artifact_hashes={
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for name in runs
            for path in sorted((base / name).rglob("*.json"))
        },
        comparison_same_topic_ids=(
            [t["topic"]["id"] for t in runs["run_3"]["traces"]]
            == [t["topic"]["id"] for t in latest]
        ),
        review_selection=dict(
            targeted_refs=[t["topic_ref"] for t in targeted],
            sampled_covered_refs=[t["topic_ref"] for t in sampled_covered],
            sampled_excluded_refs=[t["topic_ref"] for t in sampled_excluded],
            source_review_topics=len(selected),
            note="针对性案例与固定种子抽样并用；不据此估算全国准确率。",
        ),
        runs=runs,
        source_events=sources,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            dict(
                output=str(args.output),
                structural_checks=runs["run_5"]["structural_checks"],
                review_selection=result["review_selection"],
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
