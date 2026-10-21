#!/usr/bin/env python3
"""Prepare and complete a bounded Agent-native demonstration."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

RUNTIME_SRC = Path(__file__).resolve().parents[1] / "runtime" / "src"
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

sys.path.insert(0, str(RUNTIME_SRC.parent))


def prepare(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    target = workspace / "agent-demo"
    target.mkdir(parents=True, exist_ok=True)
    (workspace / "reports").mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": "agent_native_demo",
        "scope": {
            "regions": args.region,
            "industries": args.industry,
            "start_date": args.start_date,
            "end_date": args.end_date,
        },
        "instructions": [
            "Use current web tools or user-provided materials; do not invent sources.",
            "Prefer primary government, association, park, and company sources.",
            "Keep fact, analysis, and SMB recommendation distinguishable.",
            "This is a limited demonstration, not complete market coverage or trend proof.",
        ],
        "result_schema": {
            "summary": "string",
            "coverage_note": "string",
            "tracks": [
                {
                    "name": "string",
                    "definition": "string",
                    "why_now": "string",
                    "smb_value": "string",
                    "confidence": "number 0..1",
                    "smb_value_analysis": [
                        {"workload": "string", "product_category": "string", "reason": "string"}
                    ],
                    "evidence": [
                        {"title": "string", "url": "https URL", "quote": "string", "date": "string"}
                    ],
                }
            ],
        },
    }
    path = target / "task.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"task": str(path)}, ensure_ascii=False))
    return 0


def validate_result(payload: dict) -> list[dict]:
    if not isinstance(payload.get("summary"), str) or not payload["summary"].strip():
        raise ValueError("summary is required")
    tracks = payload.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        raise ValueError("at least one track is required")
    for index, track in enumerate(tracks):
        for field in ("name", "definition", "why_now", "smb_value"):
            if not isinstance(track.get(field), str) or not track[field].strip():
                raise ValueError(f"tracks[{index}].{field} is required")
        confidence = track.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError(f"tracks[{index}].confidence must be between 0 and 1")
        evidence = track.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError(f"tracks[{index}] needs evidence")
        for item in evidence:
            required = ("title", "url", "quote")
            if not all(
                isinstance(item.get(field), str) and item[field].strip() for field in required
            ):
                raise ValueError(f"tracks[{index}] has incomplete evidence")
            parsed = urlparse(item["url"])
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"tracks[{index}] has invalid evidence URL")
    return tracks


def write_database(path: Path, task: dict, result: dict, tracks: list[dict]) -> str:
    run_id = str(uuid.uuid4())
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS demo_run (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, scope_json TEXT NOT NULL,
              summary TEXT NOT NULL, coverage_note TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS demo_track (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, name TEXT NOT NULL,
              definition TEXT NOT NULL, why_now TEXT NOT NULL, smb_value TEXT NOT NULL,
              confidence REAL NOT NULL, evidence_json TEXT NOT NULL,
              FOREIGN KEY(run_id) REFERENCES demo_run(id)
            );
            """
        )
        connection.execute(
            "INSERT INTO demo_run VALUES (?, ?, ?, ?, ?)",
            (
                run_id,
                datetime.now(UTC).isoformat(),
                json.dumps(task["scope"], ensure_ascii=False),
                result["summary"],
                result.get("coverage_note", "Agent 原生演示，覆盖有限。"),
            ),
        )
        for track in tracks:
            connection.execute(
                "INSERT INTO demo_track VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()), run_id, track["name"], track["definition"],
                    track["why_now"], track["smb_value"], float(track["confidence"]),
                    json.dumps(track["evidence"], ensure_ascii=False),
                ),
            )
    return run_id


def dashboard_payload(task: dict, result: dict, tracks: list[dict]) -> dict:
    from bundled_renderer import dashboard_payload as official_payload
    document = {"tracks": []}
    evidence = {"topics": {}, "events": {}}
    for index, track in enumerate(tracks, 1):
        refs = []
        for number, item in enumerate(track["evidence"], 1):
            ref = f"D{index}E{number}"
            refs.append(ref)
            evidence["events"][ref] = {
                "id": ref, "title": item["title"], "summary": item["quote"],
                "date": item.get("date", ""), "type": "agent_demo",
                "regions": item.get("regions", []), "sources": [item["url"]],
            }
        document["tracks"].append({
            "name": track["name"], "definition": track["definition"],
            "status": track.get("status", "candidate"), "supporting_event_ids": refs,
            "topic_ids": [f"D{index}T"],
            "smb_value_analysis": track.get("smb_value_analysis", []),
        })
        evidence["topics"][f"D{index}T"] = {
            "id": f"D{index}T", "label": "Agent 近期信号分析：" + track["name"],
            "definition": track["why_now"], "event_ids": refs,
        }
    payload = official_payload(document, evidence)
    payload["agent_demo"] = {
        "summary": result["summary"], "coverage_note": result.get("coverage_note", ""),
        "scope": task["scope"],
    }
    return payload
def complete(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    sys.path.insert(0, str(workspace / ".python-deps"))
    from bundled_renderer import render as render_dashboard
    task_path = workspace / "agent-demo" / "task.json"
    if not task_path.is_file():
        raise SystemExit("task.json not found; run prepare first")
    task = json.loads(task_path.read_text(encoding="utf-8"))
    result = json.loads(args.result.resolve().read_text(encoding="utf-8"))
    tracks = validate_result(result)
    run_id = write_database(workspace / "state.sqlite3", task, result, tracks)
    output = workspace / "reports" / "agent_demo_dashboard.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = dashboard_payload(task, result, tracks)
    output.write_text(render_dashboard(payload), encoding="utf-8")
    output.with_suffix(".json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output.parent / "agent_demo_scope.json").write_text(
        json.dumps({"mode": "agent_native_demo", "scope": task["scope"],
                    "coverage_note": result.get("coverage_note", "范围有限的演示")},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"run_id": run_id, "tracks": len(tracks), "dashboard": str(output)},
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--workspace", type=Path, required=True)
    prepare_parser.add_argument("--region", action="append", required=True)
    prepare_parser.add_argument("--industry", action="append", required=True)
    prepare_parser.add_argument("--start-date", required=True)
    prepare_parser.add_argument("--end-date", required=True)
    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("--workspace", type=Path, required=True)
    complete_parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    return prepare(args) if args.command == "prepare" else complete(args)


if __name__ == "__main__":
    raise SystemExit(main())
