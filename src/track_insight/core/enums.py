from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"


class SearchDecision(StrEnum):
    KEEP = "keep"
    MAYBE = "maybe"
    DROP = "drop"


class UrlType(StrEnum):
    CONTENT_PAGE = "content_page"
    SOURCE_ENTRY = "source_entry"


class SourceClass(StrEnum):
    GOVERNMENT = "government"
    PARK = "park"
    ASSOCIATION = "association"
    COMPANY = "company"
    INDUSTRY_MEDIA = "industry_media"
    AUTHORITATIVE_MEDIA = "authoritative_media"
    INVESTMENT_INSTITUTION = "investment_institution"
    OTHER = "other"
