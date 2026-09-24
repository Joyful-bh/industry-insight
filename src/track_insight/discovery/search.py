import json
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from track_insight.core.enums import SearchDecision, TaskStatus
from track_insight.core.errors import ContractError, ModelCallError
from track_insight.core.fingerprints import fingerprint
from track_insight.discovery.contracts import SearchReviewOutput, SearchRunSummary
from track_insight.discovery.coverage import build_coverage_audit
from track_insight.discovery.url_registry import register_candidate
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.infrastructure.bailian.contracts import WebSearchSource
from track_insight.infrastructure.events import record_event
from track_insight.infrastructure.jobs import (
    claim_job_for_object,
    complete_job,
    fail_job,
    reconcile_stale_pipeline_runs,
    requeue_expired_jobs,
)
from track_insight.infrastructure.models import (
    Job,
    ModelRun,
    PipelineRun,
    ResearchPlan,
    ResearchWorkPackage,
    SearchResult,
    SearchTask,
)
from track_insight.settings import PocConfig, get_settings

SEARCH_PROCESSOR_VERSION = "search-review-v5-batched-all-sources"
RECOVERABLE_TERMINAL_ERRORS = {
    "invalid_model_output",
    "invalid_model_output_after_recovery",
    "search_reference_mismatch",
    "missing_search_sources",
}


class SearchService:
    def __init__(self, *, client: BailianClient, config: PocConfig) -> None:
        self.client = client
        self.config = config

    def run(
        self,
        session: Session,
        *,
        plan_id: object,
        max_work_packages: int | None = None,
        max_searches: int | None = None,
        resume: bool = False,
    ) -> SearchRunSummary:
        plan = session.get(ResearchPlan, plan_id)
        if plan is None:
            raise ContractError("research plan does not exist")
        requeue_expired_jobs(session)
        reconcile_stale_pipeline_runs(session)
        pipeline_run = PipelineRun(
            run_type="search_discovery",
            status=TaskStatus.RUNNING,
            input_payload={
                "plan_id": str(plan.id),
                "max_work_packages": max_work_packages,
                "max_searches": max_searches,
                "resume": resume,
            },
            started_at=datetime.now(UTC),
        )
        session.add(pipeline_run)
        session.commit()
        record_event(
            session,
            event_type="search.run.started",
            message="Search discovery run started",
            pipeline_run_id=pipeline_run.id,
            entity_type="research_plan",
            entity_id=plan.id,
            details=pipeline_run.input_payload,
        )
        session.commit()
        work_limit = min(
            max_work_packages or self.config.budgets.max_work_packages,
            self.config.budgets.max_work_packages,
        )
        search_limit = min(
            max_searches or self.config.budgets.max_searches_per_work_package,
            self.config.budgets.max_searches_per_work_package,
        )
        packages = list(
            session.scalars(
                select(ResearchWorkPackage)
                .where(ResearchWorkPackage.research_plan_id == plan.id)
                .order_by(ResearchWorkPackage.sequence_no)
                .limit(work_limit)
            )
        )
        processed = skipped = failed = 0
        eligible_statuses = (
            (TaskStatus.PENDING, TaskStatus.FAILED_RETRYABLE) if resume else (TaskStatus.PENDING,)
        )
        for package in packages:
            tasks = list(
                session.scalars(
                    select(SearchTask)
                    .where(SearchTask.research_work_package_id == package.id)
                    .order_by(SearchTask.id)
                    .limit(search_limit)
                )
            )
            for task in tasks:
                if (
                    resume
                    and task.status == TaskStatus.FAILED_TERMINAL
                    and task.error_code in RECOVERABLE_TERMINAL_ERRORS
                ):
                    self._requeue_validation_failure(session, task, pipeline_run.id)
                if task.status == TaskStatus.COMPLETED or task.status not in eligible_statuses:
                    record_event(
                        session,
                        event_type="search.task.skipped",
                        message="Search task skipped because its status is not eligible",
                        pipeline_run_id=pipeline_run.id,
                        entity_type="search_task",
                        entity_id=task.id,
                        details={"status": str(task.status), "query": task.query},
                    )
                    skipped += 1
                    continue
                job = claim_job_for_object(
                    session,
                    worker_id=f"search-run-{pipeline_run.id}",
                    lease_seconds=get_settings().job_lease_seconds,
                    job_type="search",
                    object_type="search_task",
                    object_id=str(task.id),
                )
                if job is None:
                    skipped += 1
                    record_event(
                        session,
                        event_type="search.task.skipped",
                        message="Search task skipped because its job is currently not claimable",
                        level="WARNING",
                        pipeline_run_id=pipeline_run.id,
                        entity_type="search_task",
                        entity_id=task.id,
                        details={"status": str(task.status), "query": task.query},
                    )
                    session.commit()
                    continue
                remaining_candidates = package.max_candidates - self._candidate_count(
                    session, package.id
                )
                if remaining_candidates <= 0:
                    task.status = TaskStatus.COMPLETED
                    task.completion_reason = "candidate_budget_reached"
                    task.finished_at = datetime.now(UTC)
                    complete_job(session, job, {"completion_reason": "candidate_budget_reached"})
                    skipped += 1
                    session.commit()
                    continue
                try:
                    self._run_task(
                        session,
                        plan,
                        package,
                        task,
                        job=job,
                        pipeline_run_id=pipeline_run.id,
                        remaining_candidates=remaining_candidates,
                    )
                    processed += 1
                except ModelCallError:
                    failed += 1
                    session.commit()
                    continue
                session.commit()
            self._update_package_status(session, package)
            session.commit()
        audit = build_coverage_audit(session, plan.id, self.config)
        partial_success = processed > 0 and failed > 0
        pipeline_run.status = (
            TaskStatus.COMPLETED
            if processed > 0 or failed == 0
            else TaskStatus.FAILED_RETRYABLE
        )
        pipeline_run.finished_at = datetime.now(UTC)
        pipeline_run.counters = {
            "processed_searches": processed,
            "skipped_searches": skipped,
            "failed_searches": failed,
            "candidate_count": audit.candidate_count,
        }
        record_event(
            session,
            event_type=(
                "search.run.completed"
                if pipeline_run.status == TaskStatus.COMPLETED
                else "search.run.failed"
            ),
            message="Search discovery run completed",
            level="WARNING" if partial_success else "INFO",
            pipeline_run_id=pipeline_run.id,
            entity_type="research_plan",
            entity_id=plan.id,
            details=pipeline_run.counters,
        )
        session.commit()
        return SearchRunSummary(
            plan_id=str(plan.id),
            processed_searches=processed,
            skipped_searches=skipped,
            failed_searches=failed,
            audit=audit,
        )

    def _run_task(
        self,
        session: Session,
        plan: ResearchPlan,
        package: ResearchWorkPackage,
        task: SearchTask,
        *,
        job: Job,
        pipeline_run_id: object,
        remaining_candidates: int,
    ) -> None:
        prompt_path = self.config.prompts.search_review
        prompt = prompt_path.read_text(encoding="utf-8")
        prompt_version = _prompt_version(prompt_path, prompt)
        task_payload = {
            "query": task.query,
            "purpose": task.purpose,
            "regions": task.target_regions,
            "industries": task.target_industries,
            "signal_types": task.target_signal_types,
            "source_classes": task.target_source_classes,
            "start_date": plan.start_date.isoformat(),
            "end_date": plan.end_date.isoformat(),
        }
        model_run = ModelRun(
            pipeline_run_id=pipeline_run_id,
            task_type="search_review",
            status=TaskStatus.RUNNING,
            provider="bailian",
            model=self.config.bailian.model,
            prompt_version=prompt_version,
            input_fingerprint=fingerprint(task_payload, prompt_version, self.config.bailian.model),
            tool_config={
                "type": "web_search",
                "review_batch_size": self.config.bailian.review_batch_size,
                "search_max_output_tokens": self.config.bailian.search_max_output_tokens,
                "api": "responses",
            },
            started_at=datetime.now(UTC),
        )
        session.add(model_run)
        session.flush()
        task.status = TaskStatus.RUNNING
        task.model_run_id = model_run.id
        task.started_at = datetime.now(UTC)
        session.flush()
        task_started = time.monotonic()
        record_event(
            session,
            event_type="search.task.started",
            message="Search task started",
            pipeline_run_id=pipeline_run_id,
            job_id=job.id,
            model_run_id=model_run.id,
            entity_type="search_task",
            entity_id=task.id,
            details={
                "query": task.query,
                "purpose": task.purpose,
                "target_source_classes": task.target_source_classes,
                "prompt_version": prompt_version,
                "model": self.config.bailian.model,
            },
        )
        try:
            persisted_results: dict[str, SearchResult] = {}

            def persist_discovered_sources(sources: list[WebSearchSource]) -> None:
                existing_results = {
                    item.refer: item
                    for item in session.scalars(
                        select(SearchResult).where(SearchResult.search_task_id == task.id)
                    )
                }
                for source in sources:
                    search_result = existing_results.get(source.refer)
                    if search_result is None:
                        search_result = SearchResult(
                            search_task_id=task.id,
                            refer=source.refer,
                            title=source.title,
                            url=source.link,
                            snippet=source.content,
                            media=source.media,
                            publish_date=source.publish_date,
                            decision="pending_review",
                            url_type="unknown",
                            source_class="unknown",
                            reason="等待结构化复核",
                            raw_payload=source.model_dump(mode="json"),
                        )
                        session.add(search_result)
                    else:
                        search_result.title = source.title
                        search_result.url = source.link
                        search_result.snippet = source.content
                        search_result.media = source.media
                        search_result.publish_date = source.publish_date
                        search_result.decision = "pending_review"
                        search_result.url_type = "unknown"
                        search_result.source_class = "unknown"
                        search_result.reason = "等待结构化复核"
                        search_result.raw_payload = source.model_dump(mode="json")
                    session.flush()
                    persisted_results[source.refer] = search_result
                record_event(
                    session,
                    event_type="search.sources.persisted",
                    message="All discovered search sources persisted before review",
                    pipeline_run_id=pipeline_run_id,
                    job_id=job.id,
                    model_run_id=model_run.id,
                    entity_type="search_task",
                    entity_id=task.id,
                    details={"source_count": len(sources)},
                )
                session.commit()

            result = self.client.search_and_review(
                messages=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(task_payload, ensure_ascii=False),
                    },
                ],
                output_model=SearchReviewOutput,
                model=self.config.bailian.model,
                review_batch_size=self.config.bailian.review_batch_size,
                search_max_output_tokens=self.config.bailian.search_max_output_tokens,
                on_sources_discovered=persist_discovered_sources,
            )
            judgments = {item.refer: item for item in result.output.judgments}
            references = {item.refer for item in result.web_search}
            if not references:
                raise ModelCallError(
                    "Bailian response contains no search sources",
                    code="missing_search_sources",
                )
            if set(judgments) != references:
                raise ModelCallError(
                    "search judgments must cover every returned refer exactly once",
                    code="search_reference_mismatch",
                )
        except ModelCallError as error:
            model_run.status = (
                TaskStatus.FAILED_RETRYABLE if error.retryable else TaskStatus.FAILED_TERMINAL
            )
            model_run.error_code = error.code
            model_run.error_message = str(error)
            diagnostics = error.diagnostics
            request_id, usage, raw_diagnostics = error.response_metadata()
            model_run.request_id = request_id
            model_run.usage = usage
            model_run.raw_response = raw_diagnostics
            model_run.finished_at = datetime.now(UTC)
            task.status = model_run.status
            task.error_code = error.code
            task.error_message = str(error)
            task.finished_at = datetime.now(UTC)
            fail_job(
                session,
                job,
                error_code=error.code,
                error_message=str(error),
                retryable=error.retryable,
            )
            record_event(
                session,
                event_type="search.task.failed",
                message="Search task failed",
                level="ERROR",
                pipeline_run_id=pipeline_run_id,
                job_id=job.id,
                model_run_id=model_run.id,
                entity_type="search_task",
                entity_id=task.id,
                duration_ms=int((time.monotonic() - task_started) * 1000),
                details={
                    "query": task.query,
                    "error_code": error.code,
                    "retryable": error.retryable,
                    "error_message": str(error),
                    "diagnostic_keys": sorted(diagnostics),
                    "expected_refers": diagnostics.get("expected_refers"),
                    "actual_refers": diagnostics.get("actual_refers"),
                },
            )
            session.flush()
            raise

        model_run.status = TaskStatus.COMPLETED
        model_run.request_id = result.request_id
        model_run.usage = result.usage
        model_run.raw_response = result.raw_response
        model_run.structured_output = result.output.model_dump(mode="json")
        model_run.finished_at = datetime.now(UTC)
        task.model_run_id = model_run.id
        registered_candidates = 0
        for source in result.web_search:
            judgment = judgments[source.refer]
            search_result = persisted_results[source.refer]
            search_result.decision = judgment.decision
            search_result.url_type = judgment.url_type
            search_result.source_class = judgment.source_class
            search_result.reason = judgment.reason
            if (
                judgment.decision in {SearchDecision.KEEP, SearchDecision.MAYBE}
                and registered_candidates < remaining_candidates
            ):
                try:
                    register_candidate(
                        session,
                        work_package_id=package.id,
                        search_task_id=task.id,
                        search_result=search_result,
                    )
                except ContractError:
                    search_result.decision = SearchDecision.DROP
                    search_result.reason = f"{search_result.reason}；URL格式无效"
                else:
                    registered_candidates += 1
        task.status = TaskStatus.COMPLETED
        task.completion_reason = "search_completed"
        task.finished_at = datetime.now(UTC)
        task.error_code = None
        task.error_message = None
        complete_job(
            session,
            job,
            {
                "completion_reason": "search_completed",
                "search_result_count": len(result.web_search),
            },
        )
        decision_counts: dict[str, int] = {}
        for judgment in result.output.judgments:
            decision_counts[judgment.decision.value] = (
                decision_counts.get(judgment.decision.value, 0) + 1
            )
        record_event(
            session,
            event_type="search.task.completed",
            message="Search task completed",
            pipeline_run_id=pipeline_run_id,
            job_id=job.id,
            model_run_id=model_run.id,
            entity_type="search_task",
            entity_id=task.id,
            duration_ms=int((time.monotonic() - task_started) * 1000),
            details={
                "query": task.query,
                "request_id": result.request_id,
                "api_call_count": result.call_count,
                "recovery_used": result.recovery_used,
                "search_result_count": len(result.web_search),
                "registered_candidate_count": registered_candidates,
                "decision_counts": decision_counts,
                "usage": result.usage,
            },
        )
        session.flush()

    @staticmethod
    def _requeue_validation_failure(
        session: Session, task: SearchTask, pipeline_run_id: object
    ) -> None:
        job = session.scalar(
            select(Job).where(
                Job.job_type == "search",
                Job.object_type == "search_task",
                Job.object_id == str(task.id),
            )
        )
        if job is None or job.attempts >= job.max_attempts:
            return
        previous_error = task.error_code
        task.status = TaskStatus.FAILED_RETRYABLE
        task.finished_at = None
        job.status = TaskStatus.FAILED_RETRYABLE
        job.processor_version = SEARCH_PROCESSOR_VERSION
        job.scheduled_at = datetime.now(UTC)
        job.finished_at = None
        record_event(
            session,
            event_type="search.task.requeued",
            message="Search task requeued after review implementation changed",
            pipeline_run_id=pipeline_run_id,
            job_id=job.id,
            entity_type="search_task",
            entity_id=task.id,
            details={"previous_error_code": previous_error, "attempts": job.attempts},
        )

    @staticmethod
    def _candidate_count(session: Session, package_id: object) -> int:
        from sqlalchemy import func

        from track_insight.infrastructure.models import UrlDiscovery

        return int(
            session.scalar(
                select(func.count(func.distinct(UrlDiscovery.url_candidate_id))).where(
                    UrlDiscovery.research_work_package_id == package_id
                )
            )
            or 0
        )

    @staticmethod
    def _update_package_status(session: Session, package: ResearchWorkPackage) -> None:
        statuses = set(
            session.scalars(
                select(SearchTask.status).where(SearchTask.research_work_package_id == package.id)
            )
        )
        if statuses and statuses == {TaskStatus.COMPLETED}:
            package.status = TaskStatus.COMPLETED
        elif TaskStatus.RUNNING in statuses:
            package.status = TaskStatus.RUNNING
        elif TaskStatus.FAILED_TERMINAL in statuses:
            package.status = TaskStatus.FAILED_TERMINAL
        elif TaskStatus.FAILED_RETRYABLE in statuses:
            package.status = TaskStatus.FAILED_RETRYABLE
        session.flush()


def _prompt_version(path: Path, content: str) -> str:
    return f"{path.stem}:{fingerprint(content)[:12]}"
