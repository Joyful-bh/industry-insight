import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from track_insight.core.enums import TaskStatus
from track_insight.infrastructure.models import Base, Job, PipelineRun
from track_insight.pipeline.service import (
    FullPipelineOptions,
    _find_resumable_run,
    _job_state,
    full_pipeline_status,
)


def test_pipeline_options_require_positive_values() -> None:
    with pytest.raises(ValueError, match="page_work_limit"):
        FullPipelineOptions(page_work_limit=0)


def test_job_state_counts_only_active_statuses() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    object_ids = [str(uuid.uuid4()) for _ in range(4)]
    statuses = [
        TaskStatus.PENDING,
        TaskStatus.FAILED_RETRYABLE,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED_TERMINAL,
    ]
    with Session(engine) as session, session.begin():
        for index, (object_id, status) in enumerate(zip(object_ids, statuses, strict=True)):
            session.add(
                Job(
                    job_type="page_acquire",
                    object_type="page_capture",
                    object_id=object_id,
                    idempotency_key=f"{index:064d}",
                    input_fingerprint="a" * 64,
                    processor_version="v1",
                    status=status,
                    scheduled_at=datetime.now(UTC),
                )
            )
        session.flush()
        state = _job_state(session, "page_acquire", object_ids)

    assert state.active_count == 2
    assert state.status_counts["completed"] == 1
    assert state.status_counts["failed_terminal"] == 1


def test_resume_and_status_select_latest_unfinished_run_for_plan() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    plan_id = uuid.uuid4()
    created_at = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        completed = PipelineRun(
            run_type="full_pipeline",
            status=TaskStatus.COMPLETED,
            input_payload={"plan_id": str(plan_id)},
            counters={"current_stage": "completed"},
            created_at=created_at,
        )
        unfinished = PipelineRun(
            run_type="full_pipeline",
            status=TaskStatus.FAILED_RETRYABLE,
            input_payload={"plan_id": str(plan_id)},
            counters={"current_stage": "search"},
            created_at=created_at + timedelta(seconds=1),
        )
        session.add_all([completed, unfinished])
        session.flush()
        unfinished_id = unfinished.id

        assert _find_resumable_run(session, plan_id).id == unfinished_id
        payload = full_pipeline_status(session, plan_id)

    assert payload["pipeline_run_id"] == str(unfinished_id)
    assert payload["counters"]["current_stage"] == "search"
