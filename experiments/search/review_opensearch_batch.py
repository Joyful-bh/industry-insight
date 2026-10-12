"""Replay a bounded OpenSearch batch from cached sources and export its audit."""

import argparse
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from track_insight.core.enums import TaskStatus
from track_insight.core.fingerprints import fingerprint
from track_insight.discovery.contracts import SearchReviewOutput
from track_insight.discovery.coverage import build_coverage_audit
from track_insight.discovery.search import apply_review_constraints
from track_insight.discovery.url_registry import register_candidate
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.infrastructure.bailian.contracts import WebSearchSource
from track_insight.infrastructure.database import session_scope
from track_insight.infrastructure.models import (
    ModelRun,
    PipelineRun,
    ResearchPlan,
    SearchResult,
    SearchTask,
    UrlCandidate,
    UrlDiscovery,
)
from track_insight.settings import get_settings, load_poc_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source_run")
    parser.add_argument(
        "--output", default="data/track_quality/opensearch_small_batch/run_1/results.json"
    )
    args = parser.parse_args()
    config = load_poc_config(Path("config/national.yaml"))
    prompt = config.prompts.search_review.read_text(encoding="utf-8")
    with session_scope() as session:
        original = session.get(PipelineRun, UUID(args.source_run))
        original_runs = session.scalars(
            select(ModelRun).where(ModelRun.pipeline_run_id == original.id)
        ).all()
        assert 1 <= len(original_runs) <= 6
        assert all(
            r.status == "completed"
            and r.raw_response["search_response"]["provider"] == "opensearch"
            for r in original_runs
        )
        plan_id = UUID(original.input_payload["plan_id"])
        replay = PipelineRun(
            run_type="search_review_replay",
            status=TaskStatus.RUNNING,
            input_payload={"source_run_id": str(original.id), "plan_id": str(plan_id)},
            started_at=datetime.now(UTC),
        )
        session.add(replay)
        session.flush()
        replay_id = replay.id
        run_ids = [r.id for r in original_runs]
        initial_usage = [r.usage for r in original_runs]
        initial_duration = (original.finished_at - original.started_at).total_seconds()

    def review(run_id):
        with session_scope() as session:
            original_run = session.get(ModelRun, run_id)
            task = session.scalar(select(SearchTask).where(SearchTask.model_run_id == run_id))
            assert task is not None and task.status == "completed"
            plan = session.get(ResearchPlan, plan_id)
            rows = session.scalars(
                select(SearchResult)
                .where(SearchResult.search_task_id == task.id)
                .order_by(SearchResult.refer)
            ).all()
            sources = [
                WebSearchSource(
                    refer=r.refer,
                    title=r.title,
                    link=r.url,
                    content=r.snippet,
                    media=r.media,
                    publish_date=r.publish_date,
                )
                for r in rows
            ]
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
                task_payload["start_date"], task_payload["end_date"] = dates.groups()
            before = Counter(r.decision for r in rows)
            settings = get_settings()
            client = BailianClient(
                api_key=settings.bailian_api_key.get_secret_value(),
                base_url=config.bailian.base_url,
                timeout_seconds=config.bailian.timeout_seconds,
            )
            started = datetime.now(UTC)
            try:
                result = client.review_search_sources(
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": json.dumps(task_payload, ensure_ascii=False)},
                    ],
                    output_model=SearchReviewOutput,
                    model=config.bailian.model,
                    review_batch_size=config.bailian.review_batch_size,
                    sources=sources,
                    search_response={"cached_from_model_run_id": str(run_id)},
                    initial_call_count=0,
                )
            finally:
                client.close()
            result.raw_response["review_constraints"] = apply_review_constraints(
                result.output, sources, task_payload
            )
            result.raw_response["review_input"] = task_payload
            new_run = ModelRun(
                pipeline_run_id=replay_id,
                task_type="search_review",
                status=TaskStatus.COMPLETED,
                provider="bailian",
                model=config.bailian.model,
                request_id=result.request_id,
                prompt_version=f"{config.prompts.search_review.stem}:{fingerprint(prompt)[:12]}",
                input_fingerprint=fingerprint(task_payload, prompt),
                tool_config={
                    "api": "opensearch_cached_sources",
                    "source_model_run_id": str(run_id),
                },
                raw_response=result.raw_response,
                structured_output=result.output.model_dump(mode="json"),
                usage=result.usage,
                started_at=started,
                finished_at=datetime.now(UTC),
            )
            session.add(new_run)
            session.flush()
            task.model_run_id = new_run.id
            judgments = {j.refer: j for j in result.output.judgments}
            for row in rows:
                j = judgments[row.refer]
                row.decision, row.url_type = j.decision.value, j.url_type.value
                row.source_class, row.reason = j.source_class.value, j.reason
                if row.decision in ("keep", "maybe"):
                    register_candidate(
                        session,
                        work_package_id=task.research_work_package_id,
                        search_task_id=task.id,
                        search_result=row,
                    )
                else:
                    # Revoke only this trial task's retained-candidate associations.
                    discoveries = session.scalars(
                        select(UrlDiscovery).where(
                            UrlDiscovery.search_task_id == task.id,
                            UrlDiscovery.search_result_id == row.id,
                        )
                    ).all()
                    for discovery in discoveries:
                        candidate_id = discovery.url_candidate_id
                        session.delete(discovery)
                        session.flush()
                        remaining = session.scalars(
                            select(SearchResult.decision)
                            .join(UrlDiscovery, UrlDiscovery.search_result_id == SearchResult.id)
                            .where(UrlDiscovery.url_candidate_id == candidate_id)
                        ).all()
                        candidate = session.get(UrlCandidate, candidate_id)
                        candidate.decision = (
                            "keep"
                            if "keep" in remaining
                            else ("maybe" if "maybe" in remaining else "drop")
                        )
            print(
                task.target_regions,
                dict(before),
                "->",
                dict(Counter(r.decision for r in rows)),
                flush=True,
            )
            return {
                "task_id": str(task.id),
                "query": task.query,
                "regions": task.target_regions,
                "queries": original_run.raw_response["search_response"]["queries"],
                "before_decisions": dict(before),
                "usage": result.usage,
                "corrections": result.raw_response["review_constraints"],
                "results": [
                    {
                        "refer": r.refer,
                        "title": r.title,
                        "url": r.url,
                        "snippet": r.snippet,
                        "publish_date": r.publish_date,
                        "decision": r.decision,
                        "url_type": r.url_type,
                        "source_class": r.source_class,
                        "reason": r.reason,
                    }
                    for r in rows
                ],
            }

    with ThreadPoolExecutor(max_workers=min(3, config.bailian.max_concurrency)) as pool:
        tasks = list(pool.map(review, run_ids))
    with session_scope() as session:
        audit = build_coverage_audit(session, plan_id, config)
        replay = session.get(PipelineRun, replay_id)
        replay.status = TaskStatus.COMPLETED
        replay.finished_at = datetime.now(UTC)
        replay.counters = {"reviewed_tasks": len(tasks), "failed_tasks": 0}
    output = {
        "plan_id": str(plan_id),
        "source_run_id": args.source_run,
        "replay_run_id": str(replay_id),
        "initial_duration_seconds": initial_duration,
        "initial_usage": initial_usage,
        "tasks": tasks,
        "coverage_audit": audit.model_dump(mode="json"),
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
