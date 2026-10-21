import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from track_insight.core.errors import ModelCallError
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.infrastructure.models import Base, ModelRun, PipelineRun
from track_insight.settings import load_poc_config
from track_insight.tracks.contracts import CandidateTrackOutput, TrackBuildOutput
from track_insight.tracks.markets import MarketCatalog, MarketDecisions, market_output_model
from track_insight.tracks.service import TrackService, _definition_output_model
from track_insight.tracks.support import support_recovery_model


@pytest.mark.parametrize("kind", ["support", "definition", "catalog", "audit"])
def test_reference_errors_are_repaired_inside_client_and_raw_responses_retained(monkeypatch, kind):
    text = "从事显示面板研发、生产和交付的制造企业。"
    market = dict(
        market_ref="M1",
        name="显示面板制造",
        offering_family="显示面板",
        supplier_role="MANUFACTURE_PRODUCT",
        boundary=text,
        member_refs=["C0", "C1"],
    )
    options = {
        "support": (
            support_recovery_model({"T1"}, {"R0"}),
            {"assignments": [], "excluded_refs": []},
            {"assignments": [], "excluded_refs": ["T1"]},
        ),
        "definition": (
            _definition_output_model({"R0"}),
            {"definitions": []},
            {"definitions": [{"track_ref": "R0", "definition": text}]},
        ),
        "catalog": (
            market_output_model(
                "catalog", {"tracks": [{"track_ref": "C0"}, {"track_ref": "C1"}]}, MarketCatalog
            ),
            {"markets": [{**market, "member_refs": ["C0", "C99"]}]},
            {"markets": [market]},
        ),
        "audit": (
            market_output_model("audit", {"groups": [{"group_ref": "M1"}]}, MarketDecisions),
            {"decisions": []},
            {"decisions": [{"group_ref": "M1", "decision": "REJECT"}]},
        ),
    }
    schema, invalid, valid = options[kind]
    client = BailianClient(api_key="test", base_url="https://example.invalid/v1")
    requests = []

    def post(endpoint, payload, *, operation):
        requests.append(payload)
        output = invalid if len(requests) == 1 else valid
        return {
            "id": "test",
            "model": "test",
            "usage": {},
            "choices": [{"message": {"content": json.dumps(output)}}],
        }

    monkeypatch.setattr(client, "_post_compatible", post)
    try:
        result = client.complete_structured(
            messages=[{"role": "user", "content": "source"}], output_model=schema, model="test"
        )
        assert result.recovery_used and result.call_count == 2
        assert (
            "initial_response" in result.raw_response and "repair_response" in result.raw_response
        )
        assert (
            "missing=" in requests[1]["messages"][-1]["content"]
            or "unknown=" in requests[1]["messages"][-1]["content"]
        )
    finally:
        client.close()


def test_cached_stage_reuses_valid_output_and_failure_keeps_original_diagnostics():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    config = load_poc_config()
    calls = []

    class Client:
        def complete_structured(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise ModelCallError(
                    "still invalid",
                    code="invalid_model_output_after_recovery",
                    diagnostics={
                        "initial_response": {"id": "failure", "usage": {"total_tokens": 7}},
                        "recovery_response": {"choices": []},
                    },
                )
            return SimpleNamespace(
                output=kwargs["output_model"].model_validate(
                    {"assignments": [], "excluded_refs": ["T1"]}
                ),
                request_id="test",
                usage={},
                raw_response={},
            )

    with Session(engine) as session:
        pipeline = PipelineRun(run_type="topic_to_track", status="running")
        session.add(pipeline)
        session.commit()
        service = TrackService(client=Client(), config=config)
        args = (
            session,
            pipeline,
            "track_support_recovery",
            "prompt",
            "v1",
            {"batch": 1},
            support_recovery_model({"T1"}, {"R0"}),
            4096,
        )
        service._complete_cached_track_task(*args)
        service._complete_cached_track_task(*args)
        assert len(calls) == 1
        with pytest.raises(ModelCallError):
            service._complete_cached_track_task(*args[:5], {"batch": 2}, *args[6:])
        failed = session.scalar(select(ModelRun).where(ModelRun.status == "failed_terminal"))
        assert failed.raw_response["initial_response"]["id"] == "failure"
        assert failed.raw_response["initial_response"]["usage"]["total_tokens"] == 7
    engine.dispose()


@pytest.mark.parametrize(
    "code, must_stop", [("invalid_model_output_after_recovery", False), ("http_error", True)]
)
def test_only_invalid_optional_support_can_degrade(monkeypatch, code, must_stop):
    service = TrackService(client=None, config=load_poc_config())
    tid, pending_id = uuid.uuid4(), uuid.uuid4()
    output = TrackBuildOutput(
        tracks=[
            CandidateTrackOutput(
                name="机器人制造",
                offering_family="机器人",
                supplier_role="MANUFACTURE_PRODUCT",
                definition="从事机器人研发生产的制造企业。",
                topic_ids=[tid],
                status="candidate",
                confidence=0.8,
            )
        ]
    )
    topics = [SimpleNamespace(id=tid), SimpleNamespace(id=pending_id)]
    monkeypatch.setattr("track_insight.tracks.service._topic_payload", lambda *args, **kwargs: {})
    events = []
    monkeypatch.setattr(
        "track_insight.tracks.service.record_event", lambda *args, **kwargs: events.append(kwargs)
    )

    def fail(*args, **kwargs):
        raise ModelCallError("invalid", code=code)

    monkeypatch.setattr(service, "_complete_cached_track_task", fail)
    session = SimpleNamespace(commit=lambda: None)
    args = (session, SimpleNamespace(id=uuid.uuid4()), output, topics, {}, "prompt", "v1")
    if must_stop:
        with pytest.raises(ModelCallError):
            service._recover_supporting_topics(*args)
    else:
        stats = service._recover_supporting_topics(*args)
        assert stats["skipped_batches"] == 1
        assert events[0]["event_type"] == "track.support_recovery.skipped"
    assert output.tracks[0].supporting_topic_ids == []
