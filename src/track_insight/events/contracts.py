from datetime import date
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator

from track_insight.core.enums import (
    DatePrecision,
    DocumentType,
    EventStatus,
    EventType,
    PageRelevance,
    SmbRelevance,
)


class PageReview(BaseModel):
    relevance: PageRelevance
    document_type: DocumentType
    content_sufficient: bool
    reason: str = Field(min_length=3, max_length=1000)
    regions: list[str] = Field(default_factory=list, max_length=20)
    industries: list[str] = Field(default_factory=list, max_length=30)


class DirectEvent(BaseModel):
    event_type: EventType
    event_status: EventStatus
    title: str = Field(min_length=3, max_length=500)
    summary: str = Field(min_length=10, max_length=3000)
    signal_date: date | None = None
    regions: list[str] = Field(default_factory=list, max_length=20)
    industry_objects: list[str] = Field(default_factory=list, max_length=30)
    topic_hint: str = Field(min_length=2, max_length=300)
    evidence: list[Annotated[str, Field(min_length=2, max_length=1000)]] = Field(
        min_length=1, max_length=20
    )


class DirectEventOutput(BaseModel):
    page_review: PageReview
    events: list[DirectEvent] = Field(default_factory=list)

    @field_validator("events")
    @classmethod
    def no_events_for_irrelevant_page(cls, value, info):
        review = info.data.get("page_review")
        if review is not None and review.relevance == PageRelevance.IRRELEVANT and value:
            raise ValueError("irrelevant pages must not produce events")
        if review is not None and not review.content_sufficient and value:
            raise ValueError("insufficient page content must not produce events")
        return value


class EventEntity(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    type: str = Field(min_length=1, max_length=80)


class EventEvidenceCoverage(BaseModel):
    subject: list[int] = Field(default_factory=list, max_length=20)
    action: list[int] = Field(default_factory=list, max_length=20)
    date: list[int] = Field(default_factory=list, max_length=20)
    numbers: list[int] = Field(default_factory=list, max_length=20)


class ExtractedEvent(BaseModel):
    event_type: EventType
    event_status: EventStatus = EventStatus.OBSERVED
    title: str = Field(min_length=3, max_length=500)
    summary: str = Field(min_length=10, max_length=3000)
    signal_date: date | None = None
    date_precision: DatePrecision
    regions: list[str] = Field(default_factory=list, max_length=20)
    industries: list[str] = Field(default_factory=list, max_length=30)
    industry_objects: list[str] = Field(default_factory=list, max_length=30)
    topic_hint: str = Field(default="未分类产业信号", min_length=2, max_length=300)
    entities: list[EventEntity] = Field(default_factory=list, max_length=30)
    chain_roles: list[str] = Field(default_factory=list, max_length=20)
    smb_relevance: SmbRelevance
    smb_reason: str = Field(min_length=3, max_length=1000)
    confidence: float = Field(ge=0, le=1)
    evidence: list[Annotated[str, Field(min_length=2, max_length=1000)]] = Field(
        min_length=1, max_length=20
    )
    evidence_coverage: EventEvidenceCoverage

    @model_validator(mode="after")
    def date_precision_matches_date(self) -> "ExtractedEvent":
        if self.signal_date is None:
            self.date_precision = DatePrecision.UNKNOWN
        if self.signal_date is not None and self.date_precision == DatePrecision.UNKNOWN:
            self.date_precision = DatePrecision.DAY
        upper_bound = len(self.evidence)
        self.evidence_coverage.subject = _valid_evidence_indexes(
            self.evidence_coverage.subject, upper_bound
        )
        self.evidence_coverage.action = _valid_evidence_indexes(
            self.evidence_coverage.action, upper_bound
        )
        self.evidence_coverage.date = _valid_evidence_indexes(
            self.evidence_coverage.date, upper_bound
        )
        self.evidence_coverage.numbers = _valid_evidence_indexes(
            self.evidence_coverage.numbers, upper_bound
        )
        return self


def _valid_evidence_indexes(indexes: list[int], upper_bound: int) -> list[int]:
    return list(dict.fromkeys(index for index in indexes if 0 <= index < upper_bound))


class PageEventOutput(BaseModel):
    page_review: PageReview
    events: list[ExtractedEvent] = Field(default_factory=list)

    @field_validator("events")
    @classmethod
    def no_events_for_irrelevant_page(cls, value, info):
        review = info.data.get("page_review")
        if review is not None and review.relevance == PageRelevance.IRRELEVANT and value:
            raise ValueError("irrelevant pages must not produce events")
        if review is not None and not review.content_sufficient and value:
            raise ValueError("insufficient page content must not produce events")
        return value


class Stage2Status(BaseModel):
    plan_id: str
    candidate_count: int
    enqueued_capture_count: int
    capture_status_counts: dict[str, int]
    acquisition_method_counts: dict[str, int]
    quality_status_counts: dict[str, int]
    analysis_status_counts: dict[str, int]
    relevance_counts: dict[str, int]
    event_count: int
    event_type_counts: dict[str, int]
    failure_counts: dict[str, int]
