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


class PageRelevance(StrEnum):
    RELEVANT = "relevant"
    POSSIBLY_RELEVANT = "possibly_relevant"
    IRRELEVANT = "irrelevant"


class DocumentType(StrEnum):
    POLICY = "policy"
    APPLICATION_OR_FUNDING = "application_or_funding"
    RECOGNITION_OR_LIST = "recognition_or_list"
    PROJECT = "project"
    ENTERPRISE_NEWS = "enterprise_news"
    INVESTMENT_FINANCING = "investment_financing"
    MARKET_REPORT = "market_report"
    PARK_OR_CLUSTER = "park_or_cluster"
    INDUSTRY_REPORT = "industry_report"
    OTHER = "other"


class EventType(StrEnum):
    POLICY_RELEASE = "policy_release"
    APPLICATION_OR_FUNDING = "application_or_funding"
    RECOGNITION_OR_LIST = "recognition_or_list"
    PROJECT_PROGRESS = "project_progress"
    ENTERPRISE_OPERATION = "enterprise_operation"
    INVESTMENT_FINANCING = "investment_financing"
    TECHNOLOGY_COMMERCIALIZATION = "technology_commercialization"
    MARKET_CHANGE = "market_change"
    PARK_OR_CLUSTER = "park_or_cluster"
    OTHER_INDUSTRY_SIGNAL = "other_industry_signal"


class EventStatus(StrEnum):
    PUBLISHED = "published"
    SUPPORTED = "supported"
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    OBSERVED = "observed"


class DatePrecision(StrEnum):
    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    UNKNOWN = "unknown"


class SmbRelevance(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNCLEAR = "unclear"
