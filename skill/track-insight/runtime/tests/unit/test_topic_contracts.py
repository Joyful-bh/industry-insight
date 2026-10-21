import uuid

import pytest
from pydantic import ValidationError

from track_insight.topics.contracts import TopicGenerationOutput
from track_insight.topics.service import _sanitize_generation


def _candidate(event_id: uuid.UUID, candidate_key: str = "robot") -> dict:
    return {
        "candidate_key": candidate_key,
        "label": "具身智能机器人",
        "definition": "从事具身智能机器人整机研发、制造与应用的企业活动。",
        "summary": "近期事实显示具身智能机器人研发和产业化活动增加。",
        "event_ids": [str(event_id)],
        "confidence": 0.9,
    }


def test_generation_contract_uses_event_ids_as_single_source_of_truth() -> None:
    event_id = uuid.uuid4()
    output = TopicGenerationOutput.model_validate({"candidates": [_candidate(event_id)]})
    assert _sanitize_generation(output, {event_id}) == 0


def test_generation_contract_accepts_empty_result() -> None:
    output = TopicGenerationOutput.model_validate({"candidates": []})
    assert _sanitize_generation(output, {uuid.uuid4()}) == 0


def test_generation_sanitizer_drops_only_unknown_memberships() -> None:
    known = uuid.uuid4()
    unknown = uuid.uuid4()
    candidate = _candidate(known)
    candidate["event_ids"].append(str(unknown))
    output = TopicGenerationOutput.model_validate({"candidates": [candidate]})

    assert _sanitize_generation(output, {known}) == 1
    assert output.candidates[0].event_ids == [known]


def test_generation_sanitizer_drops_candidate_without_valid_event() -> None:
    output = TopicGenerationOutput.model_validate(
        {"candidates": [_candidate(uuid.uuid4())]}
    )
    assert _sanitize_generation(output, {uuid.uuid4()}) == 1
    assert output.candidates == []


def test_generation_contract_rejects_duplicate_candidate_keys() -> None:
    event_id = uuid.uuid4()
    candidate = _candidate(event_id, "same_key")
    with pytest.raises(ValidationError):
        TopicGenerationOutput.model_validate({"candidates": [candidate, candidate]})
