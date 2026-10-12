"""Build an offline dashboard from a completed file-based SMB analysis."""

import argparse
import json
import shutil
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from track_insight.infrastructure.database import get_engine
from track_insight.infrastructure.models import Event, EventEvidence, PageCapture, Topic, TopicEvent
from track_insight.planning.coverage import PROVINCES, region_alias
from track_insight.tracks.contracts import SmbAssociationOutput


def load_evidence(session, document):
    topic_ids = {
        uuid.UUID(ref)
        for track in document["tracks"]
        for key in ("topic_ids", "supporting_topic_ids")
        for ref in track.get(key, [])
    }
    topics = {
        str(topic.id): {
            "id": str(topic.id),
            "label": topic.label,
            "definition": topic.definition,
            "event_ids": [],
        }
        for topic in session.scalars(select(Topic).where(Topic.id.in_(topic_ids)))
    }
    if len(topics) != len(topic_ids):
        raise ValueError("Some referenced Topics are missing")
    for topic_id, event_id in session.execute(
        select(TopicEvent.topic_id, TopicEvent.event_id).where(TopicEvent.topic_id.in_(topic_ids))
    ):
        topics[str(topic_id)]["event_ids"].append(str(event_id))
    event_ids = {uuid.UUID(ref) for topic in topics.values() for ref in topic["event_ids"]}
    event_ids.update(
        uuid.UUID(ref)
        for track in document["tracks"]
        for key in ("identity_event_ids", "supporting_event_ids")
        for ref in track.get(key, [])
    )
    events = {
        str(event.id): {
            "id": str(event.id),
            "title": event.title,
            "summary": event.summary,
            "date": event.signal_date.isoformat() if event.signal_date else "",
            "type": event.event_type,
            "regions": event.regions,
            "sources": [],
        }
        for event in session.scalars(select(Event).where(Event.id.in_(event_ids)))
    }
    if len(events) != len(event_ids):
        raise ValueError("Some referenced Events are missing")
    for event_id, url, final_url, requested_url in session.execute(
        select(
            EventEvidence.event_id,
            EventEvidence.source_url,
            PageCapture.final_url,
            PageCapture.requested_url,
        )
        .join(PageCapture, PageCapture.id == EventEvidence.page_capture_id)
        .where(EventEvidence.event_id.in_(event_ids))
    ):
        source = url or final_url or requested_url
        if source and source.startswith(("http://", "https://")):
            if source not in events[str(event_id)]["sources"]:
                events[str(event_id)]["sources"].append(source)
    return {"topics": topics, "events": events}


def province_summary(event_ids, events):
    counts = Counter()
    national = localized = unmapped = 0
    provinces = (*PROVINCES, "香港特别行政区", "澳门特别行政区", "台湾省")
    for ref in set(event_ids):
        regions = events.get(ref, {}).get("regions", [])
        matched = set()
        for region in regions:
            region = region.strip()
            for province in provinces:
                alias = region_alias(province).replace("特别行政区", "")
                if (
                    region.startswith(province)
                    or region == alias
                    or province in ("北京市", "天津市", "上海市", "重庆市")
                    and region.startswith(alias)
                ):
                    matched.add(province)
        counts.update(matched)
        localized += bool(matched)
        national += "全国" in regions
        unmapped += not matched and "全国" not in regions
    return {
        "province_counts": [
            {"province": province, "count": count}
            for province, count in sorted(counts.items(), key=lambda row: (-row[1], row[0]))
        ],
        "localized_events": localized,
        "national_events": national,
        "unmapped_events": unmapped,
    }


def dashboard_payload(document, evidence=None):
    evidence = evidence or {"topics": {}, "events": {}}
    tracks = []
    for index, track in enumerate(document["tracks"]):
        associations = SmbAssociationOutput.model_validate(track).model_dump()["smb_value_analysis"]
        core_ids = set(track.get("identity_event_ids", []))
        topic_ids = set(track.get("topic_ids", [])) | set(track.get("supporting_topic_ids", []))
        event_ids = core_ids | set(track.get("supporting_event_ids", []))
        for ref in topic_ids:
            event_ids.update(evidence["topics"].get(ref, {}).get("event_ids", []))
        tracks.append(
            {
                "index": index + 1,
                "name": track["name"],
                "definition": track["definition"],
                "status": track["status"],
                "associations": associations,
                "topic_count": len(topic_ids),
                "event_count": len(event_ids),
                **province_summary(event_ids, evidence["events"]),
                "core_event_ids": sorted(core_ids),
                "topic_ids": sorted(set(track.get("topic_ids", []))),
                "supporting_topic_ids": sorted(set(track.get("supporting_topic_ids", []))),
                "event_ids": sorted(
                    event_ids,
                    key=lambda ref: (evidence["events"].get(ref, {}).get("date", ""), ref),
                    reverse=True,
                ),
            }
        )
    return {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "tracks": sorted(tracks, key=lambda track: (-track["event_count"], track["index"])),
        "evidence": evidence,
        "counts": dict(Counter(track["status"] for track in tracks)),
        "with_associations": sum(bool(track["associations"]) for track in tracks),
    }


def render(payload):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return TEMPLATE.replace("__DATA__", data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/track_quality/smb_association_full/run_1/tracks.json"),
    )
    parser.add_argument("--output", type=Path, default=Path("reports/track_dashboard_latest.html"))
    args = parser.parse_args()
    status = json.loads((args.input.parent / "status.json").read_text(encoding="utf-8"))
    document = json.loads(args.input.read_text(encoding="utf-8"))
    if status["status"] != "completed" or status["completed"] != len(document["tracks"]):
        raise ValueError("A complete SMB analysis is required")
    engine = get_engine()
    try:
        with Session(engine) as session:
            evidence = load_evidence(session, document)
    finally:
        engine.dispose()
    payload = dashboard_payload(document, evidence)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        archive = args.output.parent / "archive"
        archive.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        shutil.copy2(args.output, archive / f"{args.output.stem}_{stamp}.html")
    args.output.write_text(render(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "dashboard": str(args.output.resolve()),
                "tracks": len(payload["tracks"]),
                "counts": payload["counts"],
                "with_associations": payload["with_associations"],
            },
            ensure_ascii=False,
        )
    )


TEMPLATE = (Path(__file__).parent / "templates" / "smb_dashboard.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    main()
