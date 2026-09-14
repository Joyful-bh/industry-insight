import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, or_, select, update
from sqlalchemy.orm import Session

from track_insight.enums import JobStatus
from track_insight.models import Job


def build_idempotency_key(
    job_type: str,
    object_type: str,
    object_id: str,
    input_fingerprint: str,
    processor_version: str,
) -> str:
    value = "\x1f".join((job_type, object_type, object_id, input_fingerprint, processor_version))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def enqueue_job(
    session: Session,
    *,
    job_type: str,
    object_type: str,
    object_id: str,
    input_fingerprint: str,
    processor_version: str,
    priority: int = 100,
    max_attempts: int = 3,
    pipeline_run_id: Any = None,
    scheduled_at: datetime | None = None,
) -> Job:
    key = build_idempotency_key(
        job_type, object_type, object_id, input_fingerprint, processor_version
    )
    existing = session.scalar(select(Job).where(Job.idempotency_key == key))
    if existing is not None:
        return existing

    job = Job(
        job_type=job_type,
        object_type=object_type,
        object_id=object_id,
        idempotency_key=key,
        input_fingerprint=input_fingerprint,
        processor_version=processor_version,
        priority=priority,
        max_attempts=max_attempts,
        pipeline_run_id=pipeline_run_id,
        scheduled_at=scheduled_at or datetime.now(UTC),
    )
    session.add(job)
    session.flush()
    return job


def _claim_query(now: datetime, job_type: str | None = None) -> Select[tuple[Job]]:
    query = (
        select(Job)
        .where(
            Job.status.in_((JobStatus.PENDING, JobStatus.RETRY_WAIT)),
            Job.scheduled_at <= now,
            Job.attempts < Job.max_attempts,
        )
        .order_by(Job.priority.asc(), Job.scheduled_at.asc(), Job.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job_type is not None:
        query = query.where(Job.job_type == job_type)
    return query


def claim_job(
    session: Session,
    worker_id: str,
    lease_seconds: int = 300,
    job_type: str | None = None,
) -> Job | None:
    now = datetime.now(UTC)
    job = session.scalar(_claim_query(now, job_type))
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.lease_owner = worker_id
    job.lease_until = now + timedelta(seconds=lease_seconds)
    job.started_at = job.started_at or now
    job.attempts += 1
    session.flush()
    return job


def complete_job(session: Session, job: Job, output: dict[str, Any] | None = None) -> None:
    now = datetime.now(UTC)
    job.status = JobStatus.SUCCEEDED
    job.output = output
    job.finished_at = now
    job.lease_owner = None
    job.lease_until = None
    job.error_code = None
    job.error_message = None
    session.flush()


def fail_job(
    session: Session,
    job: Job,
    *,
    error_code: str,
    error_message: str,
    retry_delay_seconds: int = 60,
    retryable: bool = True,
) -> None:
    now = datetime.now(UTC)
    job.error_code = error_code
    job.error_message = error_message
    job.lease_owner = None
    job.lease_until = None
    if retryable and job.attempts < job.max_attempts:
        job.status = JobStatus.RETRY_WAIT
        job.scheduled_at = now + timedelta(seconds=retry_delay_seconds)
    else:
        job.status = JobStatus.FAILED
        job.finished_at = now
    session.flush()


def requeue_expired_jobs(session: Session, now: datetime | None = None) -> int:
    effective_now = now or datetime.now(UTC)
    result = session.execute(
        update(Job)
        .where(
            Job.status == JobStatus.RUNNING,
            or_(Job.lease_until.is_(None), Job.lease_until < effective_now),
        )
        .values(
            status=JobStatus.RETRY_WAIT,
            scheduled_at=effective_now,
            lease_owner=None,
            lease_until=None,
            error_code="lease_expired",
            error_message="Worker lease expired before completion",
        )
    )
    return int(result.rowcount or 0)
