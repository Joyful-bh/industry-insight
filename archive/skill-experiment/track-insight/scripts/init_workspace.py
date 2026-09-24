#!/usr/bin/env python3
"""Initialize a file-backed Track Insight research workspace."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


PURPOSE = "识别适合联想 SMB 业务的重点赛道"
SCHEMA_VERSION = "track-insight-workspace-v1"
SKILL_VERSION = "track-insight-v1"


def now_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def parse_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as error:
        raise argparse.ArgumentTypeError("dates must use YYYY-MM-DD") from error


def slug(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", value).strip(" .-")
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:80] or "research"


def write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def initialize(args: argparse.Namespace) -> Path:
    start_date = args.start_date
    end_date = args.end_date
    if start_date > end_date:
        raise ValueError("start-date must not be after end-date")

    parent_manifest = None
    if args.existing_result:
        parent_validation = json.loads(
            (args.existing_result / "validation.json").read_text(encoding="utf-8")
        )
        if parent_validation.get("valid") is not True:
            raise ValueError("existing-result must have validation.json with valid=true")
        parent_manifest = json.loads(
            (args.existing_result / "manifest.json").read_text(encoding="utf-8")
        )

    run_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    default_name = f"{slug(args.topic)}-{slug('-'.join(args.region))}-{run_date}"
    workspace = (args.output or Path("results") / default_name).resolve()
    if workspace.exists():
        raise FileExistsError(f"workspace already exists: {workspace}")

    workspace.mkdir(parents=True)
    (workspace / "pages" / "attachments").mkdir(parents=True)
    (workspace / "evidence").mkdir()

    research_id = f"res_{uuid.uuid4().hex[:16]}"
    created_at = now_iso()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "skill_version": SKILL_VERSION,
        "research_id": research_id,
        "research_purpose": PURPOSE,
        "research_topic": args.topic,
        "target_regions": args.region,
        "start_date": start_date,
        "end_date": end_date,
        "specified_sources": args.source,
        "excluded_directions": args.exclude,
        "expected_track_count": args.expected_track_count,
        "incremental_update": bool(args.existing_result),
        "parent_research_id": args.parent_research_id
        or (parent_manifest or {}).get("research_id"),
        "parent_result_path": str(args.existing_result.resolve()) if args.existing_result else None,
        "created_at": created_at,
        "completed_at": None,
        "status": "running",
        "counts": (parent_manifest or {}).get("counts") or {
            "sources": 0,
            "usable_pages": 0,
            "events": 0,
            "topics": 0,
            "candidate_tracks": 0,
            "watchlist_tracks": 0,
        },
    }
    checkpoint = {
        "current_stage": "planning",
        "completed_stages": [],
        "pending_items": [],
        "quality_gaps": [],
        "next_actions": [
            "读取并验证父版本，规划增量搜索"
            if args.existing_result
            else "制定研究计划并写入 research-plan.md"
        ],
        "updated_at": created_at,
    }
    validation = {
        "valid": False,
        "validated_at": None,
        "checks": [],
        "errors": [],
        "warnings": ["工作区尚未执行校验"],
    }
    research_gate = {
        "passed": False,
        "checked_at": None,
        "metrics": {},
        "failures": [{"code": "not_checked", "message": "research gate has not been checked"}],
        "warnings": [],
    }

    if args.existing_result:
        for relative in (
            "work-packages.json",
            "search-tasks.jsonl",
            "sources.jsonl",
            "events.jsonl",
            "topics.json",
            "tracks.json",
        ):
            source_file = args.existing_result / relative
            if source_file.is_file():
                shutil.copy2(source_file, workspace / relative)
        shutil.copy2(
            args.existing_result / "evidence" / "evidence-index.jsonl",
            workspace / "evidence" / "evidence-index.jsonl",
        )
        shutil.copytree(args.existing_result / "pages", workspace / "pages", dirs_exist_ok=True)

    write_json(workspace / "manifest.json", manifest)
    write_json(workspace / "checkpoint.json", checkpoint)
    if not args.existing_result:
        write_json(
            workspace / "work-packages.json",
            {
                "schema_version": "work-package-v1",
                "minimum_unique_content_urls": 30,
                "minimum_source_classes": 4,
                "maximum_domain_ratio": 0.4,
                "work_packages": [],
            },
        )
        write_json(
            workspace / "topics.json",
            {"schema_version": "topic-v1", "topics": [], "unassigned_event_ids": []},
        )
        write_json(workspace / "tracks.json", {"schema_version": "track-v1", "tracks": []})
    write_json(workspace / "validation.json", validation)
    write_json(workspace / "research-gate.json", research_gate)
    if not args.existing_result:
        for relative in (
            "search-tasks.jsonl",
            "sources.jsonl",
            "events.jsonl",
            "pages/index.jsonl",
            "evidence/evidence-index.jsonl",
        ):
            (workspace / relative).write_text("", encoding="utf-8", newline="\n")
    else:
        if not (workspace / "work-packages.json").is_file():
            write_json(
                workspace / "work-packages.json",
                {
                    "schema_version": "work-package-v1",
                    "minimum_unique_content_urls": 30,
                    "minimum_source_classes": 4,
                    "maximum_domain_ratio": 0.4,
                    "work_packages": [],
                },
            )
        if not (workspace / "search-tasks.jsonl").is_file():
            (workspace / "search-tasks.jsonl").write_text("", encoding="utf-8", newline="\n")

    source_lines = "\n".join(f"- {item}" for item in args.source) or "- 无"
    excluded_lines = "\n".join(f"- {item}" for item in args.exclude) or "- 无"
    plan = f"""# 研究计划

## 研究范围

- 研究目的：{PURPOSE}
- 研究主题：{args.topic}
- 目标区域：{'、'.join(args.region)}
- 时间范围：{start_date} 至 {end_date}

## 指定来源

{source_lines}

## 排除方向

{excluded_lines}

## 研究问题

由执行 Agent 根据研究方法补充。

## 来源覆盖计划

由执行 Agent 根据主题和区域补充。

## 完成条件

正式结果通过证据、引用和 Track 成立条件校验，并生成 `report.md` 与 `dashboard.html`。
"""
    (workspace / "research-plan.md").write_text(plan, encoding="utf-8", newline="\n")
    return workspace


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--topic", required=True)
    result.add_argument("--region", action="append", required=True)
    result.add_argument("--start-date", required=True, type=parse_date)
    result.add_argument("--end-date", required=True, type=parse_date)
    result.add_argument("--source", action="append", default=[])
    result.add_argument("--exclude", action="append", default=[])
    result.add_argument("--expected-track-count", type=int)
    result.add_argument("--existing-result", type=Path)
    result.add_argument("--parent-research-id")
    result.add_argument("--output", type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.expected_track_count is not None and args.expected_track_count < 1:
        print("expected-track-count must be positive", file=sys.stderr)
        return 2
    if args.existing_result and not all(
        (args.existing_result / relative).is_file()
        for relative in ("manifest.json", "validation.json")
    ):
        print("existing-result must contain manifest.json and validation.json", file=sys.stderr)
        return 2
    try:
        workspace = initialize(args)
    except (FileExistsError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({"workspace": str(workspace), "status": "initialized"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
