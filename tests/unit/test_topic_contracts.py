import uuid

import pytest
from pydantic import ValidationError

from track_insight.core.errors import ContractError
from track_insight.settings import load_poc_config
from track_insight.topics.contracts import TopicGenerationOutput
from track_insight.topics.service import _validate_generation


def _candidate(event_id: uuid.UUID, candidate_key: str = "robot_park") -> dict:
    return {
        "candidate_key": candidate_key,
        "label": "机器人产业园建设",
        "definition": "围绕机器人企业集聚形成的产业园建设与运营活动。",
        "summary": "相关事件反映机器人产业园建设和企业集聚信号。",
        "event_ids": [str(event_id)],
        "confidence": 0.9,
    }


def test_generation_contract_uses_event_ids_as_single_source_of_truth() -> None:
    event_id = uuid.uuid4()
    output = TopicGenerationOutput.model_validate({"candidates": [_candidate(event_id)]})
    _validate_generation(output, {event_id}, load_poc_config())


def test_generation_contract_accepts_unassigned_events_implicitly() -> None:
    event_id = uuid.uuid4()
    output = TopicGenerationOutput.model_validate({"candidates": []})
    _validate_generation(output, {event_id}, load_poc_config())


def test_generation_validation_rejects_unknown_event_membership() -> None:
    input_event_id = uuid.uuid4()
    output = TopicGenerationOutput.model_validate(
        {"candidates": [_candidate(uuid.uuid4())]}
    )
    with pytest.raises(ContractError, match="unknown event"):
        _validate_generation(output, {input_event_id}, load_poc_config())


def test_generation_contract_rejects_duplicate_candidate_keys() -> None:
    event_id = uuid.uuid4()
    candidate = _candidate(event_id, "same_key")
    with pytest.raises(ValidationError):
        TopicGenerationOutput.model_validate({"candidates": [candidate, candidate]})
