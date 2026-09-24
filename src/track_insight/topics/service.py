import hashlib
import json
import math
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from track_insight.core.enums import TaskStatus
from track_insight.core.errors import ContractError, ModelCallError
from track_insight.core.fingerprints import fingerprint
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.infrastructure.events import record_event
from track_insight.infrastructure.jobs import (
    complete_job,
    enqueue_job,
    fail_job,
    reconcile_stale_pipeline_runs,
)
from track_insight.infrastructure.models import (
    Event,
    Job,
    ModelRun,
    PageAnalysis,
    PageCapture,
    PipelineRun,
    ResearchWorkPackage,
    Topic,
    TopicBuildRun,
    TopicCandidate,
    TopicEvent,
    UrlDiscovery,
)
from track_insight.settings import PocConfig
from track_insight.topics.contracts import TopicGenerationOutput, TopicStatus

TOPIC_PROCESSOR_VERSION = "event-topic-v10-work-package-direct"
NO_RECONCILIATION_VERSION = "not-used"


class TopicService:
    """Generate local observation Topics inside each ResearchWorkPackage."""

    def __init__(self, *, client: BailianClient, config: PocConfig) -> None:
        self.client = client
        self.config = config

    def build(
        self,
        session: Session,
        *,
        plan_id: uuid.UUID,
        max_events: int | None = None,
        batch_size: int | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        reconcile_stale_pipeline_runs(session)
        event_limit = min(
            max_events or self.config.stage3.max_events_per_run,
            self.config.stage3.max_events_per_run,
        )
        effective_batch_size = min(
            batch_size or self.config.stage3.event_batch_size,
            self.config.stage3.event_batch_size,
        )
        events = _select_events(
            session, plan_id, event_limit, self.config.stage3.minimum_event_confidence
        )
        if not events:
            raise ContractError("no eligible events found for the research plan")
        packages = [
            item for item in _events_by_work_package(session, plan_id, events) if item[1]
        ]
        if not packages:
            raise ContractError("eligible Events are not linked to any WorkPackage")

        prompt_path = self.config.stage3.prompts.topic_generation
        prompt = prompt_path.read_text(encoding="utf-8")
        prompt_version = _prompt_version(prompt_path, prompt)
        input_fp = fingerprint(
            [
                {
                    "work_package_id": str(work_package.id),
                    "event_ids": [str(event.id) for event in package_events],
                }
                for work_package, package_events in packages
            ],
            self.config.stage3.model,
            prompt_version,
            effective_batch_size,
        )
        build_run = session.scalar(
            select(TopicBuildRun).where(
                TopicBuildRun.research_plan_id == plan_id,
                TopicBuildRun.input_fingerprint == input_fp,
                TopicBuildRun.processor_version == TOPIC_PROCESSOR_VERSION,
            )
        )
        if (
            build_run is not None
            and build_run.status == TaskStatus.COMPLETED
            and build_run.error_code != "partial_batch_failure"
            and not resume
        ):
            return _run_payload(build_run, resumed=True)
        if build_run is not None and build_run.status == TaskStatus.RUNNING and not resume:
            raise ContractError("an unfinished Topic build exists; rerun with --resume")

        batch_count = sum(
            math.ceil(len(package_events) / effective_batch_size)
            for _, package_events in packages
        )
        pipeline = PipelineRun(
            run_type="event_to_topic",
            status=TaskStatus.RUNNING,
            input_payload={
                "plan_id": str(plan_id),
                "event_count": len(events),
                "work_package_count": len(packages),
                "batch_count": batch_count,
                "batch_size": effective_batch_size,
            },
            started_at=datetime.now(UTC),
        )
        session.add(pipeline)
        session.flush()
        if build_run is None:
            build_run = TopicBuildRun(
                research_plan_id=plan_id,
                pipeline_run_id=pipeline.id,
                status=TaskStatus.RUNNING,
                input_fingerprint=input_fp,
                processor_version=TOPIC_PROCESSOR_VERSION,
                generation_prompt_version=prompt_version,
                reconciliation_prompt_version=NO_RECONCILIATION_VERSION,
                model=self.config.stage3.model,
                event_ids=[str(event.id) for event in events],
                event_count=len(events),
                batch_count=batch_count,
                started_at=datetime.now(UTC),
            )
            session.add(build_run)
            session.flush()
        else:
            build_run.pipeline_run_id = pipeline.id
            build_run.status = TaskStatus.RUNNING
            build_run.error_code = None
            build_run.error_message = None
            build_run.finished_at = None

        record_event(
            session,
            event_type="topic.run.started",
            message="WorkPackage-scoped Event-to-Topic run started",
            pipeline_run_id=pipeline.id,
            entity_type="topic_build_run",
            entity_id=build_run.id,
            details=pipeline.input_payload,
        )
        session.commit()

        failed_batches = 0
        global_batch_no = 0
        for work_package, package_events in packages:
            for package_batch_no, start in enumerate(
                range(0, len(package_events), effective_batch_size), start=1
            ):
                global_batch_no += 1
                succeeded = self._generate_batch(
                    session,
                    pipeline,
                    build_run,
                    work_package,
                    global_batch_no,
                    package_batch_no,
                    package_events[start : start + effective_batch_size],
                    prompt,
                    prompt_version,
                )
                if not succeeded:
                    failed_batches += 1

        candidates = list(
            session.scalars(
                select(TopicCandidate).where(
                    TopicCandidate.topic_build_run_id == build_run.id
                )
            )
        )
        snapshot_activated = bool(candidates)
        if snapshot_activated:
            _activate_topic_snapshot(session, build_run)

        active_topics = int(
            session.scalar(
                select(func.count(Topic.id)).where(
                    Topic.updated_by_run_id == build_run.id,
                    Topic.status == "candidate",
                )
            )
            or 0
        )
        assigned = set(
            session.scalars(
                select(TopicEvent.event_id)
                .join(Topic, Topic.id == TopicEvent.topic_id)
                .where(
                    Topic.updated_by_run_id == build_run.id,
                    Topic.status == "candidate",
                )
            )
        )
        build_run.candidate_count = len(candidates)
        build_run.topic_count = active_topics
        build_run.unassigned_event_count = len({event.id for event in events} - assigned)
        build_run.status = TaskStatus.COMPLETED
        build_run.finished_at = datetime.now(UTC)
        if failed_batches:
            build_run.error_code = "partial_batch_failure"
            build_run.error_message = (
                f"{failed_batches} Topic batch(es) failed; rerun with --resume"
            )
        else:
            build_run.error_code = None
            build_run.error_message = None
        pipeline.status = TaskStatus.COMPLETED
        pipeline.counters = {
            "events": len(events),
            "work_packages": len(packages),
            "batches": batch_count,
            "failed_batches": failed_batches,
            "topics": active_topics,
            "unassigned_events": build_run.unassigned_event_count,
            "snapshot_activated": snapshot_activated,
        }
        pipeline.finished_at = datetime.now(UTC)
        record_event(
            session,
            event_type="topic.run.completed",
            message="WorkPackage-scoped Event-to-Topic run completed",
            level="WARNING" if failed_batches else "INFO",
            pipeline_run_id=pipeline.id,
            entity_type="topic_build_run",
            entity_id=build_run.id,
            details=pipeline.counters,
        )
        session.commit()
        return _run_payload(build_run, resumed=resume)

    def _generate_batch(
        self,
        session: Session,
        pipeline: PipelineRun,
        build_run: TopicBuildRun,
        work_package: ResearchWorkPackage,
        global_batch_no: int,
        package_batch_no: int,
        events: list[Event],
        prompt: str,
        prompt_version: str,
    ) -> bool:
        batch_fp = fingerprint(
            str(work_package.id),
            [(str(event.id), event.event_fingerprint) for event in events],
            prompt_version,
        )
        job = enqueue_job(
            session,
            job_type="topic_generate",
            object_type="work_package_topic_batch",
            object_id=f"{build_run.id}:{work_package.id}:{package_batch_no}",
            input_fingerprint=batch_fp,
            processor_version=TOPIC_PROCESSOR_VERSION,
        )
        if job.status == TaskStatus.COMPLETED:
            return True
        if job.status in (TaskStatus.FAILED_RETRYABLE, TaskStatus.FAILED_TERMINAL):
            job.status = TaskStatus.PENDING
            job.error_code = None
            job.error_message = None
        job.pipeline_run_id = pipeline.id
        job.status = TaskStatus.RUNNING
        job.attempts += 1
        model_run = _new_model_run(
            pipeline.id,
            "work_package_topic_generation",
            self.config.stage3.model,
            prompt_version,
            batch_fp,
        )
        session.add(model_run)
        session.flush()
        started = time.monotonic()
        record_event(
            session,
            event_type="topic.batch.started",
            message="WorkPackage Topic batch started",
            pipeline_run_id=pipeline.id,
            job_id=job.id,
            model_run_id=model_run.id,
            entity_type="research_work_package",
            entity_id=work_package.id,
            details={
                "global_batch_no": global_batch_no,
                "package_batch_no": package_batch_no,
                "event_count": len(events),
                "objective": work_package.objective,
            },
        )
        session.commit()
        try:
            result = self.client.complete_structured(
                messages=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "work_package": _work_package_payload(work_package),
                                "events": [_event_payload(event) for event in events],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                output_model=TopicGenerationOutput,
                model=self.config.stage3.model,
                max_tokens=self.config.stage3.generation_max_output_tokens,
                enable_thinking=False,
            )
            ignored_memberships = _sanitize_generation(
                result.output, {event.id for event in events}
            )
        except (ModelCallError, ContractError) as error:
            _fail_model_and_job(session, model_run, job, error)
            record_event(
                session,
                event_type="topic.batch.skipped",
                message="Invalid Topic batch was recorded and skipped",
                level="WARNING",
                pipeline_run_id=pipeline.id,
                job_id=job.id,
                model_run_id=model_run.id,
                entity_type="research_work_package",
                entity_id=work_package.id,
                details={
                    "global_batch_no": global_batch_no,
                    "package_batch_no": package_batch_no,
                    "error_message": str(error),
                },
            )
            session.commit()
            return False

        _complete_model_run(model_run, result)
        event_map = {event.id: event for event in events}
        for proposed in result.output.candidates:
            _persist_topic_observation(
                session,
                build_run,
                model_run,
                work_package,
                global_batch_no,
                proposed,
                event_map,
            )
        complete_job(
            session,
            job,
            {
                "event_count": len(events),
                "topic_count": len(result.output.candidates),
                "ignored_membership_count": ignored_memberships,
            },
        )
        record_event(
            session,
            event_type="topic.batch.completed",
            message="WorkPackage Topic batch completed",
            pipeline_run_id=pipeline.id,
            job_id=job.id,
            model_run_id=model_run.id,
            entity_type="research_work_package",
            entity_id=work_package.id,
            duration_ms=int((time.monotonic() - started) * 1000),
            details={
                "global_batch_no": global_batch_no,
                "package_batch_no": package_batch_no,
                "event_count": len(events),
                "topic_count": len(result.output.candidates),
                "ignored_membership_count": ignored_memberships,
            },
        )
        session.commit()
        return True


def _persist_topic_observation(
    session: Session,
    build_run: TopicBuildRun,
    model_run: ModelRun,
    work_package: ResearchWorkPackage,
    batch_no: int,
    proposed,
    event_map: dict[uuid.UUID, Event],
) -> None:
    member_events = [event_map[event_id] for event_id in proposed.event_ids]
    stable_suffix = fingerprint(proposed.candidate_key, proposed.label)[:14]
    candidate_key = f"w{work_package.sequence_no}_b{batch_no}_{stable_suffix}"
    keywords = _ordered_union(
        event.industry_objects + ([event.topic_hint] if event.topic_hint else [])
        for event in member_events
    )[:30]
    regions = _ordered_union(event.regions for event in member_events)
    industries = _ordered_union(event.industries for event in member_events)
    memberships = [
        {
            "event_id": str(event_id),
            "relationship": "core_signal",
            "relevance_score": proposed.confidence,
            "reason": "Generated inside one ResearchWorkPackage",
        }
        for event_id in proposed.event_ids
    ]
    session.add(
        TopicCandidate(
            topic_build_run_id=build_run.id,
            model_run_id=model_run.id,
            batch_no=batch_no,
            candidate_key=candidate_key,
            label=proposed.label,
            definition=proposed.definition,
            aliases=[],
            keywords=keywords,
            regions=regions,
            industries=industries,
            summary=proposed.summary,
            proposed_memberships=memberships,
            confidence=proposed.confidence,
            candidate_fingerprint=fingerprint(
                str(work_package.id), proposed.model_dump(mode="json")
            ),
            status="persisted",
        )
    )

    topic_fp = fingerprint(
        str(build_run.id),
        str(work_package.id),
        batch_no,
        proposed.candidate_key,
        proposed.label,
        proposed.definition,
    )
    dates = [event.signal_date for event in member_events if event.signal_date]
    topic = Topic(
        research_plan_id=build_run.research_plan_id,
        created_by_run_id=build_run.id,
        updated_by_run_id=build_run.id,
        canonical_key=f"topic_{topic_fp[:20]}",
        label=proposed.label,
        definition=proposed.definition,
        aliases=[],
        keywords=keywords,
        regions=regions,
        industries=industries,
        summary=proposed.summary,
        first_seen_at=min(dates) if dates else None,
        latest_seen_at=max(dates) if dates else None,
        event_count=len(member_events),
        confidence=proposed.confidence,
        status="building",
        topic_fingerprint=topic_fp,
    )
    session.add(topic)
    session.flush()
    for event_id in proposed.event_ids:
        session.add(
            TopicEvent(
                topic_id=topic.id,
                event_id=event_id,
                topic_build_run_id=build_run.id,
                relationship="core_signal",
                relevance_score=proposed.confidence,
                assignment_reason="Generated inside one ResearchWorkPackage",
                processor_version=TOPIC_PROCESSOR_VERSION,
            )
        )


def _activate_topic_snapshot(session: Session, build_run: TopicBuildRun) -> None:
    session.execute(
        update(Topic)
        .where(
            Topic.research_plan_id == build_run.research_plan_id,
            Topic.status == "candidate",
            Topic.updated_by_run_id != build_run.id,
        )
        .values(status="archived")
    )
    session.execute(
        update(Topic)
        .where(
            Topic.updated_by_run_id == build_run.id,
            Topic.status == "building",
        )
        .values(status="candidate")
    )


def _select_events(
    session: Session, plan_id: uuid.UUID, limit: int, minimum_confidence: float
) -> list[Event]:
    events = list(
        session.scalars(
            select(Event)
            .join(PageAnalysis, PageAnalysis.id == Event.page_analysis_id)
            .join(PageCapture, PageCapture.id == PageAnalysis.page_capture_id)
            .where(
                PageAnalysis.status == TaskStatus.COMPLETED,
                Event.confidence >= minimum_confidence,
                select(UrlDiscovery.id)
                .join(
                    ResearchWorkPackage,
                    ResearchWorkPackage.id == UrlDiscovery.research_work_package_id,
                )
                .where(
                    UrlDiscovery.url_candidate_id == PageCapture.url_candidate_id,
                    ResearchWorkPackage.research_plan_id == plan_id,
                )
                .exists(),
            )
            .order_by(Event.signal_date.asc().nulls_last(), Event.created_at, Event.id)
            .limit(limit)
        )
    )
    return sorted(
        events,
        key=lambda event: (
            (event.topic_hint or "").casefold(),
            ((event.industry_objects or event.industries or [""])[0]).casefold(),
            event.signal_date is None,
            event.signal_date or datetime.max.date(),
            str(event.id),
        ),
    )


def _events_by_work_package(
    session: Session, plan_id: uuid.UUID, events: list[Event]
) -> list[tuple[ResearchWorkPackage, list[Event]]]:
    event_map = {event.id: event for event in events}
    work_packages = list(
        session.scalars(
            select(ResearchWorkPackage)
            .where(ResearchWorkPackage.research_plan_id == plan_id)
            .order_by(ResearchWorkPackage.sequence_no, ResearchWorkPackage.id)
        )
    )
    grouped: dict[uuid.UUID, dict[uuid.UUID, Event]] = defaultdict(dict)
    if event_map and work_packages:
        rows = session.execute(
            select(UrlDiscovery.research_work_package_id, Event.id)
            .join(PageCapture, PageCapture.url_candidate_id == UrlDiscovery.url_candidate_id)
            .join(PageAnalysis, PageAnalysis.page_capture_id == PageCapture.id)
            .join(Event, Event.page_analysis_id == PageAnalysis.id)
            .where(
                UrlDiscovery.research_work_package_id.in_([item.id for item in work_packages]),
                Event.id.in_(event_map),
            )
        )
        for work_package_id, event_id in rows:
            grouped[work_package_id][event_id] = event_map[event_id]
    return [
        (
            work_package,
            sorted(
                grouped.get(work_package.id, {}).values(),
                key=lambda event: (
                    (event.topic_hint or "").casefold(),
                    ((event.industry_objects or event.industries or [""])[0]).casefold(),
                    str(event.id),
                ),
            ),
        )
        for work_package in work_packages
    ]


def _sanitize_generation(output: TopicGenerationOutput, event_ids: set[uuid.UUID]) -> int:
    ignored = 0
    kept = []
    for candidate in output.candidates:
        original = list(candidate.event_ids)
        candidate.event_ids = list(
            dict.fromkeys(event_id for event_id in original if event_id in event_ids)
        )
        ignored += len(original) - len(candidate.event_ids)
        if candidate.event_ids:
            kept.append(candidate)
    output.candidates = kept
    return ignored


def _work_package_payload(work_package: ResearchWorkPackage) -> dict[str, Any]:
    return {
        "work_package_id": str(work_package.id),
        "objective": work_package.objective,
        "regions": work_package.regions,
        "industry_scopes": work_package.industry_scopes,
        "signal_types": work_package.signal_types,
        "source_classes": work_package.source_classes,
    }


def _event_payload(event: Event) -> dict[str, Any]:
    return {
        "event_id": str(event.id),
        "event_type": event.event_type,
        "event_status": event.event_status,
        "title": event.title,
        "summary": event.summary,
        "signal_date": event.signal_date.isoformat() if event.signal_date else None,
        "regions": event.regions,
        "industries": event.industries,
        "industry_objects": event.industry_objects,
        "topic_hint": event.topic_hint,
        "entities": event.entities,
        "chain_roles": event.chain_roles,
        "smb_relevance": event.smb_relevance,
        "confidence": event.confidence,
    }


def _ordered_union(groups) -> list[str]:
    return list(dict.fromkeys(item for group in groups for item in group if item))


def _new_model_run(
    pipeline_run_id: uuid.UUID,
    task_type: str,
    model: str,
    prompt_version: str,
    input_fingerprint: str,
) -> ModelRun:
    return ModelRun(
        pipeline_run_id=pipeline_run_id,
        task_type=task_type,
        status=TaskStatus.RUNNING,
        provider="bailian",
        model=model,
        prompt_version=prompt_version,
        input_fingerprint=input_fingerprint,
        started_at=datetime.now(UTC),
    )


def _complete_model_run(model_run: ModelRun, result) -> None:
    model_run.status = TaskStatus.COMPLETED
    model_run.request_id = result.request_id
    model_run.usage = result.usage
    model_run.raw_response = result.raw_response
    model_run.structured_output = result.output.model_dump(mode="json")
    model_run.finished_at = datetime.now(UTC)


def _fail_model_and_job(
    session: Session, model_run: ModelRun, job: Job, error: Exception
) -> None:
    retryable = isinstance(error, ModelCallError) and error.retryable
    model_run.status = TaskStatus.FAILED_RETRYABLE if retryable else TaskStatus.FAILED_TERMINAL
    model_run.error_code = (
        error.code if isinstance(error, ModelCallError) else "topic_contract_error"
    )
    model_run.error_message = str(error)
    if isinstance(error, ModelCallError):
        model_run.request_id, model_run.usage, model_run.raw_response = error.response_metadata()
    model_run.finished_at = datetime.now(UTC)
    fail_job(
        session,
        job,
        error_code=model_run.error_code,
        error_message=str(error),
        retryable=retryable,
    )


def _prompt_version(path: Path, prompt: str) -> str:
    return f"{path.stem}:{hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:12]}"


def _run_payload(run: TopicBuildRun, *, resumed: bool) -> dict[str, Any]:
    return {
        "run_id": str(run.id),
        "status": str(run.status),
        "event_count": run.event_count,
        "batch_count": run.batch_count,
        "candidate_count": run.candidate_count,
        "topic_count": run.topic_count,
        "unassigned_event_count": run.unassigned_event_count,
        "error_code": run.error_code,
        "error_message": run.error_message,
        "resumed": resumed,
    }


def topic_status(session: Session, plan_id: uuid.UUID) -> TopicStatus:
    run = session.scalar(
        select(TopicBuildRun)
        .where(TopicBuildRun.research_plan_id == plan_id)
        .order_by(TopicBuildRun.created_at.desc())
        .limit(1)
    )
    topic_count = 0
    assigned_ids: set[uuid.UUID] = set()
    failed_jobs = 0
    processed_event_count = 0
    candidate_count = 0
    if run is not None:
        topic_count = int(
            session.scalar(
                select(func.count(Topic.id)).where(
                    Topic.updated_by_run_id == run.id,
                    Topic.status == "candidate",
                )
            )
            or 0
        )
        assigned_ids = set(
            session.scalars(
                select(TopicEvent.event_id)
                .join(Topic, Topic.id == TopicEvent.topic_id)
                .where(
                    Topic.updated_by_run_id == run.id,
                    Topic.status == "candidate",
                )
            )
        )
        jobs = list(
            session.scalars(
                select(Job).where(
                    Job.object_id.like(f"{run.id}:%"), Job.job_type == "topic_generate"
                )
            )
        )
        processed_event_count = sum(
            int((job.output or {}).get("event_count", 0))
            for job in jobs
            if job.status == TaskStatus.COMPLETED
        )
        candidate_count = int(
            session.scalar(
                select(func.count(TopicCandidate.id)).where(
                    TopicCandidate.topic_build_run_id == run.id
                )
            )
            or 0
        )
        failed_jobs = sum(
            job.status in (TaskStatus.FAILED_RETRYABLE, TaskStatus.FAILED_TERMINAL)
            for job in jobs
        )
    return TopicStatus(
        plan_id=str(plan_id),
        run_id=str(run.id) if run else None,
        status=str(run.status) if run else None,
        event_count=run.event_count if run else 0,
        processed_event_count=processed_event_count,
        candidate_count=candidate_count,
        topic_count=topic_count,
        assigned_event_count=len(assigned_ids),
        unassigned_event_count=run.unassigned_event_count if run else 0,
        failed_job_count=failed_jobs,
    )
