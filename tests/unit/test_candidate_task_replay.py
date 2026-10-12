import pytest
from pydantic import ValidationError

from experiments.track_quality.candidate_task_replay import response_usage, schema_for


def test_failed_recovery_tokens_are_included():
    raw = {
        "initial_response": {"usage": {"prompt_tokens": 20, "completion_tokens": 5}},
        "recovery_response": {"usage": {"prompt_tokens": 30, "completion_tokens": 6}},
    }
    assert response_usage(raw) == {
        "prompt_tokens": 50, "completion_tokens": 11, "total_tokens": 0,
    }


def test_successful_recovery_tokens_are_included():
    raw = {
        "initial_response": {"usage": {"total_tokens": 25}},
        "repair_response": {"usage": {"total_tokens": 36}},
    }
    assert response_usage(raw)["total_tokens"] == 61


def test_replay_uses_production_topic_accounting_and_event_constraint():
    schema = schema_for({
        "topics": [{"topic_ref": "O1", "representative_events": [{"event_ref": "E1"}]}],
    })
    candidate = dict(
        name="产品制造", offering_family="产品", supplier_role="MANUFACTURE_PRODUCT",
        refs=["O1"], supporting_refs=[], core_event_refs=["E1"],
    )
    schema.model_validate({"tracks": [candidate], "excluded_refs": []})
    with pytest.raises(ValidationError, match="Account for all topic_ref"):
        schema.model_validate({"tracks": [], "excluded_refs": []})
    with pytest.raises(ValidationError, match="shared Topic requires distinct"):
        schema.model_validate({"tracks": [candidate, candidate], "excluded_refs": []})
