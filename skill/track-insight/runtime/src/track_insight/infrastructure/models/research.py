import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
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


class PageCapture(TimestampMixin, Base):
    __tablename__ = "page_capture"
    __table_args__ = (
        UniqueConstraint("url_candidate_id", name="uq_page_capture_url_candidate_id"),
        Index("ix_page_capture_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    url_candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("url_candidate.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.PENDING, nullable=False)
    acquisition_method: Mapped[str | None] = mapped_column(String(40))
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(Integer)
    mime_type: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_text: Mapped[str | None] = mapped_column(Text)
    content_chars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    quality_status: Mapped[str | None] = mapped_column(String(40))
    quality_reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PageAnalysis(TimestampMixin, Base):
    __tablename__ = "page_analysis"
    __table_args__ = (
        UniqueConstraint("page_capture_id", name="uq_page_analysis_page_capture_id"),
        Index("ix_page_analysis_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    page_capture_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("page_capture.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.PENDING, nullable=False)
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_run.id"))
    relevance: Mapped[str | None] = mapped_column(String(32))
    document_type: Mapped[str | None] = mapped_column(String(50))
    reason: Mapped[str | None] = mapped_column(Text)
    regions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    industries: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    content_sufficient: Mapped[bool | None] = mapped_column(nullable=True)
    structured_output: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)


class Event(Base):
    __tablename__ = "event"
    __table_args__ = (
        UniqueConstraint("page_analysis_id", "sequence_no", name="uq_event_analysis_sequence"),
        UniqueConstraint("page_analysis_id", "event_fingerprint", name="uq_event_analysis_fp"),
        Index("ix_event_type_signal_date", "event_type", "signal_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    page_analysis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("page_analysis.id"), nullable=False
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    event_status: Mapped[str] = mapped_column(String(32), nullable=False, default="observed")
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    signal_date: Mapped[date | None] = mapped_column(Date)
    date_precision: Mapped[str] = mapped_column(String(20), nullable=False)
    regions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    industries: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    industry_objects: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    topic_hint: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    entities: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list, nullable=False)
    chain_roles: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    smb_relevance: Mapped[str] = mapped_column(String(20), nullable=False)
    smb_reason: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)
    event_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EventEvidence(Base):
    __tablename__ = "event_evidence"
    __table_args__ = (UniqueConstraint("event_id", "evidence_hash", name="uq_event_evidence_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("event.id"), nullable=False)
    page_capture_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("page_capture.id"), nullable=False
    )
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
