from enum import StrEnum


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class SourceObservationStatus(StrEnum):
    HEALTHY_NO_NEW = "healthy_no_new"
    HEALTHY_WITH_NEW = "healthy_with_new"
    COLLECTION_FAILED = "collection_failed"
    PARSE_FAILED = "parse_failed"
    LATE_DISCOVERY = "late_discovery"
    SOURCE_CHANGED = "source_changed"
    DISABLED = "disabled"


class ParseStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class RelevanceLabel(StrEnum):
    RELEVANT = "relevant"
    POSSIBLY_RELEVANT = "possibly_relevant"
    IRRELEVANT = "irrelevant"
