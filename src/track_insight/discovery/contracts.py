from pydantic import BaseModel, Field, field_validator

from track_insight.core.enums import SearchDecision, SourceClass, UrlType


class SearchJudgment(BaseModel):
    refer: str = Field(min_length=1, max_length=200)
    decision: SearchDecision
    url_type: UrlType
    source_class: SourceClass
    reason: str = Field(min_length=3, max_length=500)


class SearchReviewOutput(BaseModel):
    judgments: list[SearchJudgment]

    @field_validator("judgments")
    @classmethod
    def unique_references(cls, value: list[SearchJudgment]) -> list[SearchJudgment]:
        references = [item.refer for item in value]
        if len(references) != len(set(references)):
            raise ValueError("judgment references must be unique")
        return value


class CoverageAudit(BaseModel):
    plan_id: str
    work_package_count: int
    completed_work_package_count: int
    search_task_count: int
    completed_search_task_count: int
    raw_result_count: int
    retained_result_count: int
    candidate_count: int
    distinct_domain_count: int
    source_class_counts: dict[str, int]
    dominant_domains: list[dict[str, int | str]]
    missing_source_classes: list[str]
    warnings: list[str]


class SearchRunSummary(BaseModel):
    plan_id: str
    processed_searches: int
    skipped_searches: int
    failed_searches: int
    audit: CoverageAudit
