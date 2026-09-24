#!/usr/bin/env python3
"""Build a standalone Track Insight dashboard from validated workspace files."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def load_jsonl(path: Path) -> list[dict]:
    result = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_no} must contain a JSON object")
        result.append(value)
    return result


def track_payload(track: dict, events: dict[str, dict]) -> dict:
    event_ids = list(dict.fromkeys(track.get("supporting_event_ids") or []))
    source_ids = {
        events[event_id].get("source_id")
        for event_id in event_ids
        if event_id in events and events[event_id].get("source_id")
    }
    independent_groups = {
        events[event_id].get("duplicate_group_id") or event_id
        for event_id in event_ids
        if event_id in events
    }
    event_count = len(independent_groups)
    source_count = len(source_ids)
    heat = "高热度" if event_count >= 5 and source_count >= 3 else "活跃" if event_count >= 2 else "观察"
    return {**track, "event_count": event_count, "source_count": source_count, "heat": heat}


def build(workspace: Path, output: Path | None = None) -> Path:
    workspace = workspace.resolve()
    manifest = load_json(workspace / "manifest.json")
    validation = load_json(workspace / "validation.json")
    if validation.get("valid") is not True:
        raise ValueError("workspace validation must pass before building the dashboard")
    topics_payload = load_json(workspace / "topics.json")
    tracks_payload = load_json(workspace / "tracks.json")
    events_list = load_jsonl(workspace / "events.jsonl")
    sources = load_jsonl(workspace / "sources.jsonl")
    events = {str(item.get("event_id")): item for item in events_list}
    topics = topics_payload.get("topics") or []
    tracks = [track_payload(item, events) for item in tracks_payload.get("tracks") or []]
    payload = {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "plan": {
            "research_topic": manifest.get("research_topic"),
            "regions": manifest.get("target_regions") or [],
            "start_date": manifest.get("start_date"),
            "end_date": manifest.get("end_date"),
        },
        "metrics": {
            "track_count": len(tracks),
            "candidate_count": sum(item.get("status") == "candidate" for item in tracks),
            "watchlist_count": sum(item.get("status") == "watchlist" for item in tracks),
            "topic_count": len(topics),
            "event_count": len(events_list),
            "source_count": len(sources),
        },
        "tracks": tracks,
    }
    template_path = Path(__file__).resolve().parents[1] / "assets" / "dashboard-template.html"
    template = template_path.read_text(encoding="utf-8")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    rendered = template.replace("__DASHBOARD_DATA__", encoded)
    destination = output.resolve() if output else workspace / "dashboard.html"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8", newline="\n")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    destination = build(args.workspace, args.output)
    print(json.dumps({"dashboard": str(destination), "status": "built"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
