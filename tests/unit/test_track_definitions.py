import json
import uuid
from types import SimpleNamespace

import pytest

from track_insight.core.errors import ContractError
from track_insight.settings import load_poc_config
from track_insight.tracks.contracts import IdentifiedTrackBuildOutput, TrackDefinitionOutput
from track_insight.tracks.service import TrackService, _apply_definitions, _track_scope_payload


def test_definitions_replace_the_old_placeholder():
    track = SimpleNamespace(definition="旧模板", topic_ids=[uuid.uuid4()])
    text = "从事有机发光显示面板的研发与制造，包括OLED与AMOLED面板，区别于显示终端整机制造企业。"
    output = TrackDefinitionOutput.model_validate(
        {"definitions": [{"track_ref": "R1", "definition": text}]}
    )
    _apply_definitions({"R1": track}, output)
    assert track.definition == text


@pytest.mark.parametrize("returned", [["R1", "R1"], ["R2"], []])
def test_incomplete_or_duplicate_references_do_not_modify_tracks(returned):
    track = SimpleNamespace(definition="原定义")
    output = TrackDefinitionOutput.model_validate(
        {
            "definitions": [
                {
                    "track_ref": ref,
                    "definition": "从事明确产品的研发制造活动，属于设备制造企业集合。",
                }
                for ref in returned
            ]
        }
    )
    with pytest.raises(ContractError):
        _apply_definitions({"R1": track}, output)
    assert track.definition == "原定义"


def test_generic_definition_cannot_be_published():
    track = SimpleNamespace(definition="原定义")
    output = TrackDefinitionOutput.model_validate(
        {
            "definitions": [
                {
                    "track_ref": "R1",
                    "definition": "围绕算力开展相关产品、技术或服务研发、生产与交付的企业集合。",
                }
            ]
        }
    )
    with pytest.raises(ContractError):
        _apply_definitions({"R1": track}, output)
    assert track.definition == "原定义"


def test_event_capacity_can_exceed_one_thousand_without_enlarging_batches():
    config = load_poc_config()
    assert config.stage3.max_events_per_run == 10_000
    assert config.stage3.event_batch_size == 20


def test_track_scope_uses_plan_industries_and_configured_boundary():
    config = load_poc_config()
    plan = SimpleNamespace(industry_scopes=["制造业"])

    scope = _track_scope_payload(plan, config)

    assert scope["industry_scopes"] == ["制造业"]
    assert "制造业企业本身" in scope["track_boundary"]
    assert "一般法律" in scope["track_boundary"]


def test_track_generation_payload_contains_scope(monkeypatch):
    captured = {}

    class Client:
        def complete_structured(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                output=IdentifiedTrackBuildOutput.model_validate(
                    {"tracks": [], "excluded_refs": ["T1"]}
                )
            )

    topic_id = uuid.uuid4()
    topic = SimpleNamespace(id=topic_id)
    monkeypatch.setattr(
        "track_insight.tracks.service._topic_payload",
        lambda _session, _topic, topic_ref=None: {"topic_ref": topic_ref},
    )
    service = TrackService(client=Client(), config=load_poc_config())
    scope = {"industry_scopes": ["制造业"], "track_boundary": "只保留制造业企业。"}

    service._call_track_model(
        session=object(),
        prompt="prompt",
        topics=[topic],
        topic_refs={"T1": topic_id},
        scope=scope,
    )

    payload = json.loads(captured["messages"][1]["content"])
    assert payload["scope"] == scope
    assert payload["topics"] == [{"topic_ref": "T1"}]
