"""Replay frozen Topics; never modify historical artifacts or the production database."""

import copy
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.settings import get_settings, load_poc_config
from track_insight.tracks.contracts import IdentifiedTrackBuildOutput, TrackDefinitionOutput
from track_insight.tracks.service import (
    _combine_exact_track_identities,
    _validate_generation_reference_coverage,
)
from track_insight.tracks.support import SupportingRecoveryOutput, apply_support_recovery

ROOT = Path("data/track_quality/topic_recovery_regression/run_2")
BASE = Path("data/track_quality/identified_track_regression/v6_batched_complete_accounting")
MODEL = "deepseek-v4-flash-0731"
LOCK = threading.Lock()
STATE = {"status": "running", "pid": os.getpid(), "started_at": datetime.now(UTC).isoformat(),
         "model": MODEL, "max_concurrency": 4, "calls": {}}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(20):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.1)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def call(name, payload, prompt, schema):
    response_path = ROOT / "calls/responses" / f"{name}.json"
    previous = Path("data/track_quality/topic_recovery_regression/run_1/calls/responses") / f"{name}.json"
    if not name.startswith("recovery_") and previous.exists() and not response_path.exists():
        cached = read(previous)
        cached["reused_from"] = str(previous)
        write(response_path, cached)
    if response_path.exists():
        return schema.model_validate(read(response_path)["output"])
    messages = [{"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    with LOCK:
        if len(STATE["calls"]) >= 32:
            raise RuntimeError("bounded experiment exhausted its 32 logical-call limit")
        STATE["calls"][name] = {"status": "running"}
        write(ROOT / "status.json", STATE)
        write(ROOT / "calls/inputs" / f"{name}.json", {"model": MODEL, "messages": messages})
    settings = get_settings()
    config = load_poc_config()
    client = BailianClient(api_key=settings.bailian_api_key.get_secret_value(),
                          base_url=config.bailian.base_url, timeout_seconds=180, max_retries=1)
    try:
        result = client.complete_structured(messages=messages, output_model=schema,
                                            model=MODEL, max_tokens=8192, enable_thinking=False)
        write(response_path, {"output": result.output.model_dump(mode="json"),
                              "usage": result.usage, "raw_response": result.raw_response})
        with LOCK:
            STATE["calls"][name] = {"status": "completed", "usage": result.usage}
            write(ROOT / "status.json", STATE)
        return result.output
    except Exception as error:
        with LOCK:
            STATE["calls"][name] = {"status": "failed", "error": str(error)}
            write(ROOT / "status.json", STATE)
        raise
    finally:
        client.close()


def metrics(output):
    core = {ref for track in output.tracks for ref in track.refs}
    support = {ref for track in output.tracks for ref in track.supporting_refs}
    return {"track_count": len(output.tracks), "core_topic_count": len(core),
            "supporting_topic_count": len(support), "assigned_topic_count": len(core | support),
            "unassigned_topic_count": len(output.excluded_refs)}


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    write(ROOT / "status.json", STATE)
    requests = [read(BASE / "calls/inputs" / f"generation_{i}.json") for i in range(9)]
    batches = [json.loads(request["messages"][-1]["content"]) for request in requests]
    topics = [topic for batch in batches for topic in batch["topics"]]
    scope = batches[0]["scope"]
    all_refs = {topic["topic_ref"] for topic in topics}
    assert len(topics) == len(all_refs) == 259
    generation = Path("config/prompts/track_generation_v1.md").read_text(encoding="utf-8")
    recovery = Path("config/prompts/track_support_recovery_v1.md").read_text(encoding="utf-8")
    definition = Path("config/prompts/track_definition_v1.md").read_text(encoding="utf-8")
    write(ROOT / "manifest.json", {
        "input_topic_count": 259, "batch_size": 32, "support_batch_size": 24,
        "model": MODEL, "baseline": str(BASE), "maximum_logical_calls": 32,
        "manual_track_registry_used": False, "database_modified": False,
        "consolidation": "exact_identity_only; no dynamic market layer",
        "input_sha256": hashlib.sha256(json.dumps(topics, ensure_ascii=False).encode()).hexdigest(),
        "generation_prompt": generation, "recovery_prompt": recovery,
        "original_generation_prompt": requests[0]["messages"][0]["content"],
    })

    def generate(item):
        index, payload = item
        output = call(f"generation_{index}", payload, generation, IdentifiedTrackBuildOutput)
        _validate_generation_reference_coverage(output, {t["topic_ref"] for t in payload["topics"]})
        return output

    with ThreadPoolExecutor(max_workers=4) as pool:
        generated = list(pool.map(generate, enumerate(batches)))
    output = IdentifiedTrackBuildOutput(
        tracks=_combine_exact_track_identities([t for batch in generated for t in batch.tracks]),
        excluded_refs=[ref for batch in generated for ref in batch.excluded_refs],
    )
    _validate_generation_reference_coverage(output, all_refs)
    write(ROOT / "before_recovery.json", output.model_dump(mode="json"))
    before = metrics(output)
    pending = [topic for topic in topics if topic["topic_ref"] in output.excluded_refs]
    candidate_payload = [dict(track_ref=f"R{i}", name=track.name,
                              offering_family=track.offering_family, supplier_role=track.supplier_role)
                         for i, track in enumerate(output.tracks)]
    recovery_batches = [pending[i:i+24] for i in range(0, len(pending), 24)]

    def recover(item):
        index, batch = item
        return call(f"recovery_{index}", {"scope": scope, "tracks": candidate_payload,
                                         "topics": batch}, recovery, SupportingRecoveryOutput)

    with ThreadPoolExecutor(max_workers=4) as pool:
        recovered = list(pool.map(recover, enumerate(recovery_batches)))
    for batch, result in zip(recovery_batches, recovered, strict=True):
        apply_support_recovery(output, result, {topic["topic_ref"] for topic in batch}, batch)
    _validate_generation_reference_coverage(output, all_refs)
    write(ROOT / "accounting.json", output.model_dump(mode="json"))

    topic_map = {topic["topic_ref"]: topic for topic in topics}
    tracks = [track.model_dump(mode="json") for track in output.tracks]
    definition_batches = [list(range(i, min(i+8, len(tracks)))) for i in range(0, len(tracks), 8)]

    def define(item):
        index, batch = item
        payload = {"scope": scope, "tracks": [
            {"track_ref": f"R{i}", "name": tracks[i]["name"],
             "topics": [topic_map[ref] for ref in tracks[i]["refs"]]} for i in batch]}
        result = call(f"definition_{index}", payload, definition, TrackDefinitionOutput)
        refs = [item.track_ref for item in result.definitions]
        if len(refs) != len(set(refs)) or set(refs) != {f"R{i}" for i in batch}:
            raise RuntimeError("definition output must cover exactly the requested Tracks")
        return result

    with ThreadPoolExecutor(max_workers=4) as pool:
        definitions = list(pool.map(define, enumerate(definition_batches)))
    for batch in definitions:
        for item in batch.definitions:
            tracks[int(item.track_ref[1:])]["definition"] = item.definition
    write(ROOT / "tracks.json", tracks)
    old = IdentifiedTrackBuildOutput.model_validate({"tracks": read(BASE / "tracks.json"),
                                                    "excluded_refs": read(BASE / "audit.json")["unassigned_refs"]})
    old_used = {ref for track in old.tracks for ref in [*track.refs, *track.supporting_refs]}
    new_used = {ref for track in output.tracks for ref in [*track.refs, *track.supporting_refs]}
    responses = [read(p) for p in (ROOT / "calls/responses").glob("*.json")]
    usage = [item["usage"] for item in responses]
    fresh_usage = [item["usage"] for item in responses if not item.get("reused_from")]
    write(ROOT / "metrics.json", {
        "baseline": metrics(old), "before_recovery": before, "after_recovery": metrics(output),
        "newly_used_topics": sorted(new_used-old_used, key=lambda ref: int(ref[1:])),
        "previously_used_now_excluded": sorted(old_used-new_used, key=lambda ref: int(ref[1:])),
        "recovered_support_count": len(new_used)-before["assigned_topic_count"],
        "logical_calls": len(usage),
        "fresh_api_calls": len(fresh_usage),
        "fresh_input_tokens": sum(item.get("input_tokens", item.get("prompt_tokens", 0)) for item in fresh_usage),
        "fresh_output_tokens": sum(item.get("output_tokens", item.get("completion_tokens", 0)) for item in fresh_usage),
        "input_tokens": sum(item.get("input_tokens", item.get("prompt_tokens", 0)) for item in usage),
        "output_tokens": sum(item.get("output_tokens", item.get("completion_tokens", 0)) for item in usage),
    })
    STATE["status"] = "completed"
    STATE["finished_at"] = datetime.now(UTC).isoformat()
    write(ROOT / "status.json", STATE)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        STATE["status"] = "failed"
        STATE["error"] = str(error)
        write(ROOT / "status.json", STATE)
        raise
