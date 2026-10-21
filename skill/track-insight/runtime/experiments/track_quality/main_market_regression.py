"""Run the production market normalizer on the corrected frozen 259-Topic replay."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import recover_topics_regression as replay

from track_insight.tracks.contracts import CandidateTrackOutput
from track_insight.tracks.markets import normalize_markets

replay.ROOT = Path("data/track_quality/main_market_regression/run_1")


def main():
    replay.write(replay.ROOT / "status.json", replay.STATE)
    source = Path("data/track_quality/topic_recovery_regression/run_2")
    frozen = replay.read(source / "tracks.json")
    manifest = replay.read(source / "manifest.json")
    scope = json.loads(replay.read(replay.BASE / "calls/inputs/generation_0.json")["messages"][-1]["content"])["scope"]
    topic_ids = {f"T{i}": uuid.uuid5(uuid.NAMESPACE_URL, f"historical-track-topic:T{i}")
                 for i in range(259)}
    reverse = {value: key for key, value in topic_ids.items()}
    tracks = [CandidateTrackOutput(
        name=t["name"], offering_family=t["offering_family"], supplier_role=t["supplier_role"],
        definition=t["definition"], topic_ids=[topic_ids[r] for r in t["refs"]],
        supporting_topic_ids=[topic_ids[r] for r in t["supporting_refs"]],
        status="candidate", confidence=0.7,
    ) for t in frozen]
    prompts = {key: Path(f"config/prompts/track_market_{key}_v1.md").read_text(encoding="utf-8")
               for key in ("catalog", "assignment", "audit")}
    replay.write(replay.ROOT / "manifest.json", {
        "model": replay.MODEL, "input_topic_count": 259, "candidate_count": len(tracks),
        "source": str(source), "input_sha256": manifest["input_sha256"],
        "upstream_reused": True, "production_normalizer": "track_insight.tracks.markets.normalize_markets",
        "database_modified": False, "prompts": prompts,
    })
    replay.write(replay.ROOT / "input_tracks.json", frozen)

    def complete(task, payload, schema):
        prompt = prompts["assignment" if task.startswith("assignment_") else task]
        return replay.call(task, payload, prompt, schema)

    with ThreadPoolExecutor(max_workers=4) as pool:
        result, stats = normalize_markets(tracks, scope, complete,
                                          lambda fn, items: list(pool.map(fn, items)))
    final = [dict(name=t.name, offering_family=t.offering_family, supplier_role=t.supplier_role,
                  definition=t.definition, refs=[reverse[x] for x in t.topic_ids],
                  supporting_refs=[reverse[x] for x in t.supporting_topic_ids]) for t in result]
    accounting = replay.read(source / "accounting.json")
    replay.write(replay.ROOT / "tracks.json", final)
    replay.write(replay.ROOT / "accounting.json", {
        "tracks": final, "excluded_refs": accounting["excluded_refs"],
    })
    assigned = {ref for t in final for ref in t["refs"] + t["supporting_refs"]}
    assert len(assigned) == 136 and len(assigned | set(accounting["excluded_refs"])) == 259
    usage = [replay.read(p)["usage"] for p in (replay.ROOT / "calls/responses").glob("*.json")]
    replay.write(replay.ROOT / "metrics.json", {
        **stats, "assigned_topics": len(assigned), "unassigned_topics": len(accounting["excluded_refs"]),
        "api_calls": len(usage), "input_tokens": sum(u.get("prompt_tokens", 0) for u in usage),
        "output_tokens": sum(u.get("completion_tokens", 0) for u in usage),
    })
    replay.STATE["status"] = "completed"
    replay.STATE["finished_at"] = datetime.now(UTC).isoformat()
    replay.write(replay.ROOT / "status.json", replay.STATE)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        replay.STATE["status"] = "failed"
        replay.STATE["error"] = str(error)
        replay.write(replay.ROOT / "status.json", replay.STATE)
        raise
