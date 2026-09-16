import json
import uuid
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select

from track_insight import __version__
from track_insight.core.errors import TrackInsightError
from track_insight.discovery.coverage import build_coverage_audit
from track_insight.discovery.search import SearchService
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.infrastructure.database import check_database, session_scope
from track_insight.infrastructure.logging import configure_logging
from track_insight.infrastructure.models import PipelineRun, RunEvent
from track_insight.planning.contracts import ResearchScope
from track_insight.planning.service import PlanningService, validate_plan
from track_insight.settings import PocConfig, get_settings, load_poc_config

app = typer.Typer(no_args_is_help=True, help="Agent 驱动的赛道洞察 POC")


@app.callback()
def main() -> None:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        log_file=settings.log_file,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )


@app.command("version")
def version() -> None:
    typer.echo(__version__)


@app.command("config-show")
def config_show(config_path: Path = Path("config/poc.yaml")) -> None:
    config = load_poc_config(config_path)
    settings = get_settings()
    payload = config.model_dump(mode="json")
    payload["bailian_api_key_configured"] = bool(settings.bailian_api_key)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("db-check")
def db_check() -> None:
    check_database()
    typer.echo("database connection ok")


@app.command("research-plan-build")
def research_plan_build(
    regions: Annotated[list[str] | None, typer.Option("--region")] = None,
    industries: Annotated[list[str] | None, typer.Option("--industry")] = None,
    start_date: Annotated[str | None, typer.Option("--start-date")] = None,
    end_date: Annotated[str | None, typer.Option("--end-date")] = None,
    config_path: Path = Path("config/poc.yaml"),
) -> None:
    config = load_poc_config(config_path)
    scope = ResearchScope(
        regions=regions or config.scope.regions,
        industry_scopes=industries or config.scope.industry_scopes,
        start_date=_date(start_date) if start_date else config.scope.start_date,
        end_date=_date(end_date) if end_date else config.scope.end_date,
    )
    client = _build_client(config)
    try:
        with session_scope() as session:
            plan = PlanningService(client=client, config=config).build(session, scope)
            plan_id = str(plan.id)
        typer.echo(json.dumps({"plan_id": plan_id, "status": "completed"}, ensure_ascii=False))
    except TrackInsightError as error:
        raise typer.Exit(code=1) from _echo_error(error)


@app.command("research-plan-validate")
def research_plan_validate(
    plan_id: str,
    config_path: Path = Path("config/poc.yaml"),
) -> None:
    config = load_poc_config(config_path)
    with session_scope() as session:
        result = validate_plan(session, _uuid(plan_id), config)
    typer.echo(result.model_dump_json(indent=2))
    if not result.valid:
        raise typer.Exit(code=1)


@app.command("search-run")
def search_run(
    plan_id: str,
    max_work_packages: Annotated[int | None, typer.Option(min=1)] = None,
    max_searches: Annotated[int | None, typer.Option(min=1)] = None,
    resume: bool = typer.Option(False, "--resume"),
    config_path: Path = Path("config/poc.yaml"),
) -> None:
    config = load_poc_config(config_path)
    client = _build_client(config)
    try:
        with session_scope() as session:
            result = SearchService(client=client, config=config).run(
                session,
                plan_id=_uuid(plan_id),
                max_work_packages=max_work_packages,
                max_searches=max_searches,
                resume=resume,
            )
        typer.echo(result.model_dump_json(indent=2))
    except TrackInsightError as error:
        raise typer.Exit(code=1) from _echo_error(error)


@app.command("url-status")
def url_status(
    plan_id: str,
    config_path: Path = Path("config/poc.yaml"),
) -> None:
    config = load_poc_config(config_path)
    with session_scope() as session:
        audit = build_coverage_audit(session, _uuid(plan_id), config, persist=False)
    typer.echo(audit.model_dump_json(indent=2))


@app.command("run-list")
def run_list(limit: Annotated[int, typer.Option(min=1, max=500)] = 20) -> None:
    with session_scope() as session:
        runs = list(
            session.scalars(select(PipelineRun).order_by(PipelineRun.created_at.desc()).limit(limit))
        )
        payload = [
            {
                "pipeline_run_id": str(run.id),
                "run_type": run.run_type,
                "status": str(run.status),
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "counters": run.counters,
            }
            for run in runs
        ]
    typer.echo(
        json.dumps(payload, ensure_ascii=False, indent=2)
    )


@app.command("run-events")
def run_events(
    pipeline_run_id: str,
    limit: Annotated[int, typer.Option(min=1, max=5000)] = 500,
) -> None:
    with session_scope() as session:
        events = list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.pipeline_run_id == _uuid(pipeline_run_id))
                .order_by(RunEvent.created_at)
                .limit(limit)
            )
        )
        payload = [
            {
                "created_at": event.created_at.isoformat(),
                "level": event.level,
                "event_type": event.event_type,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "message": event.message,
                "duration_ms": event.duration_ms,
                "details": event.details,
            }
            for event in events
        ]
    typer.echo(
        json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _build_client(config: PocConfig) -> BailianClient:
    settings = get_settings()
    if settings.bailian_api_key is None or not settings.bailian_api_key.get_secret_value().strip():
        typer.echo("DASHSCOPE_API_KEY is required for real model and search calls", err=True)
        raise typer.Exit(code=2)
    return BailianClient(
        api_key=settings.bailian_api_key.get_secret_value(),
        base_url=config.bailian.base_url,
        timeout_seconds=config.bailian.timeout_seconds,
        max_retries=config.bailian.max_retries,
    )


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise typer.BadParameter("plan_id must be a UUID") from error


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise typer.BadParameter("dates must use YYYY-MM-DD") from error


def _echo_error(error: Exception) -> Exception:
    typer.echo(str(error), err=True)
    return error
