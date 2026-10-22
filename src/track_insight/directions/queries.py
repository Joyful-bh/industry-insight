import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from track_insight.core.enums import TaskStatus
from track_insight.infrastructure.models import Direction, DirectionBuildRun, DirectionTopic, Topic


def list_directions(
    session: Session, plan_id: uuid.UUID, *, limit: int = 100
) -> list[dict[str, Any]]:
    run = session.scalar(
        select(DirectionBuildRun)
        .where(
            DirectionBuildRun.research_plan_id == plan_id,
            DirectionBuildRun.status == TaskStatus.COMPLETED,
        )
        .order_by(DirectionBuildRun.created_at.desc())
        .limit(1)
    )
    if run is None:
        return []
    directions = list(
        session.scalars(
            select(Direction)
            .where(Direction.direction_build_run_id == run.id)
            .order_by(Direction.name, Direction.id)
            .limit(limit)
        )
    )
    result = []
    for direction in directions:
        topics = list(
            session.scalars(
                select(Topic)
                .join(DirectionTopic, DirectionTopic.topic_id == Topic.id)
                .where(DirectionTopic.direction_id == direction.id)
                .order_by(Topic.label, Topic.id)
            )
        )
        result.append(
            {
                "direction_id": str(direction.id),
                "name": direction.name,
                "definition": direction.definition,
                "topics": [{"topic_id": str(topic.id), "name": topic.label} for topic in topics],
            }
        )
    return result
