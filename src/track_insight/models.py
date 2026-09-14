import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from track_insight.enums import (
    JobStatus,
    ParseStatus,
    RelevanceLabel,
    RunStatus,
    SourceObservationStatus,
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def enum_column(enum_type: type, length: int = 32) -> Enum:
    return Enum(
        enum_type, native_enum=False, length=length, values_callable=lambda e: [x.value for x in e]
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ConfigVersion(Base):
    __tablename__ = "config_versions"
    __table_args__ = (UniqueConstraint("kind", "fingerprint"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Source(TimestampMixin, Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    publisher: Mapped[str | None] = mapped_column(String(300))
    source_level: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    regions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    content_types: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    entry_urls: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    adapter_key: Mapped[str] = mapped_column(String(100), nullable=False)
    schedule: Mapped[str | None] = mapped_column(String(100))
    rate_limit: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    retry_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    discovery_filter: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    evidence_usage: Mapped[str | None] = mapped_column(Text)
    usage_restrictions: Mapped[str | None] = mapped_column(Text)
    config_version: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    crawl_runs: Mapped[list["CrawlRun"]] = relationship(back_populates="source")
    documents: Mapped[list["Document"]] = relationship(back_populates="source")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[RunStatus] = mapped_column(enum_column(RunStatus), default=RunStatus.PENDING)
    as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    counters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), nullable=False)
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pipeline_runs.id"))
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[RunStatus] = mapped_column(enum_column(RunStatus), default=RunStatus.PENDING)
    cursor_before: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    cursor_after: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    discovered_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fetched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    source: Mapped[Source] = relationship(back_populates="crawl_runs")


class SourceObservation(Base):
    __tablename__ = "source_observations"
    __table_args__ = (
        UniqueConstraint("source_id", "period_start", "period_end"),
        CheckConstraint("period_end > period_start", name="positive_period"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), nullable=False)
    crawl_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("crawl_runs.id"))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[SourceObservationStatus] = mapped_column(
        enum_column(SourceObservationStatus), nullable=False
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RawObject(Base):
    __tablename__ = "raw_objects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(200))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(Integer)
    response_headers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("source_id", "canonical_url"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(300))
    title: Mapped[str | None] = mapped_column(Text)

    source: Mapped[Source] = relationship(back_populates="documents")
    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document", order_by="DocumentVersion.version_no"
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_document_versions_version_no"),
        UniqueConstraint("document_id", "content_sha256", name="uq_document_versions_content"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_object_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_objects.id"), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(String(300))
    document_number: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    region_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(64))
    normalized_text: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    body_sha256: Mapped[str | None] = mapped_column(String(64))
    simhash: Mapped[str | None] = mapped_column(String(32))
    parse_status: Mapped[ParseStatus] = mapped_column(
        enum_column(ParseStatus), default=ParseStatus.PENDING, nullable=False
    )
    parse_quality: Mapped[float | None]
    parser_version: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    document: Mapped[Document] = relationship(back_populates="versions")
    raw_object: Mapped[RawObject] = relationship()


class DocumentRelevanceAssessment(Base):
    __tablename__ = "document_relevance_assessments"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id",
            "classifier_version",
            name="uq_relevance_document_classifier",
        ),
        Index("ix_document_relevance_label", "label"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", name="fk_relevance_document_version"), nullable=False
    )
    label: Mapped[RelevanceLabel] = mapped_column(enum_column(RelevanceLabel), nullable=False)
    score: Mapped[float] = mapped_column(nullable=False)
    signal_types: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    industries: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    regions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    matched_positive_rules: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    matched_negative_rules: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    classifier_type: Mapped[str] = mapped_column(String(32), nullable=False)
    classifier_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        CheckConstraint("max_attempts > 0", name="positive_max_attempts"),
        Index("ix_jobs_claim", "status", "scheduled_at", "priority"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pipeline_runs.id"))
    job_type: Mapped[str] = mapped_column(String(100), nullable=False)
    object_type: Mapped[str] = mapped_column(String(100), nullable=False)
    object_id: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    processor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus), default=JobStatus.PENDING, nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
