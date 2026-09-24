from __future__ import annotations

import time
import uuid
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from track_insight.content.service import PageService
from track_insight.core.enums import TaskStatus
from track_insight.core.errors import ContractError, TrackInsightError
from track_insight.discovery.coverage import build_coverage_audit
from track_insight.discovery.search import SearchService
from track_insight.events.service import EventService, stage2_status
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.infrastructure.database import session_scope
from track_insight.infrastructure.events import record_event
from track_insight.infrastructure.jobs import requeue_expired_jobs
from track_insight.infrastructure.models import (
    Job,
    PageCapture,
    PipelineRun,
    ResearchPlan,
    ResearchWorkPackage,
    SearchTask,
    UrlCandidate,
    UrlDiscovery,
)
from track_insight.planning.contracts import ResearchScope
from track_insight.planning.service import PlanningService, validate_plan
from track_insight.reporting.dashboard import build_track_dashboard
from track_insight.settings import PocConfig
from track_insight.topics.service import TopicService, topic_status
from track_insight.tracks.service import TrackService, track_status

FULL_PIPELINE_RUN_TYPE = "full_pipeline"
ACTIVE_JOB_STATUSES = {
    TaskStatus.PENDING,
    TaskStatus.RUNNING,
    TaskStatus.FAILED_RETRYABLE,
}

ClientFactory = Callable[[], BailianClient]
ProgressCallback = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class FullPipelineOptions:
    output: Path = Path("reports/track_dashboard_latest.html")
    page_enqueue_limit: int = 100_000
    page_work_limit: int = 100
    event_work_limit: int = 40
    event_workers: int = 4
    poll_interval_seconds: float = 5.0
    queue_timeout_seconds: float = 1_800.0

    def __post_init__(self) -> None:
        positive = {
            "page_enqueue_limit": self.page_enqueue_limit,
            "page_work_limit": self.page_work_limit,
            "event_work_limit": self.event_work_limit,
            "event_workers": self.event_workers,
            "poll_interval_seconds": self.poll_interval_seconds,
            "queue_timeout_seconds": self.queue_timeout_seconds,
        }
        invalid = [name for name, value in positive.items() if value <= 0]
        if invalid:
            raise ValueError(f"pipeline options must be positive: {', '.join(invalid)}")


@dataclass(frozen=True)
class JobState:
    active_count: int
    status_counts: dict[str, int]


class PipelineExecutionError(TrackInsightError):
    def __init__(
        self,
        message: str,
        *,
        pipeline_run_id: uuid.UUID,
        plan_id: uuid.UUID | None,
        stage: str,
    ) -> None:
        super().__init__(message)
        self.pipeline_run_id = pipeline_run_id
        self.plan_id = plan_id
        self.stage = stage

    @property
    def resume_command(self) -> str:
        if self.plan_id is not None:
            return f"uv run track-insight pipeline-run --plan-id {self.plan_id} --resume"
        return "uv run track-insight pipeline-run --resume"


class FullPipelineService:
    """Run the existing stages as one resumable, database-backed workflow."""

    def __init__(
        self,
        *,
        config: PocConfig,
        client_factory: ClientFactory,
        progress: ProgressCallback | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.client_factory = client_factory
        self.progress = progress or (lambda _event, _payload: None)
        self.sleep = sleep

    def run(
        self,
        *,
        scope: ResearchScope | None = None,
        plan_id: uuid.UUID | None = None,
        resume: bool = False,
        options: FullPipelineOptions | None = None,
    ) -> dict[str, Any]:
        effective = options or FullPipelineOptions(
            event_workers=self.config.bailian.max_concurrency
        )
        if effective.event_workers > self.config.bailian.max_concurrency:
            raise ValueError(
                "event_workers cannot exceed "
                f"bailian.max_concurrency={self.config.bailian.max_concurrency}"
            )
        run_id, active_plan_id, active_scope = self._begin_run(
            scope=scope,
            plan_id=plan_id,
            resume=resume,
            output=effective.output,
        )
        current_stage = "starting"
        shared_client: BailianClient | None = None
        try:
            shared_client = self.client_factory()
            if active_plan_id is None:
                if active_scope is None:
                    raise ContractError("a research scope is required for a new pipeline")
                current_stage = "planning"
                self._stage_started(run_id, current_stage)
                with session_scope() as session:
                    plan = PlanningService(client=shared_client, config=self.config).build(
                        session, active_scope
                    )
                    active_plan_id = plan.id
                self._attach_plan(run_id, active_plan_id)
                self._stage_completed(run_id, current_stage, {"plan_id": str(active_plan_id)})

            current_stage = "plan_validation"
            self._stage_started(run_id, current_stage)
            with session_scope() as session:
                validation = validate_plan(session, active_plan_id, self.config)
            if not validation.valid:
                raise ContractError(
                    "research plan validation failed: " + "; ".join(validation.errors)
                )
            self._stage_completed(
                run_id, current_stage, validation.model_dump(mode="json")
            )

            current_stage = "search"
            self._stage_started(run_id, current_stage)
            search_result = self._run_search(shared_client, active_plan_id, effective)
            self._stage_completed(run_id, current_stage, search_result)

            current_stage = "page_acquisition"
            self._stage_started(run_id, current_stage)
            page_result = self._run_pages(shared_client, active_plan_id, effective)
            self._stage_completed(run_id, current_stage, page_result)

            current_stage = "event_extraction"
            self._stage_started(run_id, current_stage)
            event_result = self._run_events(active_plan_id, effective)
            if event_result["status"]["event_count"] == 0:
                raise ContractError("no eligible industry events were extracted")
            self._stage_completed(run_id, current_stage, event_result)

            current_stage = "topic_build"
            self._stage_started(run_id, current_stage)
            topic_result = self._run_topics(shared_client, active_plan_id)
            self._stage_completed(run_id, current_stage, topic_result)

            current_stage = "track_build"
            self._stage_started(run_id, current_stage)
            with session_scope() as session:
                track_result = TrackService(
                    client=shared_client, config=self.config
                ).build(session, plan_id=active_plan_id)
            self._stage_completed(run_id, current_stage, track_result)

            current_stage = "track_analysis"
            self._stage_started(run_id, current_stage)
            analysis_result = self._run_track_analysis(shared_client, active_plan_id)
            self._stage_completed(run_id, current_stage, analysis_result)

            current_stage = "dashboard"
            self._stage_started(run_id, current_stage)
            with session_scope() as session:
                dashboard_result = build_track_dashboard(
                    session, active_plan_id, output=effective.output
                )
            self._stage_completed(run_id, current_stage, dashboard_result)

            result = {
                "pipeline_run_id": str(run_id),
                "plan_id": str(active_plan_id),
                "status": str(TaskStatus.COMPLETED),
                **dashboard_result,
            }
            self._finish_run(run_id, result)
            self.progress("pipeline.completed", result)
            return result
        except BaseException as error:
            self._fail_run(run_id, active_plan_id, current_stage, error)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise PipelineExecutionError(
                f"pipeline failed at {current_stage}: {error}",
                pipeline_run_id=run_id,
                plan_id=active_plan_id,
                stage=current_stage,
            ) from error
        finally:
            if shared_client is not None:
                shared_client.close()

    def _run_search(
        self,
        client: BailianClient,
        plan_id: uuid.UUID,
        options: FullPipelineOptions,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + options.queue_timeout_seconds
        latest: dict[str, Any] = {}
        while True:
            with session_scope() as session:
                summary = SearchService(client=client, config=self.config).run(
                    session, plan_id=plan_id, resume=True
                )
                object_ids = _plan_search_task_ids(session, plan_id)
                state = _job_state(session, "search", object_ids)
                audit = build_coverage_audit(session, plan_id, self.config, persist=False)
            latest = {
                "processed_searches": summary.processed_searches,
                "skipped_searches": summary.skipped_searches,
                "failed_searches": summary.failed_searches,
                "job_status_counts": state.status_counts,
                "audit": audit.model_dump(mode="json"),
            }
            self.progress("pipeline.search.batch", latest)
            if state.active_count == 0:
                if audit.candidate_count == 0:
                    raise ContractError("search completed without retained content URLs")
                return latest
            self._wait_for_queue(deadline, options.poll_interval_seconds, "search")

    def _run_pages(
        self,
        client: BailianClient,
        plan_id: uuid.UUID,
        options: FullPipelineOptions,
    ) -> dict[str, Any]:
        with session_scope() as session:
            enqueue_result = PageService(config=self.config, client=client).enqueue(
                session,
                plan_id=plan_id,
                decisions=["keep", "maybe"],
                limit=options.page_enqueue_limit,
            )
        deadline = time.monotonic() + options.queue_timeout_seconds
        totals = Counter()
        while True:
            with session_scope() as session:
                requeue_expired_jobs(session)
            batch = PageService(config=self.config, client=client).fetch_work(
                limit=options.page_work_limit
            )
            totals.update({key: int(batch[key]) for key in ("completed", "failed", "skipped")})
            with session_scope() as session:
                state = _job_state(
                    session, "page_acquire", _plan_capture_ids(session, plan_id)
                )
            self.progress(
                "pipeline.page.batch",
                {"batch": batch, "job_status_counts": state.status_counts},
            )
            if state.active_count == 0:
                with session_scope() as session:
                    status = stage2_status(session, plan_id).model_dump(mode="json")
                return {"enqueued": enqueue_result, "totals": dict(totals), "status": status}
            if sum(int(batch[key]) for key in ("completed", "failed", "skipped")) == 0:
                self._wait_for_queue(deadline, options.poll_interval_seconds, "page acquisition")

    def _run_events(
        self, plan_id: uuid.UUID, options: FullPipelineOptions
    ) -> dict[str, Any]:
        deadline = time.monotonic() + options.queue_timeout_seconds
        totals = Counter()
        while True:
            with session_scope() as session:
                requeue_expired_jobs(session)
            batch = self._run_event_workers(options.event_work_limit, options.event_workers)
            totals.update(
                {key: int(batch[key]) for key in ("completed", "failed", "skipped", "event_count")}
            )
            with session_scope() as session:
                state = _job_state(
                    session, "page_analyze", _plan_capture_ids(session, plan_id)
                )
                status = stage2_status(session, plan_id).model_dump(mode="json")
            self.progress(
                "pipeline.event.batch",
                {"batch": batch, "job_status_counts": state.status_counts},
            )
            if state.active_count == 0:
                return {"totals": dict(totals), "status": status}
            if sum(int(batch[key]) for key in ("completed", "failed", "skipped")) == 0:
                self._wait_for_queue(deadline, options.poll_interval_seconds, "event extraction")

    def _run_event_workers(self, limit: int, workers: int) -> dict[str, int]:
        worker_count = min(workers, limit)
        base_limit, extra = divmod(limit, worker_count)
        limits = [base_limit + (1 if index < extra else 0) for index in range(worker_count)]

        def run_worker(worker_limit: int) -> dict[str, int | str]:
            client = self.client_factory()
            try:
                return EventService(client=client, config=self.config).extract_work(
                    limit=worker_limit
                )
            finally:
                client.close()

        if worker_count == 1:
            results = [run_worker(limits[0])]
        else:
            with ThreadPoolExecutor(
                max_workers=worker_count, thread_name_prefix="full-pipeline-event"
            ) as executor:
                results = list(executor.map(run_worker, limits))
        return {
            "workers": worker_count,
            "completed": sum(int(item["completed"]) for item in results),
            "failed": sum(int(item["failed"]) for item in results),
            "skipped": sum(int(item["skipped"]) for item in results),
            "event_count": sum(int(item["event_count"]) for item in results),
        }

    def _run_topics(self, client: BailianClient, plan_id: uuid.UUID) -> dict[str, Any]:
        attempts = self.config.bailian.max_retries + 1
        latest: dict[str, Any] = {}
        for _ in range(attempts):
            with session_scope() as session:
                current = topic_status(session, plan_id)
                if (
                    current.status == TaskStatus.COMPLETED
                    and current.failed_job_count == 0
                    and current.topic_count > 0
                ):
                    return current.model_dump(mode="json")
                latest = TopicService(client=client, config=self.config).build(
                    session,
                    plan_id=plan_id,
                    resume=current.run_id is not None,
                )
            if not latest.get("error_code"):
                return latest
            self.progress("pipeline.topic.retry", latest)
        raise ContractError(latest.get("error_message") or "Topic generation did not complete")

    def _run_track_analysis(
        self, client: BailianClient, plan_id: uuid.UUID
    ) -> dict[str, Any]:
        attempts = self.config.bailian.max_retries + 1
        latest: dict[str, Any] = {}
        for _ in range(attempts):
            with session_scope() as session:
                status = track_status(session, plan_id)
                if status.track_count > 0 and status.analyzed_count == status.track_count:
                    return status.model_dump(mode="json")
                latest = TrackService(client=client, config=self.config).analyze(
                    session, plan_id=plan_id
                )
            if int(latest.get("failed", 0)) == 0:
                with session_scope() as session:
                    return track_status(session, plan_id).model_dump(mode="json")
            self.progress("pipeline.track_analysis.retry", latest)
        raise ContractError(
            f"{latest.get('failed', 0)} Track analysis item(s) remain failed"
        )

    def _wait_for_queue(self, deadline: float, interval: float, stage: str) -> None:
        if time.monotonic() >= deadline:
            raise ContractError(f"{stage} queue did not drain before timeout")
        self.sleep(min(interval, max(0.1, deadline - time.monotonic())))

    def _begin_run(
        self,
        *,
        scope: ResearchScope | None,
        plan_id: uuid.UUID | None,
        resume: bool,
        output: Path,
    ) -> tuple[uuid.UUID, uuid.UUID | None, ResearchScope | None]:
        with session_scope() as session:
            run = _find_resumable_run(session, plan_id) if resume else None
            if run is not None:
                saved_plan_id = run.input_payload.get("plan_id")
                plan_id = uuid.UUID(saved_plan_id) if saved_plan_id else plan_id
                saved_scope = run.input_payload.get("scope")
                if scope is None and saved_scope:
                    scope = ResearchScope.model_validate(saved_scope)
                counters = dict(run.counters or {})
                counters["resume_count"] = int(counters.get("resume_count", 0)) + 1
                counters["current_stage"] = "resuming"
                counters.pop("error", None)
                run.counters = counters
                run.status = TaskStatus.RUNNING
                run.finished_at = None
                record_event(
                    session,
                    event_type="full_pipeline.resumed",
                    message="Full pipeline resumed",
                    pipeline_run_id=run.id,
                    details={"plan_id": str(plan_id) if plan_id else None},
                )
                return run.id, plan_id, scope
            if resume and plan_id is None:
                raise ContractError("no unfinished full pipeline run is available to resume")
            if plan_id is not None and session.get(ResearchPlan, plan_id) is None:
                raise ContractError("research plan does not exist")
            payload: dict[str, Any] = {
                "plan_id": str(plan_id) if plan_id else None,
                "scope": scope.model_dump(mode="json") if scope else None,
                "output": str(output),
            }
            run = PipelineRun(
                run_type=FULL_PIPELINE_RUN_TYPE,
                status=TaskStatus.RUNNING,
                input_payload=payload,
                counters={"current_stage": "starting", "completed_stages": []},
                started_at=datetime.now(UTC),
            )
            session.add(run)
            session.flush()
            record_event(
                session,
                event_type="full_pipeline.started",
                message="Full pipeline started",
                pipeline_run_id=run.id,
                details=payload,
            )
            return run.id, plan_id, scope

    def _attach_plan(self, run_id: uuid.UUID, plan_id: uuid.UUID) -> None:
        with session_scope() as session:
            run = _required_run(session, run_id)
            payload = dict(run.input_payload or {})
            payload["plan_id"] = str(plan_id)
            run.input_payload = payload

    def _stage_started(self, run_id: uuid.UUID, stage: str) -> None:
        with session_scope() as session:
            run = _required_run(session, run_id)
            counters = dict(run.counters or {})
            counters["current_stage"] = stage
            run.counters = counters
            record_event(
                session,
                event_type="full_pipeline.stage.started",
                message=f"Full pipeline stage started: {stage}",
                pipeline_run_id=run.id,
                entity_type="pipeline_stage",
                entity_id=stage,
            )
        self.progress("pipeline.stage.started", {"stage": stage})

    def _stage_completed(
        self, run_id: uuid.UUID, stage: str, result: dict[str, Any]
    ) -> None:
        with session_scope() as session:
            run = _required_run(session, run_id)
            counters = dict(run.counters or {})
            completed = list(counters.get("completed_stages", []))
            if stage not in completed:
                completed.append(stage)
            counters["completed_stages"] = completed
            counters["current_stage"] = stage
            counters["last_result"] = _compact_result(result)
            run.counters = counters
            record_event(
                session,
                event_type="full_pipeline.stage.completed",
                message=f"Full pipeline stage completed: {stage}",
                pipeline_run_id=run.id,
                entity_type="pipeline_stage",
                entity_id=stage,
                details=_compact_result(result),
            )
        self.progress("pipeline.stage.completed", {"stage": stage, "result": result})

    def _finish_run(self, run_id: uuid.UUID, result: dict[str, Any]) -> None:
        with session_scope() as session:
            run = _required_run(session, run_id)
            counters = dict(run.counters or {})
            counters["current_stage"] = "completed"
            counters["result"] = _compact_result(result)
            run.counters = counters
            run.status = TaskStatus.COMPLETED
            run.finished_at = datetime.now(UTC)
            record_event(
                session,
                event_type="full_pipeline.completed",
                message="Full pipeline completed",
                pipeline_run_id=run.id,
                details=_compact_result(result),
            )

    def _fail_run(
        self,
        run_id: uuid.UUID,
        plan_id: uuid.UUID | None,
        stage: str,
        error: BaseException,
    ) -> None:
        with session_scope() as session:
            run = session.get(PipelineRun, run_id)
            if run is None:
                return
            counters = dict(run.counters or {})
            counters["current_stage"] = stage
            counters["error"] = {"type": type(error).__name__, "message": str(error)}
            run.counters = counters
            payload = dict(run.input_payload or {})
            if plan_id is not None:
                payload["plan_id"] = str(plan_id)
            run.input_payload = payload
            run.status = TaskStatus.FAILED_RETRYABLE
            run.finished_at = datetime.now(UTC)
            record_event(
                session,
                event_type="full_pipeline.failed",
                message=f"Full pipeline failed at {stage}",
                level="ERROR",
                pipeline_run_id=run.id,
                entity_type="pipeline_stage",
                entity_id=stage,
                details=counters["error"],
            )


def full_pipeline_status(session: Session, plan_id: uuid.UUID | None = None) -> dict[str, Any]:
    statement = (
        select(PipelineRun)
        .where(PipelineRun.run_type == FULL_PIPELINE_RUN_TYPE)
        .order_by(PipelineRun.created_at.desc())
    )
    runs = list(session.scalars(statement.limit(100)))
    if plan_id is not None:
        runs = [run for run in runs if (run.input_payload or {}).get("plan_id") == str(plan_id)]
    if not runs:
        return {"pipeline_run_id": None, "plan_id": str(plan_id) if plan_id else None}
    run = runs[0]
    return {
        "pipeline_run_id": str(run.id),
        "plan_id": (run.input_payload or {}).get("plan_id"),
        "status": str(run.status),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "counters": run.counters,
    }


def _find_resumable_run(
    session: Session, plan_id: uuid.UUID | None
) -> PipelineRun | None:
    runs = list(
        session.scalars(
            select(PipelineRun)
            .where(
                PipelineRun.run_type == FULL_PIPELINE_RUN_TYPE,
                PipelineRun.status != TaskStatus.COMPLETED,
            )
            .order_by(PipelineRun.created_at.desc())
            .limit(100)
        )
    )
    if plan_id is None:
        return runs[0] if runs else None
    return next(
        (run for run in runs if (run.input_payload or {}).get("plan_id") == str(plan_id)),
        None,
    )


def _required_run(session: Session, run_id: uuid.UUID) -> PipelineRun:
    run = session.get(PipelineRun, run_id)
    if run is None:
        raise ContractError("full pipeline run does not exist")
    return run


def _plan_search_task_ids(session: Session, plan_id: uuid.UUID) -> list[str]:
    return [
        str(value)
        for value in session.scalars(
            select(SearchTask.id)
            .join(ResearchWorkPackage)
            .where(ResearchWorkPackage.research_plan_id == plan_id)
        )
    ]


def _plan_capture_ids(session: Session, plan_id: uuid.UUID) -> list[str]:
    return [
        str(value)
        for value in session.scalars(
            select(PageCapture.id)
            .join(UrlCandidate, UrlCandidate.id == PageCapture.url_candidate_id)
            .join(UrlDiscovery, UrlDiscovery.url_candidate_id == UrlCandidate.id)
            .join(
                ResearchWorkPackage,
                ResearchWorkPackage.id == UrlDiscovery.research_work_package_id,
            )
            .where(ResearchWorkPackage.research_plan_id == plan_id)
            .distinct()
        )
    ]


def _job_state(session: Session, job_type: str, object_ids: list[str]) -> JobState:
    if not object_ids:
        return JobState(active_count=0, status_counts={})
    statuses = list(
        session.scalars(
            select(Job.status).where(Job.job_type == job_type, Job.object_id.in_(object_ids))
        )
    )
    counts = Counter(str(status) for status in statuses)
    return JobState(
        active_count=sum(counts.get(str(status), 0) for status in ACTIVE_JOB_STATUSES),
        status_counts=dict(counts),
    )


def _compact_result(result: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in result.items():
        if key == "audit" and isinstance(value, dict):
            compact[key] = {
                name: value.get(name)
                for name in (
                    "candidate_count",
                    "completed_search_task_count",
                    "search_task_count",
                    "warnings",
                )
            }
        elif (
            key in {"status", "totals", "enqueued"} and isinstance(value, dict)
        ) or not isinstance(value, (dict, list)):
            compact[key] = value
    return compact
