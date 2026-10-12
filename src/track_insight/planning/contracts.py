from datetime import date

from pydantic import BaseModel, Field, field_validator, model_validator

from track_insight.core.enums import SourceClass
from track_insight.core.errors import ContractError


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


class CompactSearchTaskProposal(BaseModel):
    query: str = Field(min_length=2, max_length=70)
    purpose: str = Field(min_length=5, max_length=500)
    target_industries: list[str] = Field(min_length=1)
    target_signal_types: list[str] = Field(min_length=1)
    target_source_classes: list[SourceClass] = Field(min_length=1)


class CompactWorkPackageProposal(BaseModel):
    region_ref: str = Field(min_length=1)
    search_tasks: list[CompactSearchTaskProposal] = Field(min_length=1)


class CompactPlanningOutput(BaseModel):
    work_packages: list[CompactWorkPackageProposal] = Field(min_length=1)


def expand_planning_output(
    output: CompactPlanningOutput, scope: ResearchScope, region_refs: dict[str, str]
) -> PlanningOutput:
    if len({p.region_ref for p in output.work_packages}) != len(output.work_packages):
        raise ContractError("planning output repeated a region reference")
    packages = []
    for package in output.work_packages:
        if package.region_ref not in region_refs:
            raise ContractError("planning output references an unknown region")
        region = region_refs[package.region_ref]
        if region not in scope.regions:
            raise ContractError("planning region reference is outside the assigned scope")
        tasks = [
            SearchTaskProposal(**task.model_dump(), target_regions=[region])
            for task in package.search_tasks
        ]
        packages.append(
            WorkPackageProposal(
                objective=f"调查{region}的{'、'.join(scope.industry_scopes)}政策与产业信号",
                regions=[region],
                industry_scopes=scope.industry_scopes,
                signal_types=list(
                    dict.fromkeys(s for task in tasks for s in task.target_signal_types)
                ),
                source_classes=list(
                    dict.fromkeys(s for task in tasks for s in task.target_source_classes)
                ),
                search_tasks=tasks,
            )
        )
    return PlanningOutput(work_packages=packages)


class PlanValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    work_package_count: int
    search_task_count: int
    covered_source_classes: list[str]
