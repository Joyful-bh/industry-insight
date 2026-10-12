import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from track_insight.core.enums import TaskStatus
from track_insight.core.errors import ContractError, ModelCallError
from track_insight.core.fingerprints import fingerprint
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.infrastructure.bailian.contracts import ModelRunResult
from track_insight.infrastructure.events import record_event
from track_insight.infrastructure.jobs import enqueue_job
from track_insight.infrastructure.models import (
    ModelRun,
    PipelineRun,
    ResearchPlan,
    ResearchWorkPackage,
    SearchTask,
)
from track_insight.planning.contracts import (
    CompactPlanningOutput,
    PlanningOutput,
    PlanValidationResult,
    ResearchScope,
    expand_planning_output,
)
from track_insight.planning.coverage import (
    expand_scope,
    generate_covered_plan,
    missing_regions,
    query_is_regional,
    select_supplement_regions,
    validate_supplement,
)
from track_insight.settings import PocConfig

PLANNING_PROCESSOR_VERSION = "planning-v3-compact-output"


class PlanningService:
    def __init__(self, *, client: BailianClient | None = None, config: PocConfig) -> None:
        self.client = client
        self.config = config

    def build(self, session: Session, scope: ResearchScope) -> ResearchPlan:
        if self.client is None:
            raise ContractError("a model client is required to generate a plan")
        scope = expand_scope(scope)
        prompt_path = self.config.prompts.planning
        prompt = prompt_path.read_text(encoding="utf-8")
        prompt_version = _prompt_version(prompt_path, prompt)
        input_payload = {
            "scope": scope.model_dump(mode="json"),
            "budgets": self.config.budgets.model_dump(mode="json"),
            "required_source_classes": self.config.coverage.required_source_classes,
        }
        input_fingerprint = fingerprint(input_payload, prompt_version, self.config.bailian.model)
        started_at = datetime.now(UTC)
        pipeline_run = PipelineRun(
            run_type="research_planning",
            status=TaskStatus.RUNNING,
            input_payload=input_payload,
            started_at=started_at,
        )
        session.add(pipeline_run)
        session.flush()
        model_run = ModelRun(
            pipeline_run_id=pipeline_run.id,
            task_type="research_planning",
            status=TaskStatus.RUNNING,
            provider="bailian",
            model=self.config.bailian.model,
            prompt_version=prompt_version,
            input_fingerprint=input_fingerprint,
            started_at=started_at,
        )
        session.add(model_run)
        session.flush()
        operation_started = time.monotonic()
        record_event(
            session,
            event_type="planning.started",
            message="Research planning started",
            pipeline_run_id=pipeline_run.id,
            model_run_id=model_run.id,
            entity_type="pipeline_run",
            entity_id=pipeline_run.id,
            details={
                "scope": scope.model_dump(mode="json"),
                "prompt_version": prompt_version,
                "model": self.config.bailian.model,
                "input_fingerprint": input_fingerprint,
            },
        )
        try:

            def complete_batch(name, payload):
                batch = self.client.complete_structured(
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    output_model=CompactPlanningOutput,
                    model=self.config.bailian.model,
                    max_tokens=8192,
                    enable_thinking=False,
                )
                batch.output = expand_planning_output(
                    batch.output,
                    ResearchScope.model_validate(payload["scope"]),
                    payload["region_refs"],
                )
                return batch

            output, results = generate_covered_plan(scope, self.config, complete_batch)
            result = ModelRunResult(
                model=self.config.bailian.model,
                output=output,
                usage={
                    key: sum(r.usage.get(key, 0) for r in results)
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                },
                raw_response={"batches": [r.model_dump(mode="json") for r in results]},
                call_count=sum(r.call_count for r in results),
                recovery_used=any(r.recovery_used for r in results),
            )
            self._validate_output(scope, result.output)
        except (ModelCallError, ContractError) as error:
            if isinstance(error, ModelCallError):
                _fail_model_run(model_run, error)
                request_id, usage, raw_diagnostics = error.response_metadata()
                model_run.request_id = request_id
                model_run.usage = usage
                model_run.raw_response = raw_diagnostics
            else:
                model_run.status = TaskStatus.FAILED_TERMINAL
                model_run.error_code = "invalid_planning_output"
                model_run.error_message = str(error)
                model_run.finished_at = datetime.now(UTC)
            pipeline_run.status = model_run.status
            pipeline_run.finished_at = datetime.now(UTC)
            record_event(
                session,
                event_type="planning.failed",
                message="Research planning failed",
                level="ERROR",
                pipeline_run_id=pipeline_run.id,
                model_run_id=model_run.id,
                entity_type="pipeline_run",
                entity_id=pipeline_run.id,
                duration_ms=int((time.monotonic() - operation_started) * 1000),
                details={
                    "error_code": getattr(error, "code", "invalid_planning_output"),
                    "error_message": str(error),
                    "diagnostic_keys": sorted(getattr(error, "diagnostics", {})),
                },
            )
            session.commit()
            raise

        model_run.status = TaskStatus.COMPLETED
        model_run.request_id = result.request_id
        model_run.usage = result.usage
        model_run.raw_response = result.raw_response
        model_run.structured_output = result.output.model_dump(mode="json")
        model_run.finished_at = datetime.now(UTC)
        pipeline_run.status = TaskStatus.COMPLETED
        pipeline_run.finished_at = datetime.now(UTC)
        pipeline_run.counters = {
            "work_packages": len(result.output.work_packages),
            "search_tasks": sum(
                len(package.search_tasks) for package in result.output.work_packages
            ),
        }

        plan = ResearchPlan(
            status=TaskStatus.COMPLETED,
            config_version=self.config.version,
            regions=scope.regions,
            industry_scopes=scope.industry_scopes,
            start_date=scope.start_date,
            end_date=scope.end_date,
            model_run_id=model_run.id,
        )
        session.add(plan)
        session.flush()
        self._persist_packages(session, plan.id, result.output.work_packages)
        record_event(
            session,
            event_type="planning.completed",
            message="Research planning completed",
            pipeline_run_id=pipeline_run.id,
            model_run_id=model_run.id,
            entity_type="research_plan",
            entity_id=plan.id,
            duration_ms=int((time.monotonic() - operation_started) * 1000),
            details={
                **pipeline_run.counters,
                "request_id": result.request_id,
                "api_call_count": result.call_count,
                "recovery_used": result.recovery_used,
                "usage": result.usage,
            },
        )
        session.flush()
        return plan

    def import_plan(
        self,
        session: Session,
        scope: ResearchScope,
        output: PlanningOutput,
        *,
        source_path: Path,
        metadata_path: Path,
    ) -> ResearchPlan:
        scope = expand_scope(scope)
        self._validate_output(scope, output)
        import_key = fingerprint(
            {
                "scope": scope.model_dump(mode="json"),
                "plan": output.model_dump(mode="json"),
                "budgets": self.config.budgets.model_dump(mode="json"),
            },
            "saved-plan-import-v1",
        )
        for previous in session.scalars(
            select(PipelineRun).where(
                PipelineRun.run_type == "research_plan_import",
                PipelineRun.status == TaskStatus.COMPLETED,
            )
        ):
            if previous.input_payload.get("import_key") == import_key:
                plan = session.get(ResearchPlan, uuid.UUID(previous.input_payload["plan_id"]))
                if plan is not None:
                    return plan
        plan = ResearchPlan(
            status=TaskStatus.COMPLETED,
            config_version=self.config.version,
            regions=scope.regions,
            industry_scopes=scope.industry_scopes,
            start_date=scope.start_date,
            end_date=scope.end_date,
        )
        session.add(plan)
        session.flush()
        self._persist_packages(session, plan.id, output.work_packages)
        validation = validate_plan(session, plan.id, self.config)
        if not validation.valid:
            raise ContractError("imported plan validation failed: " + "; ".join(validation.errors))
        now = datetime.now(UTC)
        run = PipelineRun(
            run_type="research_plan_import",
            status=TaskStatus.COMPLETED,
            started_at=now,
            finished_at=now,
            input_payload={
                "plan_id": str(plan.id),
                "import_key": import_key,
                "source_path": str(source_path.resolve()),
                "metadata_path": str(metadata_path.resolve()),
                "scope": scope.model_dump(mode="json"),
            },
            counters={
                "work_packages": len(output.work_packages),
                "search_tasks": validation.search_task_count,
                "api_calls": 0,
            },
        )
        session.add(run)
        session.flush()
        record_event(
            session,
            event_type="planning.imported",
            message="Saved research plan imported",
            pipeline_run_id=run.id,
            entity_type="research_plan",
            entity_id=plan.id,
            details=run.input_payload,
        )
        return plan

    def _persist_packages(self, session, plan_id, proposals, start=1):
        for package_index, proposal in enumerate(proposals, start=start):
            work_package = ResearchWorkPackage(
                research_plan_id=plan_id,
                sequence_no=package_index,
                status=TaskStatus.PENDING,
                objective=proposal.objective,
                regions=proposal.regions,
                industry_scopes=proposal.industry_scopes,
                signal_types=proposal.signal_types,
                source_classes=[item.value for item in proposal.source_classes],
                max_candidates=self.config.budgets.max_candidates_per_work_package,
            )
            session.add(work_package)
            session.flush()
            for task in proposal.search_tasks:
                query_fingerprint = fingerprint(
                    task.model_dump(mode="json"), PLANNING_PROCESSOR_VERSION
                )
                search_task = SearchTask(
                    research_work_package_id=work_package.id,
                    query=task.query.strip(),
                    purpose=task.purpose,
                    target_regions=task.target_regions,
                    target_industries=task.target_industries,
                    target_signal_types=task.target_signal_types,
                    target_source_classes=[item.value for item in task.target_source_classes],
                    query_fingerprint=query_fingerprint,
                )
                session.add(search_task)
                session.flush()
                enqueue_job(
                    session,
                    job_type="search",
                    object_type="search_task",
                    object_id=str(search_task.id),
                    input_fingerprint=query_fingerprint,
                    processor_version="search-review-v5-batched-all-sources",
                    max_attempts=self.config.bailian.max_retries + 1,
                )

    def supplement(self, session: Session, plan_id, audit) -> int:
        if self.client is None:
            raise ContractError("a model client is required to supplement a plan")
        previous = session.scalars(
            select(PipelineRun).where(PipelineRun.run_type == "coverage_supplement")
        )
        if any(p.input_payload.get("plan_id") == str(plan_id) for p in previous):
            return 0
        plan = session.get(ResearchPlan, plan_id)
        packages = list(
            session.scalars(
                select(ResearchWorkPackage).where(ResearchWorkPackage.research_plan_id == plan_id)
            )
        )
        regions = select_supplement_regions(
            audit.region_coverage,
            min(
                self.config.coverage.max_supplement_regions,
                self.config.budgets.max_work_packages - len(packages),
            ),
        )
        if not regions:
            return 0
        existing = list(
            session.scalars(
                select(SearchTask)
                .join(ResearchWorkPackage)
                .where(ResearchWorkPackage.research_plan_id == plan_id)
            )
        )
        payload = {
            "plan_id": str(plan_id),
            "scope": {
                "regions": regions,
                "industry_scopes": plan.industry_scopes,
                "start_date": plan.start_date.isoformat(),
                "end_date": plan.end_date.isoformat(),
            },
            "region_coverage": {r: audit.region_coverage[r] for r in regions},
            "existing_queries": [t.query for t in existing if set(t.target_regions) & set(regions)],
        }
        path = self.config.prompts.planning_supplement
        payload["region_refs"] = {f"G{i}": region for i, region in enumerate(regions, 1)}
        prompt = path.read_text(encoding="utf-8")
        pipeline = PipelineRun(
            run_type="coverage_supplement",
            status=TaskStatus.RUNNING,
            input_payload=payload,
            started_at=datetime.now(UTC),
        )
        session.add(pipeline)
        session.flush()
        model_run = ModelRun(
            pipeline_run_id=pipeline.id,
            task_type="coverage_supplement_planning",
            status=TaskStatus.RUNNING,
            provider="bailian",
            model=self.config.bailian.model,
            prompt_version=_prompt_version(path, prompt),
            input_fingerprint=fingerprint(payload),
            started_at=datetime.now(UTC),
        )
        session.add(model_run)
        session.commit()
        try:
            result = self.client.complete_structured(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                output_model=CompactPlanningOutput,
                model=self.config.bailian.model,
                max_tokens=8192,
                enable_thinking=False,
            )
            result.output = expand_planning_output(
                result.output,
                ResearchScope.model_validate(payload["scope"]),
                payload["region_refs"],
            )
            validate_supplement(
                result.output,
                regions,
                payload["existing_queries"],
                plan.start_date.year,
                plan.end_date.year,
            )
            self._persist_packages(
                session,
                plan_id,
                result.output.work_packages,
                max((p.sequence_no for p in packages), default=0) + 1,
            )
            model_run.request_id, model_run.usage = result.request_id, result.usage
            model_run.raw_response = result.raw_response
            model_run.structured_output = result.output.model_dump(mode="json")
            model_run.status = pipeline.status = TaskStatus.COMPLETED
            model_run.finished_at = pipeline.finished_at = datetime.now(UTC)
            pipeline.counters = {"supplement_regions": regions, "new_queries": len(regions) * 2}
            session.commit()
            return len(regions)
        except (ModelCallError, ContractError) as error:
            model_run.status = pipeline.status = TaskStatus.FAILED_TERMINAL
            model_run.error_message = str(error)
            model_run.error_code = getattr(error, "code", "invalid_supplement")
            if isinstance(error, ModelCallError):
                model_run.request_id, model_run.usage, model_run.raw_response = (
                    error.response_metadata()
                )
            model_run.finished_at = pipeline.finished_at = datetime.now(UTC)
            session.commit()
            raise

    def _validate_output(self, scope: ResearchScope, output: PlanningOutput) -> None:
        if len(output.work_packages) > self.config.budgets.max_work_packages:
            raise ContractError("planning output exceeds max_work_packages")
        for package in output.work_packages:
            if len(package.search_tasks) > self.config.budgets.max_searches_per_work_package:
                raise ContractError("planning output exceeds max_searches_per_work_package")
            if not set(package.regions).issubset(scope.regions):
                raise ContractError("work package contains region outside research scope")
            for task in package.search_tasks:
                if not set(task.target_regions).issubset(scope.regions):
                    raise ContractError("search task contains region outside research scope")
        if missing_regions(output, scope.regions):
            raise ContractError("search plan omitted mandatory regions")


def validate_plan(session: Session, plan_id: object, config: PocConfig) -> PlanValidationResult:
    plan = session.get(ResearchPlan, plan_id)
    if plan is None:
        raise ContractError("research plan does not exist")
    packages = list(
        session.scalars(
            select(ResearchWorkPackage)
            .where(ResearchWorkPackage.research_plan_id == plan.id)
            .order_by(ResearchWorkPackage.sequence_no)
        )
    )
    tasks = list(
        session.scalars(
            select(SearchTask)
            .join(ResearchWorkPackage)
            .where(ResearchWorkPackage.research_plan_id == plan.id)
        )
    )
    errors: list[str] = []
    if len(packages) > config.budgets.max_work_packages:
        errors.append("work package budget exceeded")
    for package in packages:
        package_task_count = sum(task.research_work_package_id == package.id for task in tasks)
        if package_task_count > config.budgets.max_searches_per_work_package:
            errors.append(f"search budget exceeded for work package {package.id}")
    covered = sorted({value for package in packages for value in package.source_classes})
    missing = sorted(set(config.coverage.required_source_classes) - set(covered))
    warnings = [f"missing source class: {value}" for value in missing]
    covered_regions = {
        task.target_regions[0]
        for task in tasks
        if len(task.target_regions) == 1 and query_is_regional(task.query, task.target_regions[0])
    }
    errors.extend(
        f"missing regional search coverage: {region}"
        for region in plan.regions
        if region not in covered_regions
    )
    return PlanValidationResult(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        work_package_count=len(packages),
        search_task_count=len(tasks),
        covered_source_classes=covered,
    )


def _prompt_version(path: Path, content: str) -> str:
    return f"{path.stem}:{fingerprint(content)[:12]}"


def _fail_model_run(model_run: ModelRun, error: ModelCallError) -> None:
    model_run.status = (
        TaskStatus.FAILED_RETRYABLE if error.retryable else TaskStatus.FAILED_TERMINAL
    )
    model_run.error_code = error.code
    model_run.error_message = str(error)
    model_run.finished_at = datetime.now(UTC)
