"""Bounded main-service search test and inspection using existing page parsers."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from track_insight.content.fetcher import PageFetcher
from track_insight.content.quality import assess_text_quality
from track_insight.content.service import PageService
from track_insight.discovery.search import SearchService
from track_insight.infrastructure.bailian.client import BailianClient, _response_output_text
from track_insight.infrastructure.database import session_scope
from track_insight.infrastructure.models import (
    ModelRun,
    ResearchPlan,
    ResearchWorkPackage,
    SearchResult,
    SearchTask,
)
from track_insight.planning.contracts import SearchTaskProposal, WorkPackageProposal
from track_insight.planning.service import PlanningService
from track_insight.settings import get_settings, load_poc_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="data/track_quality/cited_search_smoke/run_1")
    args = parser.parse_args()
    config = load_poc_config(Path("config/national.yaml"))
    assert config.search.provider == "bailian" and config.search.selection == "citations"
    root = Path(args.output_dir)
    root.mkdir(parents=True, exist_ok=True)
    seeds = [
        "00cf2b85-4b3b-4b22-87df-2755f66b1408",
        "164f4d18-631d-43e4-aeff-c3117a2649fa",
        "21bb6d67-0c22-4c06-91b9-6500ac279adf",
    ]
    plans = []
    with session_scope() as session:
        for seed in seeds:
            original = session.get(SearchTask, UUID(seed))
            task = SearchTaskProposal(
                **{
                    key: getattr(original, key)
                    for key in [
                        "query",
                        "purpose",
                        "target_regions",
                        "target_industries",
                        "target_signal_types",
                        "target_source_classes",
                    ]
                }
            )
            plan = ResearchPlan(
                status="completed",
                config_version=config.version,
                regions=task.target_regions,
                industry_scopes=config.scope.industry_scopes,
                start_date=config.scope.start_date,
                end_date=config.scope.end_date,
            )
            session.add(plan)
            session.flush()
            PlanningService(config=config)._persist_packages(
                session,
                plan.id,
                [
                    WorkPackageProposal(
                        objective=task.purpose,
                        regions=task.target_regions,
                        industry_scopes=task.target_industries,
                        signal_types=task.target_signal_types,
                        source_classes=task.target_source_classes,
                        search_tasks=[task],
                    )
                ],
            )
            plans.append(str(plan.id))

    def status(value):
        (root / "status.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    status({"status": "running", "started_at": datetime.now(UTC).isoformat(), "plan_ids": plans})

    def search(plan_id):
        settings = get_settings()
        client = BailianClient(
            api_key=settings.bailian_api_key.get_secret_value(),
            base_url=config.bailian.base_url,
            timeout_seconds=config.bailian.timeout_seconds,
        )
        try:
            with session_scope() as session:
                summary = SearchService(client=client, config=config).run(
                    session, plan_id=UUID(plan_id), max_work_packages=1, max_searches=1
                )
                task = session.scalar(
                    select(SearchTask)
                    .join(ResearchWorkPackage)
                    .where(ResearchWorkPackage.research_plan_id == UUID(plan_id))
                )
                run = session.get(ModelRun, task.model_run_id)
                payload = {
                    "plan_id": plan_id,
                    "query": task.query,
                    "status": task.status,
                    "summary": summary.model_dump(mode="json"),
                    "usage": run.usage,
                    "raw_response": run.raw_response,
                    "error": task.error_message,
                }
                rows = list(
                    session.scalars(
                        select(SearchResult).where(SearchResult.search_task_id == task.id)
                    )
                )
                payload["sources"] = [
                    {"refer": r.refer, "url": r.url, "selected": r.decision == "keep"} for r in rows
                ]
                raw = run.raw_response or {}
                response = raw.get("search_retry_response") or raw.get("search_response") or {}
                (root / f"{plan_id}_summary.txt").write_text(
                    _response_output_text(response) or "", encoding="utf-8"
                )
                (root / f"{plan_id}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(
                    task.query,
                    task.status,
                    "raw",
                    len(rows),
                    "cited",
                    sum(r.decision == "keep" for r in rows),
                    flush=True,
                )
                return payload
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=3) as pool:
        searches = list(pool.map(search, plans))
    if any(s["status"] != "completed" for s in searches):
        status({"status": "failed", "searches": searches})
        raise RuntimeError("A main-service search failed; see saved diagnostics")
    selected = [
        {**r, "query": s["query"], "plan_id": s["plan_id"]}
        for s in searches
        for r in s["sources"]
        if r["selected"]
    ]

    def fetch(row):
        fetcher = PageFetcher(
            timeout_seconds=20,
            max_retries=0,
            max_response_bytes=config.stage2.max_response_bytes,
            min_interval_per_host_seconds=1,
        )
        try:
            result = fetcher.fetch(row["url"])
            parsed = PageService(config=config)._parse_fetch_result(result)
            text = parsed.get("content_text", "")
            quality, reasons = assess_text_quality(
                text,
                min_chars=config.stage2.min_usable_text_chars,
                pdf=result.content_type == "application/pdf",
            )
            row.update(
                title=parsed.get("title"),
                date=str(parsed.get("published_at") or ""),
                text=text,
                text_length=len(text),
                quality=str(quality),
                quality_reasons=reasons,
            )
        except Exception as error:
            row["error"] = f"{type(error).__name__}: {error}"
        finally:
            fetcher.close()
        return row

    with ThreadPoolExecutor(max_workers=6) as pool:
        pages = list(pool.map(fetch, selected))
    result = {
        "status": "completed",
        "plan_ids": plans,
        "searches": searches,
        "pages": pages,
        "independent_review_calls": 0,
        "paid_page_extractor_calls": 0,
        "note": "真实搜索经主服务入库并登记候选；正文检查复用下载清洗组件，不消费其他队列。",
    }
    (root / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    status({"status": "completed", "plan_ids": plans, "selected_urls": len(pages)})
    print(root / "results.json")


if __name__ == "__main__":
    main()
