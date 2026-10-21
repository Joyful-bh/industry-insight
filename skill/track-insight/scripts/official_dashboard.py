"""Adapt persisted pipeline output to the unmodified official dashboard exporter."""
import argparse
import json
import sys
import uuid
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path[:0] = [str(RUNTIME / "src"), str(RUNTIME)]

from bundled_renderer import dashboard_payload, load_evidence, render
from track_insight.infrastructure.database import session_scope
from track_insight.pipeline.service import full_pipeline_status
from track_insight.tracks.queries import list_tracks


def export_payload(session, plan_id):
    rows = list_tracks(session, plan_id, limit=10000)
    document = {"tracks": []}
    for row in rows:
        if row["status"] not in {"candidate", "watchlist"}:
            continue
        if row["analysis"] is None:
            raise ValueError("Track analysis is incomplete: " + row["name"])
        document["tracks"].append({
            "name": row["name"], "definition": row["definition"], "status": row["status"],
            "topic_ids": [t["topic_id"] for t in row["topics"] if t["role"] == "core"],
            "supporting_topic_ids": [t["topic_id"] for t in row["topics"] if t["role"] != "core"],
            "supporting_event_ids": row["supporting_event_ids"],
            "smb_value_analysis": row["analysis"]["smb_value_analysis"],
        })
    return dashboard_payload(document, load_evidence(session, document))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-id")
    parser.add_argument("--output", type=Path, default=Path("reports/track_dashboard_latest.html"))
    args = parser.parse_args()
    with session_scope() as session:
        plan_id = args.plan_id or full_pipeline_status(session).get("plan_id")
        if not plan_id:
            raise ValueError("No research plan exists")
        payload = export_payload(session, uuid.UUID(plan_id))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(payload), encoding="utf-8")
    args.output.with_suffix(".json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"dashboard": str(args.output.resolve()), "tracks": len(payload["tracks"])}))


if __name__ == "__main__":
    main()
