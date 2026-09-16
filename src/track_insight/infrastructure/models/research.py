import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from track_insight.core.enums import TaskStatus
from track_insight.infrastructure.models.base import Base, TimestampMixin


class ResearchPlan(Base):
    __tablename__ = "research_plan"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.PENDING, nullable=False)
    config_version: Mapped[str] = mapped_column(String(100), nullable=False)
    regions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    industry_scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    coverage_audit: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_run.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ResearchWorkPackage(Base):
    __tablename__ = "research_work_package"
    __table_args__ = (UniqueConstraint("research_plan_id", "sequence_no"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    research_plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_plan.id"), nullable=False
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.PENDING, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    regions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    industry_scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    signal_types: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_classes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    max_candidates: Mapped[int] = mapped_column(Integer, nullable=False)


class SearchTask(Base):
    __tablename__ = "search_task"
    __table_args__ = (UniqueConstraint("research_work_package_id", "query_fingerprint"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    research_work_package_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_work_package.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.PENDING, nullable=False)
    query: Mapped[str] = mapped_column(String(200), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    target_regions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    target_industries: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    target_signal_types: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    target_source_classes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    query_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_run.id"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    completion_reason: Mapped[str | None] = mapped_column(String(100))


class SearchResult(Base):
    __tablename__ = "search_result"
    __table_args__ = (UniqueConstraint("search_task_id", "refer"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    search_task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("search_task.id"), nullable=False)
    refer: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    snippet: Mapped[str | None] = mapped_column(Text)
    media: Mapped[str | None] = mapped_column(String(300))
    publish_date: Mapped[str | None] = mapped_column(String(100))
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    url_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_class: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class UrlCandidate(TimestampMixin, Base):
    __tablename__ = "url_candidate"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    canonical_url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    url_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)
    source_domain: Mapped[str] = mapped_column(String(300), nullable=False)
    source_class: Mapped[str] = mapped_column(String(50), nullable=False)
    possible_published_at: Mapped[str | None] = mapped_column(String(100))
    decision: Mapped[str] = mapped_column(String(20), nullable=False)


class UrlDiscovery(Base):
    __tablename__ = "url_discovery"
    __table_args__ = (
        UniqueConstraint("url_candidate_id", "research_work_package_id", "search_task_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    url_candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("url_candidate.id"), nullable=False
    )
    research_work_package_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_work_package.id"), nullable=False
    )
    search_task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("search_task.id"), nullable=False)
    search_result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("search_result.id"), nullable=False
    )
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
