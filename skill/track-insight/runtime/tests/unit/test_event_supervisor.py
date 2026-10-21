import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from scripts.event_stage_supervisor import active_jobs, choose_sample, reconcile_page_failures, save
from track_insight.events.service import _enqueue_current_analysis_jobs
from track_insight.infrastructure.jobs import claim_job, requeue_expired_jobs
from track_insight.infrastructure.models import Base, Job, PageCapture
from track_insight.settings import load_poc_config


@pytest.mark.parametrize("always_locked", [False, True])
def test_atomic_save_retries_windows_lock_and_cleans_own_temporary(monkeypatch, always_locked):
    path = MagicMock()
    path.name = "status.json"
    temporary = path.with_name.return_value
    temporary.replace.side_effect = (
        PermissionError("sharing violation") if always_locked else [PermissionError("busy"), None]
    )
    sleeps = []
    monkeypatch.setattr("scripts.event_stage_supervisor.time.sleep", sleeps.append)
    if always_locked:
        with pytest.raises(PermissionError):
            save(path, {"status": "running"})
        assert temporary.replace.call_count == 4
        assert sleeps == [0.1, 0.2, 0.4]
    else:
        save(path, {"status": "running"})
        assert temporary.replace.call_count == 2
        assert sleeps == [0.1]
    temporary.write_text.assert_called_once()
    temporary.unlink.assert_called_once_with(missing_ok=True)
    first_name = path.with_name.call_args.args[0]
    temporary.replace.side_effect = None
    save(path, {})
    assert path.with_name.call_args.args[0] != first_name


def test_exhausted_leases_end_and_unexhausted_leases_remain_retryable():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as session:
        jobs = [
            Job(
                job_type="page_acquire",
                object_type="page_capture",
                object_id=str(uuid.uuid4()),
                idempotency_key=str(n),
                input_fingerprint="a" * 64,
                processor_version="v1",
                status=status,
                attempts=attempts,
                max_attempts=3,
                lease_until=lease,
                lease_owner="worker",
            )
            for n, (status, attempts, lease) in enumerate(
                [
                    ("running", 3, now - timedelta(seconds=1)),
                    ("failed_retryable", 3, None),
                    ("running", 2, now - timedelta(seconds=1)),
                    ("running", 3, now + timedelta(minutes=5)),
                ]
            )
        ]
        session.add_all(jobs)
        session.flush()
        assert requeue_expired_jobs(session, now) == 3
        for job in jobs:
            session.refresh(job)
        assert [j.status for j in jobs] == [
            "failed_terminal",
            "failed_terminal",
            "failed_retryable",
            "running",
        ]
        assert all(j.finished_at is not None and j.lease_owner is None for j in jobs[:2]), [
            (j.finished_at, j.lease_owner) for j in jobs[:2]
        ]
        assert active_jobs(jobs) == jobs[2:]
        assert requeue_expired_jobs(session, now) == 0
        claimed = claim_job(session, worker_id="new", lease_seconds=60, job_type="page_acquire")
        assert claimed.id == jobs[2].id
    engine.dispose()


def test_supervisor_ends_stale_capture_without_touching_success_or_live_work():
    captures = [
        PageCapture(id=uuid.uuid4(), status=status)
        for status in ("running", "completed", "running")
    ]
    jobs = [
        Job(
            object_id=str(c.id),
            status="failed_terminal",
            attempts=3,
            max_attempts=3,
            finished_at=datetime.now(UTC),
            error_code="lease_expired",
            error_message="expired",
        )
        for c in captures
    ]
    live = Job(object_id=str(captures[2].id), status="running", attempts=3, max_attempts=3)
    exhausted = Job(status="failed_retryable", attempts=3, max_attempts=3)
    assert active_jobs([exhausted]) == []
    reconcile_page_failures(captures, [*jobs, live])
    assert [c.status for c in captures] == ["failed_terminal", "completed", "running"]
    assert captures[0].error_code == "lease_expired"


def test_sample_is_reproducible_and_covers_regions():
    rows = [
        {"id": f"{r}-{n}", "region": str(r), "method": "http_html"}
        for r in range(32)
        for n in range(5)
    ]
    sample = choose_sample(rows)
    assert len(sample) == 40
    assert len({r["id"] for r in sample}) == 40
    assert len({r["region"] for r in sample}) == 32
    assert sample == choose_sample(rows)


def test_claim_empty_scope_never_consumes_global_jobs():
    assert (
        claim_job(None, worker_id="test", lease_seconds=60, job_type="page_analyze", object_ids=[])
        is None
    )
    assert (
        claim_job(None, worker_id="test", lease_seconds=60, job_type="page_analyze", job_ids=[])
        is None
    )


def test_scoped_enqueue_and_claim_leave_other_pages_untouched():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        captures = [
            PageCapture(
                url_candidate_id=uuid.uuid4(),
                requested_url=f"https://example.com/{n}",
                status="completed",
                quality_status="usable",
                content_hash=str(n),
            )
            for n in range(2)
        ]
        session.add_all(captures)
        session.commit()
        ids = _enqueue_current_analysis_jobs(
            session, load_poc_config(), capture_ids=[captures[0].id]
        )
        jobs = list(session.scalars(select(Job)))
        assert len(jobs) == 1
        assert jobs[0].object_id == str(captures[0].id)
        assert (
            claim_job(
                session,
                worker_id="test",
                lease_seconds=60,
                job_type="page_analyze",
                object_ids=[str(captures[1].id)],
                job_ids=ids,
            )
            is None
        )
        assert (
            claim_job(
                session,
                worker_id="test",
                lease_seconds=60,
                job_type="page_analyze",
                object_ids=[str(captures[0].id)],
                job_ids=ids,
            ).id
            == ids[0]
        )
    engine.dispose()
