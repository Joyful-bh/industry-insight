import uuid

from pydantic import BaseModel, Field, field_validator


class TopicCandidateOutput(BaseModel):
    candidate_key: str = Field(pattern=r"^[A-Za-z0-9_-]+$", min_length=3, max_length=100)
    label: str = Field(min_length=2, max_length=300)
    definition: str = Field(min_length=10, max_length=2000)
    summary: str = Field(min_length=10, max_length=3000)
    event_ids: list[uuid.UUID] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class TopicGenerationOutput(BaseModel):
    candidates: list[TopicCandidateOutput] = Field(default_factory=list)

    @field_validator("candidates")
    @classmethod
    def candidate_keys_are_unique(cls, value: list[TopicCandidateOutput]):
        keys = [candidate.candidate_key for candidate in value]
        if len(keys) != len(set(keys)):
            raise ValueError("candidate_key values must be unique")
        return value


class ReconciledTopic(BaseModel):
    canonical_key: str = Field(pattern=r"^[A-Za-z0-9_-]+$", min_length=3, max_length=100)
    label: str = Field(min_length=2, max_length=300)
    definition: str = Field(min_length=10, max_length=2000)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    keywords: list[str] = Field(default_factory=list, max_length=50)
    regions: list[str] = Field(default_factory=list, max_length=20)
    industries: list[str] = Field(default_factory=list, max_length=30)
    summary: str = Field(min_length=10, max_length=3000)
    source_candidate_keys: list[str] = Field(min_length=1, max_length=50)
    confidence: float = Field(ge=0, le=1)


class TopicReconciliationOutput(BaseModel):
    topics: list[ReconciledTopic] = Field(default_factory=list, max_length=50)

    @field_validator("topics")
    @classmethod
    def canonical_keys_are_unique(cls, value: list[ReconciledTopic]):
        keys = [topic.canonical_key for topic in value]
        if len(keys) != len(set(keys)):
            raise ValueError("canonical_key values must be unique")
        return value


class TopicStatus(BaseModel):
    plan_id: str
    run_id: str | None
    status: str | None
    event_count: int
    processed_event_count: int
    candidate_count: int
    topic_count: int
    assigned_event_count: int
    unassigned_event_count: int
    failed_job_count: int
