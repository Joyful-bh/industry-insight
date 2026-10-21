"""Bounded A/B replay: only representative events change; no DB publication."""

import argparse
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from scripts.event_stage_supervisor import client, save
from track_insight.core.fingerprints import fingerprint
from track_insight.infrastructure.database import get_engine
from track_insight.infrastructure.models import Topic
from track_insight.settings import load_poc_config
from track_insight.tracks.preparation import Observation, group_output_model, prepare_observations


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--call-api", action="store_true")
    args = parser.parse_args()
    base = Path("data/track_quality/track_grouped_pilot/run_5")
    manifest = read(base / "inputs.json")
    compact = read(base / "compact_inputs.json")
    with get_engine().connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        with Session(bind=connection) as session:
            ids = [uuid.UUID(t["id"]) for t in manifest["topics"]]
            topics = list(session.scalars(select(Topic).where(Topic.id.in_(ids))))
            observations = prepare_observations(session, topics, prefer_identity_evidence=True)
        connection.rollback()
    new_by_id = {str(tid): o for o in observations for tid in o.topic_ids}
    revised = {}
    changes = []
    for old in compact:
        members = list(
            {new_by_id[tid].ref: new_by_id[tid] for tid in old["original_topic_ids"]}.values()
        )
        facts = {e["event_id"]: e for o in members for e in o.payload["representative_events"]}
        row = {k: v for k, v in old.items() if k != "original_topic_ids"}
        row["representative_events"] = list(facts.values())[:3]
        revised[row["topic_ref"]] = row
        before = [e["event_id"] for e in old["representative_events"]]
        after = [e["event_id"] for e in row["representative_events"]]
        if before != after:
            changes.append(
                dict(
                    topic_ref=row["topic_ref"],
                    label=row["label"],
                    before=old["representative_events"],
                    after=row["representative_events"],
                )
            )
    jobs = []
    # Two affected groups and one manufacturing control group; no new grouping/embedding call.
    for index in (0, 3, 4):
        frozen = read(base / f"calls/generation_{index}.input.json")
        for arm in ("baseline", "revised"):
            payload = frozen["payload"]
            if arm == "revised":
                payload = dict(payload, topics=[revised[o["topic_ref"]] for o in payload["topics"]])
            jobs.append((f"{arm}_{index}", frozen["prompt"], payload))
    save(args.output / "selection_changes.json", changes)
    for name, prompt, payload in jobs:
        path = args.output / "calls" / f"{name}.input.json"
        current_input = dict(prompt=prompt, payload=payload)
        if path.exists() and read(path) != current_input:
            raise RuntimeError("Replay inputs changed; use a new output directory")
        save(path, current_input)
    state = dict(
        status="prepared",
        model=manifest["model"],
        logical_call_limit=6,
        max_calls_including_structural_recovery=12,
        calls={},
        changed_observations=len(changes),
        database_modified=False,
    )
    save(args.output / "status.json", state)
    print(json.dumps(state, ensure_ascii=False), flush=True)
    if not args.call_api:
        return
    config = load_poc_config(Path("config/national.yaml"))
    config.bailian.max_retries = 0
    config.stage4.model = manifest["model"]

    def run(job):
        name, prompt, payload = job
        output_path = args.output / "calls" / f"{name}.json"
        rows = [Observation(o["topic_ref"], [], set(), set(), set(), o) for o in payload["topics"]]
        schema = group_output_model(rows)
        signature = fingerprint(config.stage4.model, prompt, payload, schema.model_json_schema())
        if output_path.exists():
            cached = read(output_path)
            if cached.get("signature") not in (None, signature):
                raise RuntimeError("Cached replay inputs changed; use a new output directory")
            schema.model_validate(cached["output"])
            return name, cached
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
        raw = result.raw_response
        responses = (
            [raw["initial_response"], raw["repair_response"]] if result.recovery_used else [raw]
        )
        usage = {
            key: sum(r.get("usage", {}).get(key, 0) for r in responses)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        saved = dict(
            signature=signature,
            output=result.output.model_dump(mode="json"),
            usage=usage,
            call_count=result.call_count,
        )
        save(output_path, saved)
        return name, saved

    state["status"] = "running"
    save(args.output / "status.json", state)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(run, job): job[0] for job in jobs}
        for future in as_completed(futures):
            name = futures[future]
            try:
                _, saved = future.result()
                state["calls"][name] = dict(status="completed", usage=saved["usage"])
            except Exception as exc:
                state["calls"][name] = dict(status="failed", error=str(exc))
                if hasattr(exc, "diagnostics"):
                    save(args.output / "calls" / f"{name}.failure.json", exc.diagnostics)
            save(args.output / "status.json", state)
            print(name, state["calls"][name]["status"], flush=True)
    state["status"] = (
        "completed"
        if all(c["status"] == "completed" for c in state["calls"].values())
        else "failed"
    )
    save(args.output / "status.json", state)


if __name__ == "__main__":
    main()
