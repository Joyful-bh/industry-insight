import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from track_insight.core.enums import TaskStatus
from track_insight.infrastructure.models.base import Base, TimestampMixin


class TrackBuildRun(Base):
    __tablename__ = "track_build_run"
    __table_args__ = (
        UniqueConstraint(
            "research_plan_id",
            "input_fingerprint",
            "processor_version",
            name="uq_track_build_run_input",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    research_plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_plan.id"), nullable=False
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_run.id"), nullable=False
    )
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_run.id"))
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.PENDING, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    processor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    topic_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    topic_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    track_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unassigned_topic_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CandidateTrack(TimestampMixin, Base):
    __tablename__ = "candidate_track"
    __table_args__ = (
        UniqueConstraint("research_plan_id", "canonical_key", name="uq_candidate_track_plan_key"),
        Index("ix_candidate_track_plan_status", "research_plan_id", "status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    research_plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_plan.id"), nullable=False
    )
    created_by_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("track_build_run.id"), nullable=False
    )
    updated_by_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("track_build_run.id"), nullable=False
    )
    canonical_key: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    enterprise_archetype: Mapped[str] = mapped_column(Text, nullable=False)
    core_products_services: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    core_company_types: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    supporting_company_types: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    shared_demand_drivers: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    included_activities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    excluded_activities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    chain_roles: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    observable_company_features: Mapped[list[str]] = mapped_column(
        JSON, default=list, nullable=False
    )
    possible_it_needs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    supporting_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    granularity_assessment: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    regions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    track_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class CandidateTrackTopic(Base):
    __tablename__ = "candidate_track_topic"
    __table_args__ = (
        UniqueConstraint("candidate_track_id", "topic_id", name="uq_candidate_track_topic"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    candidate_track_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate_track.id"), nullable=False
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("topic.id"), nullable=False)
    track_build_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("track_build_run.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class CandidateTrackAnalysis(Base):
    __tablename__ = "candidate_track_analysis"
    __table_args__ = (
        UniqueConstraint(
            "candidate_track_id",
            "input_fingerprint",
            "processor_version",
            name="uq_track_analysis_input",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    candidate_track_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate_track.id"), nullable=False
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_run.id"), nullable=False
    )
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_run.id"))
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.PENDING, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    processor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    why_now: Mapped[str | None] = mapped_column(Text)
    signal_statistics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    activity_assessment: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    industry_chain_analysis: Mapped[str | None] = mapped_column(Text)
    smb_value_analysis: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    evidence_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    uncertainties: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
