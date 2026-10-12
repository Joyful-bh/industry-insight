from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.run_track_full import best_effort, monitor_client, usage_total
from track_insight.core.fingerprints import fingerprint
from track_insight.tracks.service import _stored_build_prompt_version


def test_composite_prompt_version_fits_database_without_truncating_identity():
    version = "generation:" + "a" * 120
    assert _stored_build_prompt_version(version) == "build:" + fingerprint(version)
    assert len(_stored_build_prompt_version(version)) <= 100
    assert _stored_build_prompt_version(version) != _stored_build_prompt_version(version + "b")
    assert _stored_build_prompt_version("v1") == "v1"


def test_accounting_includes_failed_recovery():
    assert usage_total(
        {
            "initial_response": {"usage": {"prompt_tokens": 10, "total_tokens": 15}},
            "recovery_response": {"usage": {"prompt_tokens": 20, "total_tokens": 30}},
        }
    ) == {"prompt_tokens": 30, "completion_tokens": 0, "total_tokens": 45}


@pytest.mark.parametrize("monitoring_broken", [False, True])
def test_monitor_keeps_payload_and_only_increases_catalog_ceiling(monkeypatch, monitoring_broken):
    received = []

    class Client:
        def complete_structured(self, **kwargs):
            received.append(kwargs)
            return SimpleNamespace(raw_response={"usage": {"total_tokens": 1}}, call_count=1)

        def embed_texts(self, texts, *, model):
            return {"vectors": [[1.0]], "usage": {"total_tokens": 3}}

    names = (
        "track_generation",
        "track_definition",
        "track_support_recovery",
        "track_market_catalog",
        "track_market_audit",
    )
    prompts = {}
    for name in names:
        prompts[name] = SimpleNamespace(read_text=lambda encoding, name=name: name)
    config = SimpleNamespace(stage4=SimpleNamespace(prompts=SimpleNamespace(**prompts)))
    state = dict(
        structured_requests=0,
        embedding_calls_completed=0,
        embedding_tokens=0,
        structured_usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        completed_calls_by_task={},
    )
    active = Client()
    if monitoring_broken:

        def fail(*args, **kwargs):
            raise PermissionError("locked status/log")

        monkeypatch.setattr("scripts.run_track_full.append_call", fail)
        record = fail
    else:

        def record(**kwargs):
            pass

    monitor_client(active, config, MagicMock(), state, record)
    for task in ("track_generation", "track_market_catalog"):
        active.complete_structured(
            messages=[{"content": task}, {"content": "frozen"}], max_tokens=16384
        )
    assert received[0]["max_tokens"] == 16384
    assert received[1]["max_tokens"] == 65536
    assert received[1]["messages"][1]["content"] == "frozen"
    assert state["structured_requests"] == 2
    active.embed_texts(["text"], model="embedding")
    assert state["embedding_tokens"] == 3


def test_monitoring_failure_never_hides_original_model_error(monkeypatch):
    from track_insight.core.errors import ModelCallError

    error = ModelCallError("invalid model output", diagnostics={"response": {"usage": {}}})
    active = MagicMock()
    active.complete_structured.side_effect = error
    config = MagicMock()
    state = dict(
        structured_requests=0,
        structured_usage={key: 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
        completed_calls_by_task={},
    )

    def locked(*args, **kwargs):
        raise PermissionError("locked monitoring file")

    monkeypatch.setattr("scripts.run_track_full.save", locked)
    monkeypatch.setattr("scripts.run_track_full.append_call", locked)
    monitor_client(active, config, MagicMock(), state, locked)
    with pytest.raises(ModelCallError) as caught:
        active.complete_structured(messages=[{"content": "prompt"}], max_tokens=100)
    assert caught.value is error
    assert state["structured_requests"] == 1


def test_only_optional_files_tolerate_os_errors():
    from scripts.event_stage_supervisor import save

    action = MagicMock(side_effect=PermissionError("locked"))
    assert best_effort(action) is None
    path = MagicMock()
    path.with_name.return_value.replace.side_effect = PermissionError("locked export")
    with pytest.raises(PermissionError):
        save(path, {"tracks": []})
    action.side_effect = ValueError("programming error")
    with pytest.raises(ValueError):
        best_effort(action)
