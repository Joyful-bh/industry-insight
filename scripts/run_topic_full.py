"""Run the national Topic build and summarize it before any Track build starts."""

import argparse
import re
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from scripts.event_stage_supervisor import client, save
from track_insight.infrastructure.database import session_scope
from track_insight.infrastructure.models import (
    Job,
    Topic,
    TopicBuildRun,
    TopicCandidate,
    TopicEvent,
)
from track_insight.settings import load_poc_config
from track_insight.topics.service import TopicService, _count_eligible_events, topic_status


def summarize(session, plan_id, run_id):
    topics = list(
        session.scalars(
            select(Topic).where(
                Topic.research_plan_id == plan_id,
                Topic.updated_by_run_id == run_id,
                Topic.status == "candidate",
            )
        )
    )
    labels = Counter(re.sub(r"\W+", "", topic.label).casefold() for topic in topics)
    examples = {}
    for topic in topics:
        examples.setdefault(re.sub(r"\W+", "", topic.label).casefold(), topic.label)
    jobs = list(
        session.scalars(
            select(Job).where(Job.job_type == "topic_generate", Job.object_id.like(f"{run_id}:%"))
        )
    )
    return {
        "topic_status": topic_status(session, plan_id).model_dump(mode="json"),
        "candidate_count": session.scalar(
            select(func.count(TopicCandidate.id)).where(TopicCandidate.topic_build_run_id == run_id)
        ),
        "topic_count": len(topics),
        "topic_event_memberships": session.scalar(
            select(func.count(TopicEvent.id)).where(TopicEvent.topic_build_run_id == run_id)
        ),
        "job_status": dict(Counter(job.status for job in jobs)),
        "exact_normalized_label_duplicate_groups": sum(n > 1 for n in labels.values()),
        "exact_normalized_label_duplicate_excess": sum(n - 1 for n in labels.values()),
        "most_repeated_labels": [
            {"label": examples[label], "count": count}
            for label, count in labels.most_common(20)
            if count > 1
        ],
        "interpretation": (
            "Exact-label duplicates are a lower bound; semantic overlaps require separate review. "
            "This result is local Topics, not final Tracks."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-id", required=True, type=uuid.UUID)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    config = load_poc_config(Path("config/national.yaml"))
    state = {
        "plan_id": str(args.plan_id),
        "status": "preflight",
        "started_at": datetime.now(UTC).isoformat(),
        "attempts": [],
    }

    def record(phase, **extra):
        state.update(extra)
        state["status"] = phase
        state["updated_at"] = datetime.now(UTC).isoformat()
        save(output / "status.json", state)

    record("preflight")
    try:
        with session_scope() as session:
            count = _count_eligible_events(
                session, args.plan_id, config.stage3.minimum_event_confidence
            )
            existing = session.scalar(
                select(TopicBuildRun)
                .where(TopicBuildRun.research_plan_id == args.plan_id)
                .order_by(TopicBuildRun.created_at.desc())
                .limit(1)
            )
            if existing and existing.status == "running":
                raise RuntimeError("A Topic build is already running; inspect it before resuming")
        if count == 0 or count > config.stage3.max_events_per_run:
            raise RuntimeError(
                f"Eligible Event count {count} is outside configured Topic limit "
                f"{config.stage3.max_events_per_run}"
            )
        record("building_topics", eligible_events=count, batch_size=config.stage3.event_batch_size)
        active_client = client(config)
        try:
            service = TopicService(client=active_client, config=config)
            for attempt in range(3):
                with session_scope() as session:
                    result = service.build(session, plan_id=args.plan_id, resume=attempt > 0)
                state["attempts"].append(result)
                record("building_topics", last_result=result)
                if result["status"] == "completed":
                    break
            else:
                record("needs_human", reason="Topic batches still failed after two resumes")
                return
        finally:
            active_client.close()
        with session_scope() as session:
            assessment = summarize(session, args.plan_id, uuid.UUID(result["run_id"]))
        save(output / "assessment.json", assessment)
        record(
            "completed",
            assessment_file=str(output / "assessment.json"),
            topic_count=assessment["topic_count"],
        )
    except Exception as error:
        record("needs_human", error=f"{type(error).__name__}: {error}")
        raise


if __name__ == "__main__":
    main()
