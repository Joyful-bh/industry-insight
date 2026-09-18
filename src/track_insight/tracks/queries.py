import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from track_insight.infrastructure.models import (
    CandidateTrack,
    CandidateTrackAnalysis,
    CandidateTrackTopic,
    Topic,
)


def list_tracks(session: Session, plan_id: uuid.UUID, *, limit: int = 50) -> list[dict[str, Any]]:
    tracks = list(
        session.scalars(
            select(CandidateTrack)
            .where(CandidateTrack.research_plan_id == plan_id)
            .order_by(CandidateTrack.status, CandidateTrack.name)
            .limit(limit)
        )
    )
    return [_payload(session, track) for track in tracks]


def show_track(session: Session, track_id: uuid.UUID) -> dict[str, Any] | None:
    track = session.get(CandidateTrack, track_id)
    return _payload(session, track) if track else None


def _payload(session: Session, track: CandidateTrack) -> dict[str, Any]:
    relations = list(
        session.execute(
            select(CandidateTrackTopic, Topic)
            .join(Topic, Topic.id == CandidateTrackTopic.topic_id)
            .where(CandidateTrackTopic.candidate_track_id == track.id)
            .order_by(CandidateTrackTopic.role, Topic.label)
        )
    )
    analysis = session.scalar(
        select(CandidateTrackAnalysis)
        .where(
            CandidateTrackAnalysis.candidate_track_id == track.id,
            CandidateTrackAnalysis.status == "completed",
        )
        .order_by(CandidateTrackAnalysis.created_at.desc())
        .limit(1)
    )
    return {
        "candidate_track_id": str(track.id),
        "canonical_key": track.canonical_key,
        "name": track.name,
        "aliases": track.aliases,
        "definition": track.definition,
        "enterprise_archetype": track.enterprise_archetype,
        "core_products_services": track.core_products_services,
        "core_company_types": track.core_company_types,
        "supporting_company_types": track.supporting_company_types,
        "shared_demand_drivers": track.shared_demand_drivers,
        "included_activities": track.included_activities,
        "excluded_activities": track.excluded_activities,
        "chain_roles": track.chain_roles,
        "observable_company_features": track.observable_company_features,
        "possible_it_needs": track.possible_it_needs,
        "supporting_event_ids": track.supporting_event_ids,
        "granularity_assessment": track.granularity_assessment,
        "regions": track.regions,
        "confidence": track.confidence,
        "status": track.status,
        "topics": [
            {
                "topic_id": str(topic.id),
                "label": topic.label,
                "role": rel.role,
                "relevance_score": rel.relevance_score,
                "reason": rel.reason,
            }
            for rel, topic in relations
        ],
        "analysis": None
        if analysis is None
        else {
            "analysis_id": str(analysis.id),
            "enterprise_archetype": track.enterprise_archetype,
            "core_products_services": track.core_products_services,
            "core_company_types": track.core_company_types,
            "supporting_company_types": track.supporting_company_types,
            "shared_demand_drivers": track.shared_demand_drivers,
            "included_activities": track.included_activities,
            "excluded_activities": track.excluded_activities,
            "chain_roles": track.chain_roles,
            "observable_company_features": track.observable_company_features,
            "possible_it_needs": track.possible_it_needs,
            "summary": analysis.summary,
            "why_now": analysis.why_now,
            "signal_statistics": analysis.signal_statistics,
            "activity_assessment": analysis.activity_assessment,
            "industry_chain_analysis": analysis.industry_chain_analysis,
            "smb_value_analysis": analysis.smb_value_analysis,
            "evidence_event_ids": analysis.evidence_event_ids,
            "uncertainties": analysis.uncertainties,
        },
    }
