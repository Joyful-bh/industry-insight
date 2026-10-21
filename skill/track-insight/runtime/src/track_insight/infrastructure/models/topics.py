import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
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


class TopicBuildRun(Base):
    __tablename__ = "topic_build_run"
    __table_args__ = (
        UniqueConstraint(
            "research_plan_id",
            "input_fingerprint",
            "processor_version",
            name="uq_topic_build_run_input",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    research_plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_plan.id"), nullable=False
    )
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pipeline_run.id"))
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.PENDING, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    processor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    reconciliation_prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    event_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    batch_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    topic_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unassigned_event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TopicCandidate(Base):
    __tablename__ = "topic_candidate"
    __table_args__ = (
        UniqueConstraint(
            "topic_build_run_id", "batch_no", "candidate_key", name="uq_topic_candidate_key"
        ),
        Index("ix_topic_candidate_run_batch", "topic_build_run_id", "batch_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    topic_build_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topic_build_run.id"), nullable=False
    )
    model_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("model_run.id"), nullable=False)
    batch_no: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_key: Mapped[str] = mapped_column(String(100), nullable=False)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    regions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    industries: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_memberships: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    candidate_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="generated", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Topic(TimestampMixin, Base):
    __tablename__ = "topic"
    __table_args__ = (
        UniqueConstraint("research_plan_id", "topic_fingerprint", name="uq_topic_plan_fingerprint"),
        UniqueConstraint("research_plan_id", "canonical_key", name="uq_topic_plan_canonical_key"),
        Index("ix_topic_plan_status", "research_plan_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    research_plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_plan.id"), nullable=False
    )
    created_by_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topic_build_run.id"), nullable=False
    )
    updated_by_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topic_build_run.id"), nullable=False
    )
    canonical_key: Mapped[str] = mapped_column(String(100), nullable=False)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    regions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    industries: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[date | None] = mapped_column(Date)
    latest_seen_at: Mapped[date | None] = mapped_column(Date)
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    topic_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class TopicEvent(Base):
    __tablename__ = "topic_event"
    __table_args__ = (UniqueConstraint("topic_id", "event_id", name="uq_topic_event"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("topic.id"), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("event.id"), nullable=False)
    topic_build_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topic_build_run.id"), nullable=False
    )
    relationship: Mapped[str] = mapped_column(String(32), nullable=False)
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False)
    assignment_reason: Mapped[str] = mapped_column(Text, nullable=False)
    processor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TopicMergeDecision(Base):
    __tablename__ = "topic_merge_decision"
    __table_args__ = (
        UniqueConstraint("topic_build_run_id", "candidate_id", name="uq_topic_merge_candidate"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    topic_build_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topic_build_run.id"), nullable=False
    )
    model_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("model_run.id"), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topic_candidate.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target_topic_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("topic.id"))
    canonical_key: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
