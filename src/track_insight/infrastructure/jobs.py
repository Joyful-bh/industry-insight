import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from track_insight.core.enums import TaskStatus
from track_insight.core.fingerprints import fingerprint
from track_insight.infrastructure.models import Job


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
) -> Job:
    key = fingerprint(
        {
            "job_type": job_type,
            "object_type": object_type,
            "object_id": object_id,
            "input_fingerprint": input_fingerprint,
        },
        processor_version,
    )
    existing = session.scalar(select(Job).where(Job.idempotency_key == key))
    if existing is not None:
        return existing
    job_id = uuid.uuid4()
    inserted_id = session.scalar(
        insert(Job)
        .values(
            id=job_id,
            job_type=job_type,
            object_type=object_type,
            object_id=object_id,
            idempotency_key=key,
            input_fingerprint=input_fingerprint,
            processor_version=processor_version,
            status=TaskStatus.PENDING,
            priority=priority,
            attempts=0,
            max_attempts=max_attempts,
        )
        .on_conflict_do_nothing(index_elements=[Job.idempotency_key])
        .returning(Job.id)
    )
    if inserted_id is not None:
        return session.get(Job, inserted_id)
    return session.scalar(select(Job).where(Job.idempotency_key == key))


def claim_job(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: int,
    job_type: str,
    processor_version: str | None = None,
) -> Job | None:
    now = datetime.now(UTC)
    conditions = [
        Job.job_type == job_type,
        Job.status.in_((TaskStatus.PENDING, TaskStatus.FAILED_RETRYABLE)),
        Job.scheduled_at <= now,
        Job.attempts < Job.max_attempts,
    ]
    if processor_version is not None:
        conditions.append(Job.processor_version == processor_version)
    job = session.scalar(
        select(Job)
        .where(*conditions)
        .order_by(Job.priority, Job.scheduled_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = TaskStatus.RUNNING
    job.lease_owner = worker_id
    job.lease_until = now + timedelta(seconds=lease_seconds)
    job.started_at = job.started_at or now
    job.attempts += 1
    session.flush()
    return job


def claim_job_for_object(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: int,
    job_type: str,
    object_type: str,
    object_id: str,
) -> Job | None:
    now = datetime.now(UTC)
    job = session.scalar(
        select(Job)
        .where(
            Job.job_type == job_type,
            Job.object_type == object_type,
            Job.object_id == object_id,
            Job.status.in_((TaskStatus.PENDING, TaskStatus.FAILED_RETRYABLE)),
            Job.scheduled_at <= now,
            Job.attempts < Job.max_attempts,
        )
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = TaskStatus.RUNNING
    job.lease_owner = worker_id
    job.lease_until = now + timedelta(seconds=lease_seconds)
    job.started_at = job.started_at or now
    job.attempts += 1
    session.flush()
    return job


def complete_job(session: Session, job: Job, output: dict | None = None) -> None:
    job.status = TaskStatus.COMPLETED
    job.output = output
    job.finished_at = datetime.now(UTC)
    job.lease_owner = None
    job.lease_until = None
    session.flush()


def fail_job(
    session: Session,
    job: Job,
    *,
    error_code: str,
    error_message: str,
    retryable: bool,
    retry_delay_seconds: int = 60,
) -> None:
    now = datetime.now(UTC)
    can_retry = retryable and job.attempts < job.max_attempts
    job.status = TaskStatus.FAILED_RETRYABLE if can_retry else TaskStatus.FAILED_TERMINAL
    job.scheduled_at = now + timedelta(seconds=retry_delay_seconds) if can_retry else now
    job.finished_at = None if can_retry else now
    job.lease_owner = None
    job.lease_until = None
    job.error_code = error_code
    job.error_message = error_message
    session.flush()


def requeue_expired_jobs(session: Session, now: datetime | None = None) -> int:
    effective_now = now or datetime.now(UTC)
    result = session.execute(
        update(Job)
        .where(
            Job.status == TaskStatus.RUNNING,
            or_(Job.lease_until.is_(None), Job.lease_until < effective_now),
        )
        .values(
            status=TaskStatus.FAILED_RETRYABLE,
            scheduled_at=effective_now,
            lease_owner=None,
            lease_until=None,
            error_code="lease_expired",
            error_message="Worker lease expired before completion",
        )
    )
    return int(result.rowcount or 0)
