#!/usr/bin/env python3
"""Check whether planned search and page acquisition are complete enough for Event extraction."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


TERMINAL_PAGE_STATUSES = {"usable", "insufficient", "inaccessible"}
RETAINED_DECISIONS = {"keep", "maybe"}


def _load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def check_research_gate(workspace: Path) -> dict:
    workspace = workspace.resolve()
    payload = _load_json(workspace / "work-packages.json", {})
    tasks = _load_jsonl(workspace / "search-tasks.jsonl")
    sources = _load_jsonl(workspace / "sources.jsonl")
    pages = _load_jsonl(workspace / "pages" / "index.jsonl")
    failures: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []

    if not isinstance(payload, dict) or not isinstance(payload.get("work_packages"), list):
        failures.append({"code": "invalid_work_packages", "message": "work-packages.json is missing or invalid"})
        packages: list[dict] = []
    else:
        packages = [item for item in payload["work_packages"] if isinstance(item, dict)]

    package_by_id = {
        str(item.get("work_package_id")): item
        for item in packages
        if item.get("work_package_id")
    }
    task_by_id = {
        str(item.get("search_task_id")): item
        for item in tasks
        if item.get("search_task_id")
    }
    if len(package_by_id) != len(packages):
        failures.append({"code": "duplicate_or_missing_work_package_id"})
    if len(task_by_id) != len(tasks):
        failures.append({"code": "duplicate_or_missing_search_task_id"})
    sources_by_id = {
        str(item.get("source_id")): item
        for item in sources
        if item.get("source_id")
    }
    pages_by_source: dict[str, list[dict]] = defaultdict(list)
    for page in pages:
        if page.get("source_id"):
            pages_by_source[str(page["source_id"])].append(page)

    if not packages:
        failures.append({"code": "empty_work_packages", "message": "at least one WorkPackage is required"})
    if not tasks:
        failures.append({"code": "empty_search_tasks", "message": "at least one SearchTask is required"})

    for package_id, package in package_by_id.items():
        if package.get("status") != "completed":
            failures.append({"code": "work_package_incomplete", "object_id": package_id})
        package_tasks = [task for task in tasks if str(task.get("work_package_id")) == package_id]
        if len(package_tasks) < 2:
            failures.append({
                "code": "work_package_has_too_few_tasks",
                "object_id": package_id,
                "actual": len(package_tasks),
                "required": 2,
            })

    for task_id, task in task_by_id.items():
        package_id = str(task.get("work_package_id") or "")
        if package_id not in package_by_id:
            failures.append({"code": "unknown_work_package", "object_id": task_id})
        if task.get("status") != "completed":
            failures.append({"code": "search_task_incomplete", "object_id": task_id})
        if not task.get("searched_at"):
            failures.append({"code": "search_task_missing_timestamp", "object_id": task_id})
        if not isinstance(task.get("result_count"), int) or task.get("result_count", -1) < 0:
            failures.append({"code": "search_task_missing_result_count", "object_id": task_id})
        discovered = task.get("discovered_source_ids")
        if not isinstance(discovered, list):
            failures.append({"code": "invalid_discovered_sources", "object_id": task_id})
            continue
        for source_id in discovered:
            if str(source_id) not in sources_by_id:
                failures.append({"code": "unknown_discovered_source", "object_id": task_id, "source_id": source_id})
            elif task_id not in (sources_by_id[str(source_id)].get("search_task_ids") or []):
                failures.append({"code": "source_task_backlink_missing", "object_id": task_id, "source_id": source_id})

    retained = [
        source for source in sources
        if source.get("decision") in RETAINED_DECISIONS and source.get("url_type") == "content_page"
    ]
    for source in retained:
        source_id = str(source.get("source_id") or "")
        task_ids = source.get("search_task_ids") or []
        discovery_method = source.get("discovery_method")
        if discovery_method not in {"specified_source", "local_file"}:
            if not isinstance(task_ids, list) or not task_ids:
                failures.append({"code": "source_missing_search_task", "object_id": source_id})
            elif any(str(task_id) not in task_by_id for task_id in task_ids):
                failures.append({"code": "source_has_unknown_search_task", "object_id": source_id})
        source_pages = pages_by_source.get(source_id, [])
        if not source_pages:
            failures.append({"code": "source_not_acquired", "object_id": source_id})
        elif not any(page.get("quality_status") in TERMINAL_PAGE_STATUSES for page in source_pages):
            failures.append({"code": "source_acquisition_pending", "object_id": source_id})

    retained_by_package: dict[str, set[str]] = defaultdict(set)
    for source in retained:
        source_id = str(source.get("source_id") or "")
        for task_id in source.get("search_task_ids") or []:
            task = task_by_id.get(str(task_id))
            if task and task.get("work_package_id"):
                retained_by_package[str(task["work_package_id"])].add(source_id)
    for package_id, package in package_by_id.items():
        minimum = package.get("minimum_retained_urls")
        if not isinstance(minimum, int) or minimum < 1:
            failures.append({"code": "invalid_package_url_minimum", "object_id": package_id})
        elif len(retained_by_package[package_id]) < minimum:
            failures.append({
                "code": "work_package_url_coverage_shortfall",
                "object_id": package_id,
                "actual": len(retained_by_package[package_id]),
                "required": minimum,
            })

    unique_urls = {source.get("canonical_url") or source.get("url") for source in retained}
    unique_urls.discard(None)
    source_classes = {source.get("source_class") for source in retained if source.get("source_class")}
    domains = Counter(source.get("domain") for source in retained if source.get("domain"))
    dominant_domain_ratio = max(domains.values(), default=0) / max(len(retained), 1)
    minimum_urls = payload.get("minimum_unique_content_urls", 0) if isinstance(payload, dict) else 0
    minimum_classes = payload.get("minimum_source_classes", 0) if isinstance(payload, dict) else 0
    maximum_domain_ratio = payload.get("maximum_domain_ratio", 1.0) if isinstance(payload, dict) else 1.0
    if len(unique_urls) < minimum_urls:
        failures.append({"code": "unique_url_coverage_shortfall", "actual": len(unique_urls), "required": minimum_urls})
    if len(source_classes) < minimum_classes:
        failures.append({"code": "source_class_coverage_shortfall", "actual": len(source_classes), "required": minimum_classes})
    if retained and dominant_domain_ratio > maximum_domain_ratio:
        failures.append({"code": "dominant_domain_ratio_exceeded", "actual": round(dominant_domain_ratio, 4), "maximum": maximum_domain_ratio})
    usable_count = sum(
        any(page.get("quality_status") == "usable" for page in pages_by_source.get(str(source.get("source_id")), []))
        for source in retained
    )
    if retained and usable_count < len(retained) / 2:
        warnings.append({"code": "low_usable_page_ratio", "usable": usable_count, "retained": len(retained)})

    return {
        "passed": not failures,
        "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "metrics": {
            "work_packages": len(packages),
            "search_tasks": len(tasks),
            "retained_content_urls": len(unique_urls),
            "usable_pages": usable_count,
            "source_classes": sorted(str(item) for item in source_classes),
            "independent_domains": len(domains),
            "dominant_domain_ratio": round(dominant_domain_ratio, 4),
        },
        "failures": failures,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    result = check_research_gate(args.workspace)
    if args.write:
        destination = args.workspace.resolve() / "research-gate.json"
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
