import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select

from track_insight import __version__
from track_insight.content.service import PageService
from track_insight.core.enums import SearchDecision
from track_insight.core.errors import TrackInsightError
from track_insight.discovery.coverage import build_coverage_audit
from track_insight.discovery.search import SearchService
from track_insight.events.service import EventService, list_events, stage2_status
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.infrastructure.database import check_database, session_scope
from track_insight.infrastructure.logging import configure_logging
from track_insight.infrastructure.models import PipelineRun, RunEvent
from track_insight.planning.contracts import ResearchScope
from track_insight.planning.service import PlanningService, validate_plan
from track_insight.reporting.dashboard import build_track_dashboard
from track_insight.settings import PocConfig, get_settings, load_poc_config
from track_insight.topics.queries import list_topics, show_topic
from track_insight.topics.service import TopicService, topic_status
from track_insight.tracks.queries import list_tracks, show_track
from track_insight.tracks.service import TrackService, track_status

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


@app.command("page-enqueue")
def page_enqueue(
    plan_id: str,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 30,
    decisions: Annotated[str, typer.Option(help="逗号分隔：keep,maybe")] = "keep,maybe",
    config_path: Path = Path("config/poc.yaml"),
) -> None:
    config = load_poc_config(config_path)
    requested = [item.strip() for item in decisions.split(",") if item.strip()]
    allowed = {item.value for item in SearchDecision}
    invalid = sorted(set(requested) - allowed)
    if not requested or invalid:
        raise typer.BadParameter(f"decisions must contain values from {', '.join(sorted(allowed))}")
    with session_scope() as session:
        result = PageService(config=config).enqueue(
            session,
            plan_id=_uuid(plan_id),
            decisions=requested,
            limit=limit,
        )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("page-fetch-work")
def page_fetch_work(
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 20,
    config_path: Path = Path("config/poc.yaml"),
) -> None:
    config = load_poc_config(config_path)
    client = _build_optional_client(config)
    try:
        result = PageService(config=config, client=client).fetch_work(limit=limit)
    except TrackInsightError as error:
        raise typer.Exit(code=1) from _echo_error(error)
    finally:
        if client is not None:
            client.close()
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["failed"]:
        raise typer.Exit(code=1)


@app.command("event-extract-work")
def event_extract_work(
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 5,
    workers: Annotated[int, typer.Option(min=1, max=8)] = 1,
    config_path: Path = Path("config/poc.yaml"),
) -> None:
    config = load_poc_config(config_path)
    if workers > config.bailian.max_concurrency:
        raise typer.BadParameter(
            f"workers cannot exceed bailian.max_concurrency={config.bailian.max_concurrency}"
        )
    worker_count = min(workers, limit)
    base_limit, extra = divmod(limit, worker_count)
    worker_limits = [base_limit + (1 if index < extra else 0) for index in range(worker_count)]
    try:
        if worker_count == 1:
            results = [_run_event_extract_worker(config, worker_limits[0])]
        else:
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="event-extract",
            ) as executor:
                results = list(
                    executor.map(
                        lambda worker_limit: _run_event_extract_worker(config, worker_limit),
                        worker_limits,
                    )
                )
    except TrackInsightError as error:
        raise typer.Exit(code=1) from _echo_error(error)
    result = {
        "pipeline_run_ids": [item["pipeline_run_id"] for item in results],
        "workers": worker_count,
        "completed": sum(int(item["completed"]) for item in results),
        "failed": sum(int(item["failed"]) for item in results),
        "skipped": sum(int(item["skipped"]) for item in results),
        "event_count": sum(int(item["event_count"]) for item in results),
    }
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["failed"]:
        raise typer.Exit(code=1)


@app.command("stage2-status")
def stage2_plan_status(plan_id: str) -> None:
    with session_scope() as session:
        result = stage2_status(session, _uuid(plan_id))
    typer.echo(result.model_dump_json(indent=2))


@app.command("event-list")
def event_list(
    plan_id: str,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 50,
) -> None:
    with session_scope() as session:
        result = list_events(session, _uuid(plan_id), limit=limit)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("topic-build")
def topic_build(
    plan_id: str,
    max_events: Annotated[int | None, typer.Option(min=1)] = None,
    batch_size: Annotated[int | None, typer.Option(min=1)] = None,
    resume: bool = typer.Option(False, "--resume"),
    config_path: Path = Path("config/poc.yaml"),
) -> None:
    config = load_poc_config(config_path)
    client = _build_client(config)
    try:
        with session_scope() as session:
            result = TopicService(client=client, config=config).build(
                session,
                plan_id=_uuid(plan_id),
                max_events=max_events,
                batch_size=batch_size,
                resume=resume,
            )
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    except TrackInsightError as error:
        raise typer.Exit(code=1) from _echo_error(error)
    finally:
        client.close()


@app.command("topic-status")
def topic_plan_status(plan_id: str) -> None:
    with session_scope() as session:
        result = topic_status(session, _uuid(plan_id))
    typer.echo(result.model_dump_json(indent=2))


@app.command("topic-list")
def topic_list(
    plan_id: str,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 50,
    with_events: bool = typer.Option(False, "--with-events"),
) -> None:
    with session_scope() as session:
        result = list_topics(session, _uuid(plan_id), limit=limit, with_events=with_events)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("topic-show")
def topic_show(topic_id: str) -> None:
    with session_scope() as session:
        result = show_topic(session, _uuid(topic_id))
    if result is None:
        typer.echo("Topic not found", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("track-build")
def track_build(
    plan_id: str,
    max_topics: Annotated[int | None, typer.Option(min=1)] = None,
    config_path: Path = Path("config/poc.yaml"),
) -> None:
    config = load_poc_config(config_path)
    client = _build_client(config)
    try:
        with session_scope() as session:
            result = TrackService(client=client, config=config).build(
                session, plan_id=_uuid(plan_id), max_topics=max_topics
            )
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    except TrackInsightError as error:
        raise typer.Exit(code=1) from _echo_error(error)
    finally:
        client.close()


@app.command("track-analyze")
def track_analyze(
    plan_id: str,
    limit: Annotated[int | None, typer.Option(min=1)] = None,
    config_path: Path = Path("config/poc.yaml"),
) -> None:
    config = load_poc_config(config_path)
    client = _build_client(config)
    try:
        with session_scope() as session:
            result = TrackService(client=client, config=config).analyze(
                session, plan_id=_uuid(plan_id), limit=limit
            )
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    except TrackInsightError as error:
        raise typer.Exit(code=1) from _echo_error(error)
    finally:
        client.close()


@app.command("track-status")
def track_plan_status(plan_id: str) -> None:
    with session_scope() as session:
        result = track_status(session, _uuid(plan_id))
    typer.echo(result.model_dump_json(indent=2))


@app.command("track-list")
def track_list(plan_id: str, limit: Annotated[int, typer.Option(min=1, max=500)] = 50) -> None:
    with session_scope() as session:
        result = list_tracks(session, _uuid(plan_id), limit=limit)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("track-show")
def track_show(track_id: str) -> None:
    with session_scope() as session:
        result = show_track(session, _uuid(track_id))
    if result is None:
        typer.echo("Candidate Track not found", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("dashboard-build")
def dashboard_build(
    plan_id: str,
    output: Annotated[Path, typer.Option("--output", "-o")] = Path(
        "reports/track_dashboard.html"
    ),
) -> None:
    try:
        with session_scope() as session:
            result = build_track_dashboard(session, _uuid(plan_id), output=output)
    except ValueError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("run-list")
def run_list(limit: Annotated[int, typer.Option(min=1, max=500)] = 20) -> None:
    with session_scope() as session:
        runs = list(
            session.scalars(
                select(PipelineRun).order_by(PipelineRun.created_at.desc()).limit(limit)
            )
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
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


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
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _run_event_extract_worker(config: PocConfig, limit: int) -> dict[str, int | str]:
    client = _build_client(config)
    try:
        return EventService(client=client, config=config).extract_work(limit=limit)
    finally:
        client.close()


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


def _build_optional_client(config: PocConfig) -> BailianClient | None:
    settings = get_settings()
    if settings.bailian_api_key is None or not settings.bailian_api_key.get_secret_value().strip():
        return None
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
