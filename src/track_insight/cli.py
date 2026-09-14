import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import select
from sqlalchemy.engine import make_url

from track_insight import __version__
from track_insight.acquisition import CollectionError, Collector, HtmlLinkAdapter, HttpFetcher
from track_insight.adapters import build_adapter, default_cutoff
from track_insight.blobstore import LocalBlobStore
from track_insight.config import get_settings
from track_insight.database import check_database, session_scope
from track_insight.enums import RunStatus
from track_insight.jobs import enqueue_job
from track_insight.logging import configure_logging
from track_insight.models import ConfigVersion, CrawlRun, Job, PipelineRun, Source
from track_insight.parse_worker import enqueue_parse_jobs, run_parse_jobs
from track_insight.readiness import readiness_lock, run_readiness_audit, write_readiness_reports
from track_insight.relevance_worker import enqueue_relevance_jobs, run_relevance_jobs
from track_insight.scope_config import load_collection_scope
from track_insight.source_config import load_source_definitions, sync_source_definitions

DEFAULT_SCOPE_PATH = Path("config/collection_scope.yaml")

app = typer.Typer(no_args_is_help=True, help="赛道洞察 POC 管理命令")


def _setup_logging() -> None:
    configure_logging(get_settings().log_level)


@app.callback()
def main() -> None:
    _setup_logging()


@app.command("version")
def version() -> None:
    typer.echo(__version__)


@app.command("config-show")
def config_show() -> None:
    settings = get_settings()
    url = make_url(settings.database_url).render_as_string(hide_password=True)
    typer.echo(
        json.dumps(
            {
                "database_url": url,
                "blob_root": str(settings.resolved_blob_root()),
                "log_level": settings.log_level,
                "job_lease_seconds": settings.job_lease_seconds,
                "llm_enabled": settings.llm_enabled(),
                "llm_base_url": settings.llm_base_url,
                "llm_model": settings.llm_model,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("db-check")
def db_check() -> None:
    check_database()
    typer.echo("database: ok")


@app.command("self-check")
def self_check(skip_database: bool = typer.Option(False, "--skip-database")) -> None:
    settings = get_settings()
    LocalBlobStore(settings.resolved_blob_root())
    typer.echo(f"blob root: {settings.resolved_blob_root()}")
    if not skip_database:
        check_database()
        typer.echo("database: ok")
    typer.echo("self-check: ok")


@app.command("blob-put")
def blob_put(path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    store = LocalBlobStore(get_settings().resolved_blob_root())
    blob = store.put_file(path)
    typer.echo(
        json.dumps(
            {
                "sha256": blob.sha256,
                "storage_key": blob.storage_key,
                "size_bytes": blob.size_bytes,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("source-validate")
def source_validate(directory: Path = Path("config/sources")) -> None:
    definitions = load_source_definitions(directory)
    for definition in definitions:
        typer.echo(f"{definition.code}\t{definition.fingerprint()}")
    typer.echo(f"validated sources: {len(definitions)}")


@app.command("source-sync")
def source_sync(directory: Path = Path("config/sources")) -> None:
    definitions = load_source_definitions(directory)
    with session_scope() as session:
        created, updated = sync_source_definitions(session, definitions)
    typer.echo(f"sources created: {created}, updated: {updated}")


@app.command("collect")
def collect(
    source_code: str,
    url_pattern: str,
    max_items: int = typer.Option(1, min=1, max=10),
    mode: str = typer.Option("incremental"),
) -> None:
    settings = get_settings()
    with session_scope() as session:
        source = session.scalar(select(Source).where(Source.code == source_code))
        if source is None:
            raise typer.BadParameter(f"unknown source code: {source_code}")
        if not source.enabled:
            raise typer.BadParameter(f"source is disabled: {source_code}")
        collector = Collector(LocalBlobStore(settings.resolved_blob_root()))
        result = collector.run(
            session,
            source,
            HtmlLinkAdapter(url_pattern, max_items=max_items),
            mode=mode,
        )
    typer.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2))


@app.command("collect-source")
def collect_source(
    source_code: str,
    max_pages: int = typer.Option(5, min=1, max=500),
    max_items: int = typer.Option(5000, min=1, max=50000),
    mode: str = typer.Option("backfill"),
    resume: bool = typer.Option(True, "--resume/--no-resume"),
    stop_after_known: int = typer.Option(20, min=0, max=1000),
    scope_path: Annotated[Path, typer.Option()] = DEFAULT_SCOPE_PATH,
) -> None:
    """使用已注册的站点薄适配器采集；传入 all 处理全部启用来源。"""
    settings = get_settings()
    scope = load_collection_scope(scope_path)
    cutoff = default_cutoff(scope)

    with session_scope() as session:
        if source_code == "all":
            enabled_sources = list(
                session.scalars(select(Source).where(Source.enabled.is_(True)).order_by(Source.code))
            )
            source_codes = [source.code for source in enabled_sources]
            source_versions = {
                source.code: source.config_version for source in enabled_sources
            }
        else:
            source = session.scalar(select(Source).where(Source.code == source_code))
            if source is None:
                raise typer.BadParameter(f"unknown source code: {source_code}")
            if not source.enabled:
                raise typer.BadParameter(f"source is disabled: {source_code}")
            source_codes = [source.code]
            source_versions = {source.code: source.config_version}

    pipeline_run_id = None
    start_index = 0
    batch_fingerprint = _collection_batch_fingerprint(
        source_codes,
        mode=mode,
        max_pages=max_pages,
        max_items=max_items,
    )
    resume_key = _collection_resume_key(
        mode=mode,
        max_pages=max_pages,
        max_items=max_items,
        stop_after_known=stop_after_known,
        scope_fingerprint=scope.fingerprint(),
    )
    completed_sources: dict[str, Any] = {}
    if source_code == "all":
        with session_scope() as session:
            previous_batch = session.scalar(
                select(PipelineRun)
                .where(
                    PipelineRun.run_type == "all_sources_collection",
                    PipelineRun.status == RunStatus.RUNNING,
                )
                .order_by(PipelineRun.created_at.desc())
                .limit(1)
            )
            if (
                resume
                and previous_batch
                and previous_batch.input_config.get("fingerprint") == batch_fingerprint
            ):
                pipeline_run_id = previous_batch.id
                cutoff = datetime.fromisoformat(previous_batch.input_config["cutoff"])
                completed_sources = dict(previous_batch.counters.get("completed_sources", {}))
                if not completed_sources:
                    completed_sources = _migrate_legacy_completed_sources(
                        session, previous_batch, source_versions
                    )
                    if completed_sources:
                        previous_batch.counters = {
                            **previous_batch.counters,
                            "completed_sources": completed_sources,
                            "legacy_completion_migrated": True,
                        }
                start_index = min(
                    int(previous_batch.counters.get("next_source_index", 0)), len(source_codes)
                )
            else:
                compatible_batch = None
                if resume:
                    candidates = list(
                        session.scalars(
                            select(PipelineRun)
                            .where(
                                PipelineRun.run_type == "all_sources_collection",
                                PipelineRun.status == RunStatus.RUNNING,
                            )
                            .order_by(PipelineRun.created_at.desc())
                            .limit(50)
                        )
                    )
                    compatible_batch = next(
                        (
                            item
                            for item in candidates
                            if (
                                item.input_config.get("resume_key") == resume_key
                                and item.counters.get("completed_sources")
                            )
                            or _legacy_batch_parameters_match(
                                item,
                                mode=mode,
                                max_pages=max_pages,
                                max_items=max_items,
                                stop_after_known=stop_after_known,
                            )
                        ),
                        None,
                    )
                if compatible_batch is not None:
                    cutoff = datetime.fromisoformat(compatible_batch.input_config["cutoff"])
                    completed_sources = dict(
                        compatible_batch.counters.get("completed_sources", {})
                    )
                    if not completed_sources:
                        completed_sources = _migrate_legacy_completed_sources(
                            session, compatible_batch, source_versions
                        )
                        if completed_sources:
                            compatible_batch.counters = {
                                **compatible_batch.counters,
                                "completed_sources": completed_sources,
                                "legacy_completion_migrated": True,
                            }
                batch = PipelineRun(
                    run_type="all_sources_collection",
                    status=RunStatus.RUNNING,
                    as_of=datetime.now(UTC),
                    started_at=datetime.now(UTC),
                    input_config={
                        "fingerprint": batch_fingerprint,
                        "resume_key": resume_key,
                        "mode": mode,
                        "cutoff": cutoff.isoformat(),
                        "max_pages": max_pages,
                        "max_items": max_items,
                        "source_codes": source_codes,
                        "source_versions": source_versions,
                    },
                    counters={
                        "next_source_index": 0,
                        "total_sources": len(source_codes),
                        "completed_sources": completed_sources,
                    },
                )
                session.add(batch)
                session.flush()
                pipeline_run_id = batch.id

    if start_index:
        typer.echo(
            f"resuming all-source batch at [{start_index + 1}/{len(source_codes)}] "
            f"{source_codes[start_index]}",
            err=True,
        )

    outputs: list[dict[str, Any]] = []
    skipped_sources: list[str] = []
    iteration_start = start_index if not completed_sources else 0
    for index, code in enumerate(source_codes[iteration_start:], start=iteration_start + 1):
        if _source_completion_matches(completed_sources, code, source_versions[code]):
            typer.echo(f"[{index}/{len(source_codes)}] skipping completed {code}", err=True)
            skipped_sources.append(code)
            continue
        typer.echo(f"[{index}/{len(source_codes)}] collecting {code}", err=True)
        output = _collect_configured_source(
            code,
            settings=settings,
            scope=scope,
            cutoff=cutoff,
            max_pages=max_pages,
            max_items=max_items,
            mode=mode,
            resume=resume,
            stop_after_known=stop_after_known,
            pipeline_run_id=pipeline_run_id,
        )
        outputs.append(output)
        typer.echo(json.dumps(output, ensure_ascii=False))
        if pipeline_run_id is not None:
            with session_scope() as session:
                batch = session.get(PipelineRun, pipeline_run_id)
                if batch is not None:
                    saved_completed = dict(batch.counters.get("completed_sources", {}))
                    if output.get("status") == "completed":
                        saved_completed[code] = {
                            "config_version": source_versions[code],
                            "completed_at": datetime.now(UTC).isoformat(),
                        }
                    batch.counters = {
                        **batch.counters,
                        "next_source_index": index,
                        "last_source": code,
                        "completed_sources": saved_completed,
                    }
                    completed_sources = saved_completed

    if source_code == "all":
        if pipeline_run_id is not None:
            with session_scope() as session:
                batch = session.get(PipelineRun, pipeline_run_id)
                if batch is not None:
                    batch.status = RunStatus.SUCCEEDED
                    batch.finished_at = datetime.now(UTC)
        summary = {
            "sources": len(source_codes),
            "resumed_from_index": start_index,
            "skipped_completed_sources": len(skipped_sources),
            "completed": sum(item["status"] == "completed" for item in outputs),
            "discovered": sum(item.get("discovered", 0) for item in outputs),
            "fetched": sum(item.get("fetched", 0) for item in outputs),
            "new_versions": sum(item.get("new_versions", 0) for item in outputs),
            "unchanged": sum(item.get("unchanged", 0) for item in outputs),
            "failed_documents": sum(item.get("failed", 0) for item in outputs),
            "cutoff": cutoff.isoformat(),
        }
        typer.echo(json.dumps({"summary": summary}, ensure_ascii=False, indent=2))


def _collect_configured_source(
    source_code: str,
    *,
    settings: Any,
    scope: Any,
    cutoff: Any,
    max_pages: int,
    max_items: int,
    mode: str,
    resume: bool,
    stop_after_known: int,
    pipeline_run_id: Any = None,
) -> dict[str, Any]:
    cursor = None
    totals = {
        "discovered": 0,
        "fetched": 0,
        "new_versions": 0,
        "unchanged": 0,
        "failed": 0,
    }
    crawl_run_id = None
    pages_done = 0
    last_cursor = None
    while pages_done < max_pages and totals["discovered"] < max_items:
        with session_scope() as session:
            source = session.scalar(select(Source).where(Source.code == source_code))
            if source is None or not source.enabled:
                return {"source": source_code, "status": "unavailable"}
            if pages_done == 0 and resume:
                previous = session.scalar(
                    select(CrawlRun)
                    .where(CrawlRun.source_id == source.id, CrawlRun.mode == mode)
                    .order_by(CrawlRun.created_at.desc())
                    .limit(1)
                )
                if (
                    previous
                    and previous.cursor_after
                    and not previous.cursor_after.get("complete")
                ):
                    cursor = previous.cursor_after
            remaining_items = max_items - totals["discovered"]
            adapter = build_adapter(source, scope, max_pages=1, max_items=remaining_items)
            collector = Collector(LocalBlobStore(settings.resolved_blob_root()))
            effective_known_stop = stop_after_known if mode == "incremental" else 0
            result = collector.run(
                session,
                source,
                adapter,
                mode=mode,
                cutoff=cutoff,
                cursor=cursor,
                stop_after_known=effective_known_stop,
                pipeline_run_id=pipeline_run_id,
                progress=lambda report: typer.echo(
                    json.dumps(
                        {"source": source_code, "page_progress": report}, ensure_ascii=False
                    ),
                    err=True,
                ),
            )
        crawl_run_id = result.crawl_run_id
        for key in totals:
            totals[key] += getattr(result, key)
        pages_done += 1
        last_cursor = result.cursor
        if not last_cursor or last_cursor.get("complete"):
            break
        if last_cursor == cursor:
            break
        cursor = last_cursor
    return {
        "source": source_code,
        "status": "completed",
        "crawl_run_id": crawl_run_id,
        **totals,
        "cursor": last_cursor,
        "pages_committed": pages_done,
        "cutoff": cutoff.isoformat(),
    }


def _collection_batch_fingerprint(
    source_codes: list[str], *, mode: str, max_pages: int, max_items: int
) -> str:
    payload = json.dumps(
        {
            "source_codes": source_codes,
            "mode": mode,
            "max_pages": max_pages,
            "max_items": max_items,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _collection_resume_key(
    *,
    mode: str,
    max_pages: int,
    max_items: int,
    stop_after_known: int,
    scope_fingerprint: str,
) -> str:
    """Identify compatible collection work independently of the enabled source list."""
    payload = json.dumps(
        {
            "mode": mode,
            "max_pages": max_pages,
            "max_items": max_items,
            "stop_after_known": stop_after_known,
            "scope_fingerprint": scope_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _source_completion_matches(
    completed_sources: dict[str, Any], source_code: str, config_version: str
) -> bool:
    completion = completed_sources.get(source_code)
    return isinstance(completion, dict) and completion.get("config_version") == config_version


def _legacy_batch_parameters_match(
    batch: PipelineRun,
    *,
    mode: str,
    max_pages: int,
    max_items: int,
    stop_after_known: int,
) -> bool:
    config = batch.input_config
    if config.get("resume_key"):
        return False
    if mode != "backfill" or stop_after_known != 20:
        return False
    return (
        config.get("mode") == mode
        and config.get("max_pages") == max_pages
        and config.get("max_items") == max_items
        and isinstance(config.get("source_codes"), list)
        and bool(config.get("cutoff"))
    )


def _migrate_legacy_completed_sources(
    session: Any,
    batch: PipelineRun,
    current_source_versions: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Recover the completed prefix of a pre-source-checkpoint collection batch."""
    old_source_codes = batch.input_config.get("source_codes")
    if not isinstance(old_source_codes, list):
        return {}
    completed_count = min(
        max(int(batch.counters.get("next_source_index", 0)), 0), len(old_source_codes)
    )
    migrated: dict[str, dict[str, str]] = {}
    for code in old_source_codes[:completed_count]:
        config_version = current_source_versions.get(code)
        if config_version is None:
            continue
        version_created_at = session.scalar(
            select(ConfigVersion.created_at).where(
                ConfigVersion.kind == "source",
                ConfigVersion.fingerprint == config_version,
            )
        )
        if version_created_at is None or version_created_at > batch.created_at:
            continue
        migrated[code] = {
            "config_version": config_version,
            "completed_at": batch.created_at.isoformat(),
            "migrated_from_legacy_batch": str(batch.id),
        }
    return migrated


@app.command("adapter-audit")
def adapter_audit(
    source_code: str = typer.Argument("all"),
    max_pages: int = typer.Option(3, min=1, max=5),
    scope_path: Annotated[Path, typer.Option()] = DEFAULT_SCOPE_PATH,
) -> None:
    """低频读取真实列表页，检查适配器发现、翻页和时间边界。"""
    scope = load_collection_scope(scope_path)
    cutoff = default_cutoff(scope)
    with session_scope() as session:
        query = select(Source).where(Source.enabled.is_(True)).order_by(Source.code)
        if source_code != "all":
            query = query.where(Source.code == source_code)
        sources = session.scalars(query).all()
        if not sources:
            raise typer.BadParameter(f"unknown source code: {source_code}")
        for source in sources:
            adapter = build_adapter(source, scope, max_pages=max_pages, max_items=5000)
            try:
                batch = adapter.discover(
                    source,
                    HttpFetcher(source),
                    cutoff=cutoff,
                    progress=lambda report, code=source.code: typer.echo(
                        json.dumps({"source": code, "page": report}, ensure_ascii=False), err=True
                    ),
                )
                result = {
                    "source": source.code,
                    "status": "ok" if not batch.failures else "partial",
                    "pages": len(batch.page_reports),
                    "items": len(batch.items),
                    "dated_items": sum(item.published_at is not None for item in batch.items),
                    "failures": batch.failures,
                    "cursor": batch.next_cursor,
                }
            except CollectionError as error:
                result = {"source": source.code, "status": "failed", "error": str(error)}
            typer.echo(json.dumps(result, ensure_ascii=False))


@app.command("source-readiness-audit")
def source_readiness_audit(
    max_pages: int = typer.Option(3, min=1, max=5),
    max_documents: int = typer.Option(3, min=1, max=5),
    max_attachments: int = typer.Option(2, min=0, max=5),
    output_dir: Annotated[Path, typer.Option()] = Path("reports"),
    scope_path: Annotated[Path, typer.Option()] = DEFAULT_SCOPE_PATH,
) -> None:
    """Run a bounded real collection test for every enabled source."""
    with readiness_lock():
        report = run_readiness_audit(
            load_collection_scope(scope_path),
            max_pages=max_pages,
            max_documents=max_documents,
            max_attachments=max_attachments,
            progress=lambda item, current, total: typer.echo(
                f"[{current}/{total}] {item.code}: {item.status} "
                f"(fetched={item.fetched}, failed={item.failed})",
                err=True,
            ),
        )
    json_path, markdown_path = write_readiness_reports(report, output_dir)
    typer.echo(json.dumps(report["summary"], ensure_ascii=False))
    typer.echo(f"JSON: {json_path}")
    typer.echo(f"Markdown: {markdown_path}")


@app.command("scope-validate")
def scope_validate(path: Path = Path("config/collection_scope.yaml")) -> None:
    scope = load_collection_scope(path)
    typer.echo(
        json.dumps(
            {
                "version": scope.version,
                "target_regions": [region.model_dump() for region in scope.target_regions],
                "backfill_months": scope.backfill_months,
                "document_formats": sorted(scope.document_formats),
                "fingerprint": scope.fingerprint(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("job-enqueue")
def job_enqueue(
    job_type: str,
    object_type: str,
    object_id: str,
    input_fingerprint: str,
    processor_version: str,
    priority: int = 100,
) -> None:
    with session_scope() as session:
        job = enqueue_job(
            session,
            job_type=job_type,
            object_type=object_type,
            object_id=object_id,
            input_fingerprint=input_fingerprint,
            processor_version=processor_version,
            priority=priority,
        )
        typer.echo(str(job.id))


@app.command("job-list")
def job_list(limit: int = typer.Option(20, min=1, max=500)) -> None:
    with session_scope() as session:
        jobs = session.scalars(select(Job).order_by(Job.created_at.desc()).limit(limit)).all()
        for job in jobs:
            typer.echo(
                f"{job.id}\t{job.status.value}\t{job.job_type}\t{job.object_type}:{job.object_id}"
            )


@app.command("parse-work")
def parse_work(
    limit: int = typer.Option(20, min=1, max=1000),
    worker_id: str | None = typer.Option(None),
) -> None:
    result = run_parse_jobs(get_settings(), limit=limit, worker_id=worker_id)
    typer.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2))


@app.command("parse-enqueue")
def parse_enqueue(limit: int = typer.Option(1000, min=1, max=100000)) -> None:
    count = enqueue_parse_jobs(limit=limit)
    typer.echo(f"parse jobs enqueued: {count}")


@app.command("relevance-enqueue")
def relevance_enqueue(
    limit: int = typer.Option(10000, min=1, max=100000),
    rules_path: Annotated[Path, typer.Option()] = Path("config/relevance_rules.yaml"),
) -> None:
    count = enqueue_relevance_jobs(rules_path, limit=limit, settings=get_settings())
    typer.echo(f"relevance jobs enqueued: {count}")


@app.command("relevance-work")
def relevance_work(
    limit: int = typer.Option(500, min=1, max=10000),
    worker_id: str | None = typer.Option(None),
    rules_path: Annotated[Path, typer.Option()] = Path("config/relevance_rules.yaml"),
) -> None:
    settings = get_settings()
    result = run_relevance_jobs(
        limit=limit,
        lease_seconds=settings.job_lease_seconds,
        rules_path=rules_path,
        worker_id=worker_id,
        settings=settings,
    )
    typer.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2))


@app.command("relevance-status")
def relevance_status() -> None:
    from sqlalchemy import func

    from track_insight.models import DocumentRelevanceAssessment

    with session_scope() as session:
        rows = session.execute(
            select(DocumentRelevanceAssessment.label, func.count())
            .group_by(DocumentRelevanceAssessment.label)
            .order_by(DocumentRelevanceAssessment.label)
        ).all()
    typer.echo(json.dumps({label.value: count for label, count in rows}, ensure_ascii=False))


if __name__ == "__main__":
    app()
