from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.supervise_track_full import supervise, trigger_codex


@pytest.mark.parametrize("fixed", [True, False])
def test_supervisor_invokes_codex_once_after_failure_then_resumes_or_blocks(monkeypatch, fixed):
    states, launches, incidents = [], [], []
    monkeypatch.setattr(Path, "exists", lambda self: False)
    monkeypatch.setattr("scripts.supervise_track_full.read_status", lambda p: {"status": "failed"})
    monkeypatch.setattr("scripts.supervise_track_full.worker_completed", lambda code, p: code == 0)

    def launch(plan, directory, record):
        launches.append(directory.name)
        return 1 if len(launches) == 1 else 0

    def repair(codex, directory, incident):
        incidents.append(incident)
        return {"fixed": fixed, "summary": "test"}

    supervise(
        "plan",
        Path("runs"),
        4,
        "codex",
        lambda **kwargs: states.append(kwargs),
        launch=launch,
        repair=repair,
    )
    assert len(incidents) == 1
    assert launches == (["run_4", "run_5"] if fixed else ["run_4"])
    assert states[-1]["status"] == ("completed" if fixed else "blocked")


def test_success_never_invokes_a_model(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: False)
    monkeypatch.setattr(
        "scripts.supervise_track_full.read_status", lambda p: {"status": "completed"}
    )
    monkeypatch.setattr("scripts.supervise_track_full.worker_completed", lambda code, p: True)
    repair = MagicMock()
    supervise("plan", Path("runs"), 4, "codex", MagicMock(), launch=lambda *args: 0, repair=repair)
    repair.assert_not_called()


def test_trigger_does_not_bypass_sandbox_and_uses_workspace_approval_review(monkeypatch):
    directory = MagicMock()
    result = directory.__truediv__.return_value
    result.read_text.return_value = '{"fixed": true, "summary": "test"}'
    monkeypatch.setattr("scripts.supervise_track_full.save", lambda *args: None)
    run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr("scripts.supervise_track_full.subprocess.run", run)
    trigger_codex("codex", directory, {"returncode": 1})
    command = run.call_args.args[0]
    assert "--approve-for-me" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "不要启动worker" in run.call_args.kwargs["input"]
