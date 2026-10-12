import uuid

from pydantic import BaseModel, Field, field_validator

from track_insight.core.errors import ContractError
from track_insight.core.fingerprints import fingerprint


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


class CompactTopicCandidateOutput(BaseModel):
    label: str = Field(min_length=2, max_length=300)
    definition: str = Field(min_length=10, max_length=2000)
    summary: str = Field(min_length=10, max_length=3000)
    event_refs: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class CompactTopicGenerationOutput(BaseModel):
    candidates: list[CompactTopicCandidateOutput] = Field(default_factory=list)


def expand_topic_output(
    output: CompactTopicGenerationOutput, event_refs: dict[str, uuid.UUID]
) -> TopicGenerationOutput:
    candidates = []
    for index, candidate in enumerate(output.candidates):
        if not set(candidate.event_refs).issubset(event_refs):
            raise ContractError("Topic output references an unknown short Event reference")
        ids = list(dict.fromkeys(event_refs[ref] for ref in candidate.event_refs))
        key_hash = fingerprint(candidate.label, sorted(map(str, ids)))[:12]
        candidates.append(
            TopicCandidateOutput(
                candidate_key=f"topic_{index}_{key_hash}",
                label=candidate.label,
                definition=candidate.definition,
                summary=candidate.summary,
                event_ids=ids,
                confidence=candidate.confidence,
            )
        )
    return TopicGenerationOutput(candidates=candidates)


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
