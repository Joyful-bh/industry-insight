from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from track_insight.core.enums import TaskStatus
from track_insight.infrastructure.jobs import claim_job, enqueue_job, requeue_expired_jobs
from track_insight.infrastructure.models import Base


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
