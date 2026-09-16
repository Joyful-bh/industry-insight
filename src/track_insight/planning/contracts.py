from datetime import date

from pydantic import BaseModel, Field, field_validator, model_validator

from track_insight.core.enums import SourceClass


class ResearchScope(BaseModel):
    regions: list[str] = Field(min_length=1)
    industry_scopes: list[str] = Field(min_length=1)
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_dates(self) -> "ResearchScope":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be earlier than start_date")
        return self


class SearchTaskProposal(BaseModel):
    query: str = Field(min_length=2, max_length=70)
    purpose: str = Field(min_length=5, max_length=500)
    target_regions: list[str] = Field(min_length=1)
    target_industries: list[str] = Field(min_length=1)
    target_signal_types: list[str] = Field(min_length=1)
    target_source_classes: list[SourceClass] = Field(min_length=1)


class WorkPackageProposal(BaseModel):
    objective: str = Field(min_length=5, max_length=500)
    regions: list[str] = Field(min_length=1)
    industry_scopes: list[str] = Field(min_length=1)
    signal_types: list[str] = Field(min_length=1)
    source_classes: list[SourceClass] = Field(min_length=1)
    search_tasks: list[SearchTaskProposal] = Field(min_length=1)

    @field_validator("search_tasks")
    @classmethod
    def unique_queries(cls, value: list[SearchTaskProposal]) -> list[SearchTaskProposal]:
        normalized = [task.query.strip().casefold() for task in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("search task queries must be unique within a work package")
        return value


class PlanningOutput(BaseModel):
    work_packages: list[WorkPackageProposal] = Field(min_length=1)


class PlanValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    work_package_count: int
    search_task_count: int
    covered_source_classes: list[str]
