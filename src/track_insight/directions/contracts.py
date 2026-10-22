from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class DirectionItem(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    definition: str = Field(min_length=8, max_length=500)
    topic_refs: list[str] = Field(min_length=1)


class DirectionGroupingOutput(BaseModel):
    directions: list[DirectionItem] = Field(default_factory=list)


class DirectionMergeGroup(BaseModel):
    direction_refs: list[str] = Field(min_length=2)
    name: str = Field(min_length=2, max_length=200)
    definition: str = Field(min_length=8, max_length=500)

    @model_validator(mode="after")
    def direction_refs_are_unique(self) -> DirectionMergeGroup:
        if len(self.direction_refs) != len(set(self.direction_refs)):
            raise ValueError("merge group direction_refs must be unique")
        return self


class DirectionDedupOutput(BaseModel):
    merge_groups: list[DirectionMergeGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def direction_refs_are_not_reused(self) -> DirectionDedupOutput:
        refs = [ref for group in self.merge_groups for ref in group.direction_refs]
        if len(refs) != len(set(refs)):
            raise ValueError("one direction_ref cannot appear in multiple merge groups")
        return self


class DirectionStatus(BaseModel):
    plan_id: str
    run_id: str | None
    status: str | None
    topic_count: int
    direction_count: int
    assigned_topic_count: int
    discarded_topic_count: int
    group_count: int
