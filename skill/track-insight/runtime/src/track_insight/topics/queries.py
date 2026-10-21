import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from track_insight.infrastructure.models import Event, Topic, TopicEvent


def list_topics(
    session: Session, plan_id: uuid.UUID, *, limit: int = 50, with_events: bool = False
) -> list[dict[str, Any]]:
    topics = list(
        session.scalars(
            select(Topic)
            .where(Topic.research_plan_id == plan_id)
            .order_by(Topic.latest_seen_at.desc().nulls_last(), Topic.label)
            .limit(limit)
        )
    )
    return [_topic_payload(session, topic, with_events=with_events) for topic in topics]


def show_topic(session: Session, topic_id: uuid.UUID) -> dict[str, Any] | None:
    topic = session.get(Topic, topic_id)
    return _topic_payload(session, topic, with_events=True) if topic else None


def _topic_payload(session: Session, topic: Topic, *, with_events: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "topic_id": str(topic.id),
        "canonical_key": topic.canonical_key,
        "label": topic.label,
        "definition": topic.definition,
        "aliases": topic.aliases,
        "keywords": topic.keywords,
        "regions": topic.regions,
        "industries": topic.industries,
        "summary": topic.summary,
        "first_seen_at": topic.first_seen_at.isoformat() if topic.first_seen_at else None,
        "latest_seen_at": topic.latest_seen_at.isoformat() if topic.latest_seen_at else None,
        "event_count": topic.event_count,
        "confidence": topic.confidence,
        "status": topic.status,
    }
    if with_events:
        rows = session.execute(
            select(TopicEvent, Event)
            .join(Event, Event.id == TopicEvent.event_id)
            .where(TopicEvent.topic_id == topic.id)
            .order_by(Event.signal_date.asc().nulls_last(), Event.created_at)
        )
        payload["events"] = [
            {
                "event_id": str(event.id),
                "event_type": event.event_type,
                "title": event.title,
                "summary": event.summary,
                "signal_date": event.signal_date.isoformat() if event.signal_date else None,
                "relationship": relation.relationship,
                "relevance_score": relation.relevance_score,
                "assignment_reason": relation.assignment_reason,
            }
            for relation, event in rows
        ]
    return payload
