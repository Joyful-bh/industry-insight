from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from track_insight.enums import JobStatus
from track_insight.jobs import (
    build_idempotency_key,
    claim_job,
    complete_job,
    enqueue_job,
    fail_job,
    requeue_expired_jobs,
)
from track_insight.models import Base, Job


def make_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_idempotency_key_is_stable() -> None:
    first = build_idempotency_key("parse", "document", "1", "abc", "v1")
    second = build_idempotency_key("parse", "document", "1", "abc", "v1")
    changed = build_idempotency_key("parse", "document", "1", "def", "v1")

    assert first == second
    assert first != changed


def test_enqueue_claim_and_complete() -> None:
    with make_session() as session:
        first = enqueue_job(
            session,
            job_type="document_parse",
            object_type="document",
            object_id="1",
            input_fingerprint="abc",
            processor_version="v1",
        )
        second = enqueue_job(
            session,
            job_type="document_parse",
            object_type="document",
            object_id="1",
            input_fingerprint="abc",
            processor_version="v1",
        )
        assert first.id == second.id

        claimed = claim_job(session, "worker-1")
        assert claimed is not None
        assert claimed.status == JobStatus.RUNNING
        assert claimed.attempts == 1

        complete_job(session, claimed, {"document_version_id": "2"})
        assert claimed.status == JobStatus.SUCCEEDED
        assert claimed.output == {"document_version_id": "2"}


def test_failure_retries_then_stops() -> None:
    with make_session() as session:
        job = enqueue_job(
            session,
            job_type="document_parse",
            object_type="document",
            object_id="1",
            input_fingerprint="abc",
            processor_version="v1",
            max_attempts=1,
        )
        claimed = claim_job(session, "worker-1")
        assert claimed is job

        fail_job(session, job, error_code="parse_error", error_message="invalid document")
        assert job.status == JobStatus.FAILED


def test_expired_lease_is_requeued() -> None:
    with make_session() as session:
        job = enqueue_job(
            session,
            job_type="document_parse",
            object_type="document",
            object_id="1",
            input_fingerprint="abc",
            processor_version="v1",
        )
        claim_job(session, "worker-1")
        job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        session.flush()

        assert requeue_expired_jobs(session) == 1
        refreshed = session.scalar(select(Job).where(Job.id == job.id))
        assert refreshed is not None
        assert refreshed.status == JobStatus.RETRY_WAIT


def test_claim_job_can_filter_job_type() -> None:
    with make_session() as session:
        enqueue_job(
            session,
            job_type="other_job",
            object_type="other",
            object_id="1",
            input_fingerprint="other",
            processor_version="v1",
        )
        parse_job = enqueue_job(
            session,
            job_type="document_parse",
            object_type="document_version",
            object_id="2",
            input_fingerprint="parse",
            processor_version="v1",
        )

        claimed = claim_job(session, "worker-1", job_type="document_parse")

        assert claimed is parse_job


def test_claim_job_can_filter_processor_version() -> None:
    with make_session() as session:
        enqueue_job(
            session,
            job_type="document_parse",
            object_type="document_version",
            object_id="1",
            input_fingerprint="old",
            processor_version="v1",
        )
        current_job = enqueue_job(
            session,
            job_type="document_parse",
            object_type="document_version",
            object_id="2",
            input_fingerprint="current",
            processor_version="v2",
        )

        claimed = claim_job(
            session,
            "worker-1",
            job_type="document_parse",
            processor_version="v2",
        )

        assert claimed is current_job
