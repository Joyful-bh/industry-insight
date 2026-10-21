#!/usr/bin/env python3
"""Extract normalized track records from the self-contained HTML dashboard."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


DATA_PATTERN = re.compile(r"const\s+DATA\s*=\s*(\{.*?\})\s*;\s*const\s+esc\s*=", re.DOTALL)

LIST_FIELDS = (
    "aliases",
    "core_products_services",
    "core_company_types",
    "supporting_company_types",
    "shared_demand_drivers",
    "included_activities",
    "excluded_activities",
    "chain_roles",
    "observable_company_features",
    "possible_it_needs",
    "supporting_event_ids",
    "regions",
)

CSV_FIELDS = (
    "track_id",
    "canonical_key",
    "name",
    "status",
    "heat",
    "confidence",
    "definition",
    "enterprise_archetype",
    "core_products_services",
    "core_company_types",
    "supporting_company_types",
    "included_activities",
    "excluded_activities",
    "chain_roles",
    "observable_company_features",
    "possible_it_needs",
    "why_now",
    "uncertainties",
    "regions",
    "event_count",
    "source_count",
)


def parse_dashboard(path: Path) -> dict[str, Any]:
    html = path.read_text(encoding="utf-8")
    match = DATA_PATTERN.search(html)
    if not match:
        raise ValueError(f"Cannot find embedded DATA object in {path}")
    return json.loads(match.group(1))


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def normalize_track(raw: dict[str, Any]) -> dict[str, Any]:
    analysis = raw.get("analysis") if isinstance(raw.get("analysis"), dict) else {}

    def prefer(field: str, default: Any = "") -> Any:
        value = analysis.get(field)
        if value not in (None, "", []):
            return value
        return raw.get(field, default)

    track = {
        "track_id": raw.get("candidate_track_id") or raw.get("canonical_key"),
        "canonical_key": raw.get("canonical_key"),
        "name": raw.get("name", ""),
        "aliases": as_list(raw.get("aliases")),
        "definition": raw.get("definition", ""),
        "enterprise_archetype": prefer("enterprise_archetype"),
        "core_products_services": as_list(prefer("core_products_services", [])),
        "core_company_types": as_list(prefer("core_company_types", [])),
        "supporting_company_types": as_list(prefer("supporting_company_types", [])),
        "shared_demand_drivers": as_list(prefer("shared_demand_drivers", [])),
        "included_activities": as_list(prefer("included_activities", [])),
        "excluded_activities": as_list(prefer("excluded_activities", [])),
        "chain_roles": as_list(prefer("chain_roles", [])),
        "observable_company_features": as_list(prefer("observable_company_features", [])),
        "possible_it_needs": as_list(prefer("possible_it_needs", [])),
        "summary": analysis.get("summary", ""),
        "why_now": analysis.get("why_now", ""),
        "activity_assessment": analysis.get("activity_assessment", {}),
        "industry_chain_analysis": analysis.get("industry_chain_analysis", ""),
        "smb_value_analysis": as_list(analysis.get("smb_value_analysis")),
        "uncertainties": as_list(analysis.get("uncertainties")),
        "signal_statistics": analysis.get("signal_statistics", {}),
        "supporting_event_ids": as_list(raw.get("supporting_event_ids")),
        "topics": as_list(raw.get("topics")),
        "regions": as_list(raw.get("regions")),
        "confidence": raw.get("confidence"),
        "status": raw.get("status", ""),
        "heat": raw.get("heat", ""),
        "event_count": raw.get("event_count", 0),
        "source_count": raw.get("source_count", 0),
    }
    return track


def validate(tracks: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    ids = [track.get("track_id") for track in tracks]
    names = [track.get("name") for track in tracks]
    for label, values in (("track_id", ids), ("name", names)):
        missing = sum(not value for value in values)
        duplicates = [value for value, count in Counter(values).items() if value and count > 1]
        if missing:
            errors.append(f"{missing} track(s) have no {label}")
        if duplicates:
            errors.append(f"duplicate {label}: {', '.join(map(str, duplicates))}")
    for track in tracks:
        for field in ("definition", "included_activities", "excluded_activities"):
            if not track.get(field):
                errors.append(f"{track.get('name') or track.get('track_id')}: missing {field}")
    return errors


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, tracks: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for track in tracks:
            handle.write(json.dumps(track, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_csv(path: Path, tracks: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for track in tracks:
            row = dict(track)
            for field in CSV_FIELDS:
                if isinstance(row.get(field), (list, dict)):
                    row[field] = json.dumps(row[field], ensure_ascii=False, separators=(",", ":"))
            writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="self-contained track dashboard HTML")
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed/tracks"))
    args = parser.parse_args()

    dashboard = parse_dashboard(args.input)
    tracks = [normalize_track(raw) for raw in dashboard.get("tracks", [])]
    errors = validate(tracks)
    if errors:
        raise ValueError("Track validation failed:\n- " + "\n- ".join(errors))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "source_file": str(args.input.as_posix()),
        "generated_at": dashboard.get("generated_at"),
        "plan": dashboard.get("plan", {}),
        "dashboard_metrics": dashboard.get("metrics", {}),
        "extracted_track_count": len(tracks),
        "status_counts": dict(Counter(track["status"] for track in tracks)),
        "heat_counts": dict(Counter(track["heat"] for track in tracks)),
    }
    write_json(args.output_dir / "metadata.json", metadata)
    write_json(args.output_dir / "tracks.json", {"metadata": metadata, "tracks": tracks})
    write_jsonl(args.output_dir / "tracks.jsonl", tracks)
    write_csv(args.output_dir / "tracks.csv", tracks)
    print(f"Extracted {len(tracks)} tracks to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
