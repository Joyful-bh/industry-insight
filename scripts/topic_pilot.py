"""Bounded replay of the production Topic generation contract without snapshot activation."""

import argparse
import hashlib
import json
import random
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from scripts.event_stage_supervisor import client, save
from track_insight.infrastructure.database import session_scope
from track_insight.infrastructure.models import Event, PageAnalysis, PageCapture
from track_insight.settings import load_poc_config
from track_insight.topics.contracts import CompactTopicGenerationOutput, expand_topic_output
from track_insight.topics.service import (
    _event_payload,
    _events_by_work_package,
    _sanitize_generation,
    _work_package_payload,
)


def prepare(config, plan_id):
    with session_scope() as session:
        events = list(
            session.scalars(
                select(Event)
                .join(PageAnalysis, PageAnalysis.id == Event.page_analysis_id)
                .where(
                    PageAnalysis.status == "completed",
                    Event.confidence >= config.stage3.minimum_event_confidence,
                )
                .order_by(Event.id)
            )
        )
        packages = [
            (w, rows) for w, rows in _events_by_work_package(session, plan_id, events) if rows
        ]
        indexes = sorted({round(i * (len(packages) - 1) / 11) for i in range(12)})
        rng = random.Random(20261010)
        batches = []
        for index in indexes:
            work_package, rows = packages[index]
            rows.sort(
                key=lambda e: (
                    (e.topic_hint or "").casefold(),
                    ((e.industry_objects or e.industries or [""])[0]).casefold(),
                    e.signal_date is None,
                    str(e.signal_date),
                    str(e.id),
                )
            )
            groups = [
                rows[n : n + config.stage3.event_batch_size]
                for n in range(0, len(rows), config.stage3.event_batch_size)
            ]
            full_groups = [g for g in groups if len(g) == config.stage3.event_batch_size]
            chosen = rng.choice(full_groups or groups)
            captures = {
                e.id: session.scalar(
                    select(PageCapture)
                    .join(PageAnalysis, PageAnalysis.page_capture_id == PageCapture.id)
                    .where(PageAnalysis.id == e.page_analysis_id)
                )
                for e in chosen
            }
            batches.append(
                {
                    "batch": len(batches),
                    "work_package": _work_package_payload(work_package),
                    "events": [
                        _event_payload(e, event_ref=f"E{i}") for i, e in enumerate(chosen, 1)
                    ],
                    "event_ids": [str(e.id) for e in chosen],
                    "sources": [
                        {"event_ref": f"E{i}", "url": captures[e.id].requested_url}
                        for i, e in enumerate(chosen, 1)
                    ],
                }
            )
        return batches


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-id", type=uuid.UUID, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = load_poc_config(Path("config/national.yaml"))
    prompt = config.stage3.prompts.topic_generation.read_text(encoding="utf-8")
    directory = args.output
    manifest_path = directory / "inputs.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["plan_id"] != str(args.plan_id):
            raise RuntimeError("Pilot directory belongs to another plan")
    else:
        manifest = {"plan_id": str(args.plan_id), "batches": prepare(config, args.plan_id)}
        save(manifest_path, manifest)
    batches = manifest["batches"]
    signature = hashlib.sha256((prompt + config.stage3.model).encode()).hexdigest()
    state = {
        "status": "running",
        "model": config.stage3.model,
        "started_at": datetime.now(UTC).isoformat(),
        "batches": len(batches),
        "input_events": sum(len(b["events"]) for b in batches),
        "calls": {},
    }
    save(directory / "status.json", state)

    def work(batch):
        path = directory / f"batch_{batch['batch']:02d}.json"
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous.get("signature") == signature and previous["status"] == "completed":
                return previous
        active_client = client(config)
        try:
            result = active_client.complete_structured(
                messages=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"work_package": batch["work_package"], "events": batch["events"]},
                            ensure_ascii=False,
                        ),
                    },
                ],
                output_model=CompactTopicGenerationOutput,
                model=config.stage3.model,
                max_tokens=config.stage3.generation_max_output_tokens,
                enable_thinking=False,
            )
            refs = {f"E{i}": uuid.UUID(eid) for i, eid in enumerate(batch["event_ids"], 1)}
            expanded = expand_topic_output(result.output, refs)
            ignored = _sanitize_generation(expanded, set(refs.values()))
            payload = {
                "batch": batch["batch"],
                "status": "completed",
                "signature": signature,
                "usage": result.usage,
                "request_id": result.request_id,
                "output": expanded.model_dump(mode="json"),
                "ignored_memberships": ignored,
            }
        except Exception as error:
            payload = {
                "batch": batch["batch"],
                "status": "failed",
                "signature": signature,
                "error": f"{type(error).__name__}: {error}",
            }
        finally:
            active_client.close()
        save(path, payload)
        return payload

    results = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        for future in as_completed([executor.submit(work, batch) for batch in batches]):
            result = future.result()
            results.append(result)
            state["calls"][str(result["batch"])] = result["status"]
            state["updated_at"] = datetime.now(UTC).isoformat()
            save(directory / "status.json", state)
            print(f"batch {result['batch']}: {result['status']}", flush=True)
    save(directory / "results.json", sorted(results, key=lambda r: r["batch"]))
    state["status"] = (
        "completed" if all(r["status"] == "completed" for r in results) else "partial_failure"
    )
    save(directory / "status.json", state)


if __name__ == "__main__":
    main()
