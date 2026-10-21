from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from track_insight.core.enums import TaskStatus
from track_insight.infrastructure.jobs import (
    claim_job,
    enqueue_job,
    reconcile_stale_pipeline_runs,
    requeue_expired_jobs,
)
from track_insight.infrastructure.models import Base, PipelineRun


def test_expired_job_can_be_reclaimed() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        job = enqueue_job(
            session,
            job_type="search",
            object_type="search_task",
            object_id="task-1",
            input_fingerprint="a" * 64,
            processor_version="v1",
        )
        claimed = claim_job(session, worker_id="worker-1", lease_seconds=30, job_type="search")
        assert claimed is not None
        job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        assert requeue_expired_jobs(session) == 1
        assert job.status == TaskStatus.FAILED_RETRYABLE
        reclaimed = claim_job(session, worker_id="worker-2", lease_seconds=30, job_type="search")
        assert reclaimed is not None
        assert reclaimed.id == job.id


def test_abandoned_pipeline_is_closed_but_live_leased_pipeline_is_not() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        stale = PipelineRun(
            run_type="test",
            status=TaskStatus.RUNNING,
            started_at=now - timedelta(hours=2),
        )
        live = PipelineRun(
            run_type="test",
            status=TaskStatus.RUNNING,
            started_at=now - timedelta(hours=2),
        )
        session.add_all([stale, live])
        session.flush()
        job = enqueue_job(
            session,
            job_type="test",
            object_type="test",
            object_id="live",
            input_fingerprint="b" * 64,
            processor_version="v1",
        )
        job.pipeline_run_id = live.id
        job.status = TaskStatus.RUNNING
        job.lease_until = now + timedelta(minutes=5)

        assert reconcile_stale_pipeline_runs(
            session, now=now, stale_after_seconds=1800
        ) == 1
        session.refresh(stale)
        session.refresh(live)
        assert stale.status == TaskStatus.FAILED_RETRYABLE
        assert stale.finished_at is not None
        assert live.status == TaskStatus.RUNNING
