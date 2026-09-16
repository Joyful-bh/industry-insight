import logging
from typing import Any

from sqlalchemy.orm import Session

from track_insight.infrastructure.models import RunEvent

logger = logging.getLogger("track_insight.run")


def record_event(
    session: Session,
    *,
    event_type: str,
    message: str,
    level: str = "INFO",
    pipeline_run_id: object | None = None,
    job_id: object | None = None,
    model_run_id: object | None = None,
    entity_type: str | None = None,
    entity_id: object | None = None,
    details: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> RunEvent:
    safe_details = details or {}
    event = RunEvent(
        pipeline_run_id=pipeline_run_id,
        job_id=job_id,
        model_run_id=model_run_id,
        level=level.upper(),
        event_type=event_type,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        message=message,
        details=safe_details,
        duration_ms=duration_ms,
    )
    session.add(event)
    logger.log(
        getattr(logging, level.upper(), logging.INFO),
        message,
        extra={
            "event_type": event_type,
            "pipeline_run_id": str(pipeline_run_id) if pipeline_run_id else None,
            "job_id": str(job_id) if job_id else None,
            "model_run_id": str(model_run_id) if model_run_id else None,
            "entity_type": entity_type,
            "entity_id": str(entity_id) if entity_id is not None else None,
            "duration_ms": duration_ms,
            "details": safe_details,
        },
    )
    session.flush()
    return event
