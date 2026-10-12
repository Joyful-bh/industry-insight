import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from track_insight.core.enums import SearchDecision, TaskStatus, UrlType
from track_insight.core.errors import ContractError, ModelCallError
from track_insight.core.fingerprints import fingerprint
from track_insight.discovery.contracts import SearchReviewOutput, SearchRunSummary
from track_insight.discovery.coverage import build_coverage_audit
from track_insight.discovery.opensearch import search_and_review as opensearch_search_and_review
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

SEARCH_PROCESSOR_VERSION = "search-v7-cited-sources"
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
        package_ids = [package.id for package in packages]
        plan_key, run_key = plan.id, pipeline_run.id
        session.commit()
        workers = min(self.config.bailian.max_concurrency, len(package_ids))
        if workers > 1:
            bind = session.get_bind()
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(
                        self._run_package_isolated,
                        bind,
                        plan_key,
                        package_id,
                        run_key,
                        search_limit,
                        resume,
                    )
                    for package_id in package_ids
                ]
                try:
                    for future in futures:
                        done, ignored, errors = future.result()
                        processed += done
                        skipped += ignored
                        failed += errors
                except BaseException:
                    for future in futures:
                        future.cancel()
                    raise
            session.expire_all()
        else:
            for package in packages:
                done, ignored, errors = self._run_package(
                    session, plan, package, pipeline_run.id, search_limit, resume
                )
                processed += done
                skipped += ignored
                failed += errors
        audit = build_coverage_audit(session, plan.id, self.config)
        partial_success = processed > 0 and failed > 0
        pipeline_run.status = (
            TaskStatus.COMPLETED if processed > 0 or failed == 0 else TaskStatus.FAILED_RETRYABLE
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

    def _run_package_isolated(
        self, bind, plan_id, package_id, pipeline_run_id, search_limit, resume
    ) -> tuple[int, int, int]:
        with Session(bind=bind) as worker_session:
            return self._run_package(
                worker_session,
                worker_session.get(ResearchPlan, plan_id),
                worker_session.get(ResearchWorkPackage, package_id),
                pipeline_run_id,
                search_limit,
                resume,
            )

    def _run_package(
        self, session, plan, package, pipeline_run_id, search_limit, resume
    ) -> tuple[int, int, int]:
        processed = skipped = failed = 0
        eligible_statuses = (
            (TaskStatus.PENDING, TaskStatus.FAILED_RETRYABLE) if resume else (TaskStatus.PENDING,)
        )
        tasks = list(
            session.scalars(
                select(SearchTask)
                .where(SearchTask.research_work_package_id == package.id)
                .order_by(SearchTask.id)
                .limit(search_limit)
            )
        )
        for task in tasks:
            if resume and task.status == TaskStatus.RUNNING:
                recovered_job = session.scalar(
                    select(Job).where(
                        Job.job_type == "search",
                        Job.object_type == "search_task",
                        Job.object_id == str(task.id),
                        Job.status == TaskStatus.FAILED_RETRYABLE,
                    )
                )
                if recovered_job is not None:
                    task.status = TaskStatus.FAILED_RETRYABLE
            if (
                resume
                and task.status == TaskStatus.FAILED_TERMINAL
                and task.error_code in RECOVERABLE_TERMINAL_ERRORS
            ):
                self._requeue_validation_failure(session, task, pipeline_run_id)
            if task.status == TaskStatus.COMPLETED or task.status not in eligible_statuses:
                record_event(
                    session,
                    event_type="search.task.skipped",
                    message="Search task skipped because its status is not eligible",
                    pipeline_run_id=pipeline_run_id,
                    entity_type="search_task",
                    entity_id=task.id,
                    details={"status": str(task.status), "query": task.query},
                )
                skipped += 1
                continue
            job = claim_job_for_object(
                session,
                worker_id=f"search-run-{pipeline_run_id}",
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
                    pipeline_run_id=pipeline_run_id,
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
                    pipeline_run_id=pipeline_run_id,
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
        return processed, skipped, failed

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
        cited_only = (
            self.config.search.provider == "bailian" and self.config.search.selection == "citations"
        )
        if cited_only:
            prompt = "收集百炼搜索综述引用的URL，作为待核验的原始材料候选。"
            prompt_version = "search-citations-v1"
        else:
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
        dates = re.match(r"(\d{4}-\d{2}-\d{2})至(\d{4}-\d{2}-\d{2})", task.query)
        if dates:
            start, end = dates.groups()
            if task_payload["start_date"] <= start <= end <= task_payload["end_date"]:
                task_payload["start_date"], task_payload["end_date"] = start, end
        model_run = ModelRun(
            pipeline_run_id=pipeline_run_id,
            task_type="search_citations" if cited_only else "search_review",
            status=TaskStatus.RUNNING,
            provider="bailian",
            model=self.config.bailian.model,
            prompt_version=prompt_version,
            input_fingerprint=fingerprint(
                task_payload,
                prompt_version,
                self.config.bailian.model,
                self.config.search.model_dump(mode="json"),
            ),
            tool_config={
                "type": "web_search",
                "review_batch_size": self.config.bailian.review_batch_size,
                "search_max_output_tokens": self.config.bailian.search_max_output_tokens,
                "api": self.config.search.provider,
                "search_config": self.config.search.model_dump(mode="json"),
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
        session.commit()
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
                            reason="等待候选选择",
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
                        search_result.reason = "等待候选选择"
                        search_result.raw_payload = source.model_dump(mode="json")
                    session.flush()
                    persisted_results[source.refer] = search_result
                record_event(
                    session,
                    event_type="search.sources.persisted",
                    message="All discovered search sources persisted before selection",
                    pipeline_run_id=pipeline_run_id,
                    job_id=job.id,
                    model_run_id=model_run.id,
                    entity_type="search_task",
                    entity_id=task.id,
                    details={"source_count": len(sources)},
                )
                session.commit()

            messages = [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(task_payload, ensure_ascii=False),
                },
            ]
            if self.config.search.provider == "opensearch":
                result = opensearch_search_and_review(
                    client=self.client,
                    config=self.config,
                    messages=messages,
                    output_model=SearchReviewOutput,
                    on_sources_discovered=persist_discovered_sources,
                )
            else:
                result = self.client.search_and_review(
                    messages=messages,
                    output_model=SearchReviewOutput,
                    model=self.config.bailian.model,
                    review_batch_size=self.config.bailian.review_batch_size,
                    search_max_output_tokens=self.config.bailian.search_max_output_tokens,
                    on_sources_discovered=persist_discovered_sources,
                    cited_only=cited_only,
                )
            if not cited_only:
                result.raw_response["review_constraints"] = apply_review_constraints(
                    result.output, result.web_search, task_payload
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

        # 不同工作包可能同时发现同一 URL；仅串行化短暂的候选落库事务。
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(74120901)"))
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


def apply_review_constraints(
    output: SearchReviewOutput, sources: list[WebSearchSource], task: dict
) -> list[dict]:
    corrections = []
    by_refer = {source.refer: source for source in sources}
    for judgment in output.judgments:
        source = by_refer.get(judgment.refer)
        if source is None:
            continue
        reason = None
        published = None
        if source.publish_date:
            try:
                published = date.fromisoformat(source.publish_date[:10]).isoformat()
            except ValueError:
                pass
        if published and not task["start_date"] <= published <= task["end_date"]:
            reason = "搜索返回的发布日期超出本任务时间范围。"
        url = urlsplit(source.link)
        query_keys = {key.lower() for key in parse_qs(url.query)}
        search_page = bool(re.search(r"/(?:search|searchlist)(?:/|$)", url.path.lower()))
        search_page = search_page or bool(query_keys & {"q", "query", "searchword", "keyword"})
        if judgment.url_type == UrlType.SOURCE_ENTRY or search_page:
            reason = "栏目、列表或搜索入口不能作为具体信号材料。"
            judgment.url_type = UrlType.SOURCE_ENTRY
        if reason and judgment.decision != SearchDecision.DROP:
            corrections.append(
                {
                    "refer": judgment.refer,
                    "model_decision": judgment.decision.value,
                    "final_decision": "drop",
                    "reason": reason,
                }
            )
            judgment.decision = SearchDecision.DROP
            judgment.reason = reason
    return corrections
