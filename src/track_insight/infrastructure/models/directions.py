import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
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


class DirectionBuildRun(Base):
    __tablename__ = "direction_build_run"
    __table_args__ = (
        UniqueConstraint(
            "research_plan_id",
            "input_fingerprint",
            "processor_version",
            name="uq_direction_build_run_input",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    research_plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_plan.id"), nullable=False
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_run.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.PENDING, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    processor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    topic_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    discarded_topic_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    topic_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    direction_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    assigned_topic_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    group_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    statistics: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Direction(TimestampMixin, Base):
    __tablename__ = "direction"
    __table_args__ = (
        UniqueConstraint("direction_build_run_id", "canonical_key", name="uq_direction_run_key"),
        Index("ix_direction_plan_run", "research_plan_id", "direction_build_run_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    research_plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_plan.id"), nullable=False
    )
    direction_build_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direction_build_run.id"), nullable=False
    )
    canonical_key: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    direction_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class DirectionTopic(Base):
    __tablename__ = "direction_topic"
    __table_args__ = (
        UniqueConstraint("direction_id", "topic_id", name="uq_direction_topic"),
        Index("ix_direction_topic_build", "direction_build_run_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    direction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("direction.id"), nullable=False)
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("topic.id"), nullable=False)
    direction_build_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direction_build_run.id"), nullable=False
    )
