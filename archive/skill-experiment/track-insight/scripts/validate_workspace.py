#!/usr/bin/env python3
"""Validate a file-backed Track Insight workspace and its evidence graph."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from check_research_gate import check_research_gate


SOURCE_CLASSES = {
    "government", "park", "association", "company", "industry_media",
    "authoritative_media", "investment_institution", "other",
}
PAGE_RELEVANCE = {"relevant", "possibly_relevant", "irrelevant"}
DOCUMENT_TYPES = {
    "policy",
    "application_or_funding",
    "recognition_or_list",
    "project",
    "enterprise_news",
    "investment_financing",
    "market_report",
    "park_or_cluster",
    "industry_report",
    "other",
}
EVENT_TYPES = {
    "policy_release", "application_or_funding", "recognition_or_list",
    "project_progress", "enterprise_operation", "investment_financing",
    "technology_commercialization", "market_change", "park_or_cluster",
    "other_industry_signal",
}
EVENT_STATUSES = {"published", "supported", "planned", "in_progress", "completed", "observed"}
DATE_PRECISIONS = {"day", "month", "year", "unknown"}
SMB_RELEVANCE = {"high", "medium", "low", "unclear"}
TRACK_STATUSES = {"candidate", "watchlist"}
TOPIC_ROLES = {"core", "supporting", "adjacent"}
ACTIVITY_LABELS = {"newly_observed", "recently_active", "continuously_active", "insufficient_history"}
REQUIRED_FILES = {
    "manifest.json", "checkpoint.json", "research-plan.md", "work-packages.json",
    "search-tasks.jsonl", "research-gate.json", "sources.jsonl",
    "pages/index.jsonl", "events.jsonl", "topics.json", "tracks.json",
    "evidence/evidence-index.jsonl",
}


class Validator:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.errors: list[dict[str, str]] = []
        self.warnings: list[dict[str, str]] = []
        self.checks: list[dict[str, object]] = []

    def error(self, code: str, message: str, object_id: object | None = None) -> None:
        item = {"code": code, "message": message}
        if object_id is not None:
            item["object_id"] = str(object_id)
        self.errors.append(item)

    def warn(self, code: str, message: str, object_id: object | None = None) -> None:
        item = {"code": code, "message": message}
        if object_id is not None:
            item["object_id"] = str(object_id)
        self.warnings.append(item)

    def check(self, name: str, before: int, checked: int) -> None:
        self.checks.append({
            "name": name,
            "status": "passed" if len(self.errors) == before else "failed",
            "checked": checked,
            "failed": len(self.errors) - before,
        })

    def json_file(self, relative: str, default: object) -> object:
        path = self.workspace / relative
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            self.error("invalid_json", f"{relative}: {error}")
            return default

    def jsonl_file(self, relative: str) -> list[dict]:
        path = self.workspace / relative
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            self.error("unreadable_jsonl", f"{relative}: {error}")
            return []
        rows = []
        for line_no, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                self.error("invalid_jsonl", f"{relative}:{line_no}: {error}")
                continue
            if not isinstance(value, dict):
                self.error("invalid_jsonl_object", f"{relative}:{line_no}: expected object")
                continue
            rows.append(value)
        return rows

    def contained_file(self, relative: object) -> Path | None:
        if not isinstance(relative, str) or not relative:
            return None
        candidate = (self.workspace / relative).resolve()
        if not candidate.is_relative_to(self.workspace):
            return None
        return candidate


def unique_index(validator: Validator, rows: list[dict], key: str, object_type: str) -> dict[str, dict]:
    result = {}
    for row in rows:
        value = row.get(key)
        if not isinstance(value, str) or not value:
            validator.error("missing_id", f"{object_type} is missing {key}")
        elif value in result:
            validator.error("duplicate_id", f"duplicate {key}: {value}", value)
        else:
            result[value] = row
    return result


def valid_date(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def required_strings(record: dict, fields: tuple[str, ...], validator: Validator, object_id: object) -> None:
    for field in fields:
        if not isinstance(record.get(field), str) or not record[field].strip():
            validator.error("missing_field", f"{field} must be a non-empty string", object_id)


def validate(workspace: Path) -> dict:
    v = Validator(workspace)
    before = len(v.errors)
    for relative in sorted(REQUIRED_FILES):
        if not (v.workspace / relative).is_file():
            v.error("missing_file", f"required file is missing: {relative}")
    v.check("required_files", before, len(REQUIRED_FILES))

    manifest = v.json_file("manifest.json", {})
    checkpoint = v.json_file("checkpoint.json", {})
    topics_payload = v.json_file("topics.json", {"topics": [], "unassigned_event_ids": []})
    tracks_payload = v.json_file("tracks.json", {"tracks": []})
    sources = v.jsonl_file("sources.jsonl")
    pages = v.jsonl_file("pages/index.jsonl")
    events = v.jsonl_file("events.jsonl")
    evidence_index_rows = v.jsonl_file("evidence/evidence-index.jsonl")

    before = len(v.errors)
    if not isinstance(manifest, dict):
        v.error("invalid_manifest", "manifest.json must contain an object")
        manifest = {}
    required_strings(manifest, ("schema_version", "skill_version", "research_id", "research_purpose", "research_topic", "start_date", "end_date", "status"), v, "manifest")
    if not isinstance(manifest.get("target_regions"), list) or not manifest.get("target_regions"):
        v.error("invalid_regions", "manifest.target_regions must be a non-empty array")
    if manifest.get("status") not in {"running", "completed", "incomplete", "failed"}:
        v.error("invalid_status", "manifest.status is invalid")
    if not valid_date(manifest.get("start_date")) or not valid_date(manifest.get("end_date")):
        v.error("invalid_date", "manifest start/end dates must use YYYY-MM-DD")
    if isinstance(checkpoint, dict) and checkpoint.get("current_stage") not in {
        "planning", "discovery", "acquisition", "event_extraction", "topic_synthesis",
        "track_synthesis", "track_analysis", "validation", "reporting", "completed",
    }:
        v.error("invalid_stage", "checkpoint.current_stage is invalid")
    v.check("manifest_and_checkpoint", before, 2)

    source_by_id = unique_index(v, sources, "source_id", "Source")
    page_by_id = unique_index(v, pages, "page_id", "Page")
    event_by_id = unique_index(v, events, "event_id", "Event")
    evidence_by_id = unique_index(v, evidence_index_rows, "evidence_id", "Evidence")

    before = len(v.errors)
    canonical_urls: set[str] = set()
    for source in sources:
        source_id = source.get("source_id")
        if source.get("source_class") not in SOURCE_CLASSES:
            v.error("invalid_source_class", "source_class is invalid", source_id)
        if source.get("url_type") not in {"content_page", "source_entry"}:
            v.error("invalid_url_type", "url_type is invalid", source_id)
        if source.get("decision") not in {"keep", "maybe", "drop"}:
            v.error("invalid_source_decision", "decision is invalid", source_id)
        canonical = source.get("canonical_url")
        if canonical:
            if canonical in canonical_urls:
                v.error("duplicate_canonical_url", "canonical_url must be unique", source_id)
            canonical_urls.add(canonical)
        if not source.get("url") and not source.get("local_path"):
            v.error("missing_source_location", "source needs url or local_path", source_id)
    v.check("sources", before, len(sources))

    before = len(v.errors)
    content_cache: dict[str, str] = {}
    for page in pages:
        page_id = page.get("page_id")
        if page.get("source_id") not in source_by_id:
            v.error("unknown_source_ref", "page source_id does not exist", page_id)
        if page.get("quality_status") not in {
            "usable", "insufficient", "inaccessible", "reader_required", "ocr_required"
        }:
            v.error("invalid_page_quality", "page quality_status is invalid", page_id)
        if page.get("relevance") is not None and page.get("relevance") not in PAGE_RELEVANCE:
            v.error("invalid_page_relevance", "page relevance is invalid", page_id)
        if page.get("document_type") is not None and page.get("document_type") not in DOCUMENT_TYPES:
            v.error("invalid_document_type", "page document_type is invalid", page_id)
        content_path = page.get("content_path")
        if page.get("quality_status") == "usable":
            resolved = v.contained_file(content_path)
            if resolved is None or not resolved.is_file():
                v.error("missing_page_content", "usable page content_path is invalid", page_id)
            else:
                content_cache[str(content_path)] = resolved.read_text(encoding="utf-8")
    v.check("pages", before, len(pages))

    before = len(v.errors)
    research_gate = check_research_gate(v.workspace)
    if not research_gate["passed"]:
        v.error(
            "research_gate_failed",
            f"research discovery/acquisition gate has {len(research_gate['failures'])} failure(s)",
        )
    for warning in research_gate["warnings"]:
        v.warn(str(warning.get("code") or "research_gate_warning"), json.dumps(warning, ensure_ascii=False))
    v.check("research_gate", before, research_gate["metrics"].get("search_tasks", 0))

    before = len(v.errors)
    event_evidence_ids: set[str] = set()
    for event in events:
        event_id = event.get("event_id")
        required_strings(event, ("title", "summary", "topic_hint", "smb_reason", "event_fingerprint"), v, event_id)
        if event.get("source_id") not in source_by_id:
            v.error("unknown_source_ref", "event source_id does not exist", event_id)
        page = page_by_id.get(event.get("page_id"))
        if page is None:
            v.error("unknown_page_ref", "event page_id does not exist", event_id)
        elif page.get("source_id") != event.get("source_id"):
            v.error("source_page_mismatch", "event source_id and page source_id disagree", event_id)
        elif page.get("quality_status") != "usable" or page.get("content_sufficient") is not True:
            v.error("ineligible_event_page", "Event page must be usable and sufficient", event_id)
        elif page.get("relevance") == "irrelevant":
            v.error("irrelevant_event_page", "irrelevant page cannot produce Events", event_id)
        if event.get("event_type") not in EVENT_TYPES:
            v.error("invalid_event_type", "event_type is invalid", event_id)
        if event.get("event_status") not in EVENT_STATUSES:
            v.error("invalid_event_status", "event_status is invalid", event_id)
        if event.get("date_precision") not in DATE_PRECISIONS or not valid_date(event.get("signal_date")):
            v.error("invalid_event_date", "signal_date or date_precision is invalid", event_id)
        if event.get("smb_relevance") not in SMB_RELEVANCE:
            v.error("invalid_smb_relevance", "smb_relevance is invalid", event_id)
        confidence = event.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            v.error("invalid_confidence", "confidence must be between 0 and 1", event_id)
        evidence = event.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            v.error("missing_evidence", "event must contain evidence", event_id)
            evidence = []
        coverage = event.get("evidence_coverage") or {}
        for key in ("subject", "action", "date", "numbers"):
            indexes = coverage.get(key, []) if isinstance(coverage, dict) else []
            if not isinstance(indexes, list) or any(not isinstance(i, int) or i < 0 or i >= len(evidence) for i in indexes):
                v.error("invalid_evidence_coverage", f"invalid {key} evidence indexes", event_id)
        if not coverage.get("subject") or not coverage.get("action"):
            v.error("incomplete_evidence_coverage", "subject and action require evidence", event_id)
        if event.get("signal_date") and not coverage.get("date"):
            v.error("missing_date_evidence", "signal_date requires date evidence", event_id)
        if re.search(r"\d", str(event.get("summary") or "")) and not coverage.get("numbers"):
            v.error("missing_number_evidence", "numeric summary requires number evidence", event_id)
        for item in evidence:
            evidence_id = item.get("evidence_id") if isinstance(item, dict) else None
            if not evidence_id or evidence_id in event_evidence_ids:
                v.error("invalid_evidence_id", "event evidence_id is missing or duplicated", event_id)
                continue
            event_evidence_ids.add(evidence_id)
            if item.get("source_id") != event.get("source_id") or item.get("page_id") != event.get("page_id"):
                v.error("evidence_ref_mismatch", "evidence references disagree with Event", evidence_id)
            content_path = item.get("content_path")
            resolved = v.contained_file(content_path)
            if resolved is None or not resolved.is_file():
                v.error("missing_evidence_content", "evidence content_path is invalid", evidence_id)
                continue
            text = content_cache.setdefault(str(content_path), resolved.read_text(encoding="utf-8"))
            quote = item.get("quote")
            if not isinstance(quote, str) or not quote.strip() or quote not in text:
                v.error("unresolvable_quote", "evidence quote is not present in page text", evidence_id)
            if evidence_id not in evidence_by_id:
                v.error("missing_evidence_index", "evidence is absent from evidence-index.jsonl", evidence_id)
            else:
                indexed = evidence_by_id[evidence_id]
                for field in ("event_id", "source_id", "page_id", "content_path", "quote"):
                    expected = event_id if field == "event_id" else item.get(field)
                    if indexed.get(field) != expected:
                        v.error(
                            "evidence_index_mismatch",
                            f"evidence index field {field} disagrees with Event evidence",
                            evidence_id,
                        )
    for evidence_id in evidence_by_id:
        if evidence_id not in event_evidence_ids:
            v.warn("orphan_evidence_index", "indexed evidence is not referenced by an Event", evidence_id)
    v.check("events_and_evidence", before, len(events))

    before = len(v.errors)
    if not isinstance(topics_payload, dict) or not isinstance(topics_payload.get("topics"), list):
        v.error("invalid_topics_file", "topics.json must contain a topics array")
        topics = []
    else:
        topics = topics_payload["topics"]
    topic_by_id = unique_index(v, topics, "topic_id", "Topic")
    assigned_events: set[str] = set()
    for topic in topics:
        topic_id = topic.get("topic_id")
        required_strings(topic, ("canonical_key", "label", "definition", "summary"), v, topic_id)
        confidence = topic.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            v.error("invalid_confidence", "Topic confidence must be between 0 and 1", topic_id)
        event_ids = topic.get("event_ids")
        if not isinstance(event_ids, list) or not event_ids:
            v.error("empty_topic", "Topic must reference at least one Event", topic_id)
            continue
        for event_id in event_ids:
            if event_id not in event_by_id:
                v.error("unknown_event_ref", "Topic references unknown Event", topic_id)
            assigned_events.add(event_id)
    unassigned = topics_payload.get("unassigned_event_ids", []) if isinstance(topics_payload, dict) else []
    for event_id in unassigned:
        if event_id not in event_by_id:
            v.error("unknown_unassigned_event", "unassigned_event_ids contains unknown Event", event_id)
    omitted = set(event_by_id) - assigned_events - set(unassigned)
    if omitted:
        v.warn("unaccounted_events", f"{len(omitted)} Events are neither assigned nor unassigned")
    v.check("topics", before, len(topics))

    before = len(v.errors)
    if not isinstance(tracks_payload, dict) or not isinstance(tracks_payload.get("tracks"), list):
        v.error("invalid_tracks_file", "tracks.json must contain a tracks array")
        tracks = []
    else:
        tracks = tracks_payload["tracks"]
    unique_index(v, tracks, "track_id", "Track")
    for track in tracks:
        track_id = track.get("track_id")
        required_strings(track, ("canonical_key", "name", "definition", "enterprise_archetype"), v, track_id)
        if track.get("status") not in TRACK_STATUSES:
            v.error("invalid_track_status", "Track status is invalid", track_id)
        memberships = track.get("topic_memberships")
        if not isinstance(memberships, list) or not memberships:
            v.error("missing_topic_memberships", "Track needs Topic memberships", track_id)
            memberships = []
        for membership in memberships:
            if membership.get("topic_id") not in topic_by_id:
                v.error("unknown_topic_ref", "Track references unknown Topic", track_id)
            if membership.get("role") not in TOPIC_ROLES:
                v.error("invalid_topic_role", "Track Topic role is invalid", track_id)
        supporting_event_ids = track.get("supporting_event_ids")
        if not isinstance(supporting_event_ids, list) or not supporting_event_ids:
            v.error("missing_track_events", "Track needs supporting Events", track_id)
            supporting_event_ids = []
        for event_id in supporting_event_ids:
            if event_id not in event_by_id:
                v.error("unknown_event_ref", "Track references unknown Event", track_id)
        track_list_fields = (
            "core_products_services",
            "core_company_types",
            "included_activities",
            "excluded_activities",
            "chain_roles",
            "observable_company_features",
            "possible_it_needs",
        )
        for field in track_list_fields:
            if not isinstance(track.get(field), list):
                v.error("invalid_track_field", f"{field} must be an array", track_id)
            elif track.get("status") == "candidate" and not track[field]:
                v.error("empty_candidate_field", f"candidate Track requires {field}", track_id)
        analysis = track.get("analysis")
        if not isinstance(analysis, dict):
            v.error("missing_track_analysis", "Track analysis must be an object", track_id)
            continue
        required_strings(analysis, ("summary", "why_now", "industry_chain_analysis"), v, track_id)
        activity = analysis.get("activity_assessment") or {}
        if activity.get("label") not in ACTIVITY_LABELS:
            v.error("invalid_activity_label", "activity label is invalid", track_id)
        value_items = analysis.get("smb_value_analysis")
        if not isinstance(value_items, list) or not value_items:
            v.error("missing_smb_value", "Track needs SMB value analysis", track_id)
        else:
            for item in value_items:
                required_strings(item, ("business_activity", "workload", "it_need", "coverage_path"), v, track_id)
        for event_id in analysis.get("evidence_event_ids", []):
            if event_id not in event_by_id:
                v.error("unknown_analysis_event", "Track analysis references unknown Event", track_id)
    v.check("tracks", before, len(tracks))

    before = len(v.errors)
    if manifest.get("status") == "completed":
        for relative in ("report.md", "dashboard.html"):
            path = v.workspace / relative
            if not path.is_file() or path.stat().st_size == 0:
                v.error("missing_deliverable", f"completed workspace requires {relative}")
        if checkpoint.get("current_stage") != "completed":
            v.error("incomplete_checkpoint", "completed manifest requires completed checkpoint")
        try:
            completed_at = datetime.fromisoformat(str(manifest.get("completed_at")))
            if completed_at.tzinfo is None:
                raise ValueError
        except ValueError:
            v.error("invalid_completed_at", "completed manifest requires a timezone-aware completed_at")
    counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
    actual_counts = {
        "sources": len(sources),
        "usable_pages": sum(page.get("quality_status") == "usable" for page in pages),
        "events": len(events),
        "topics": len(topics),
        "candidate_tracks": sum(track.get("status") == "candidate" for track in tracks),
        "watchlist_tracks": sum(track.get("status") == "watchlist" for track in tracks),
    }
    for key, actual in actual_counts.items():
        if counts.get(key) != actual:
            message = f"manifest counts.{key}={counts.get(key)!r}, actual={actual}"
            if manifest.get("status") == "completed":
                v.error("count_mismatch", message)
            else:
                v.warn("count_mismatch", message)
    v.check("delivery_and_counts", before, len(actual_counts))

    return {
        "valid": not v.errors,
        "validated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "checks": v.checks,
        "errors": v.errors,
        "warnings": v.warnings,
        "actual_counts": actual_counts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    result = validate(args.workspace)
    if args.write:
        path = args.workspace.resolve() / "validation.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
