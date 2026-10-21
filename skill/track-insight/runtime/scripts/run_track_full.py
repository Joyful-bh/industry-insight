"""Run the production Track build with call-driven progress and draft export."""

import argparse
import json
import logging
import math
import os
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select, text

from scripts.event_stage_supervisor import client, save
from track_insight.core.fingerprints import fingerprint
from track_insight.infrastructure.database import get_engine, session_scope
from track_insight.infrastructure.models import (
    CandidateTrack,
    CandidateTrackTopic,
    Topic,
    TrackBuildRun,
)
from track_insight.settings import load_poc_config
from track_insight.tracks.service import TrackService, close_interrupted_build

logger = logging.getLogger(__name__)


def best_effort(action, *args, **kwargs):
    """Optional monitoring files must not interrupt model calls or database commits."""
    try:
        return action(*args, **kwargs)
    except OSError as error:
        logger.warning("Optional monitoring write failed: %s", error)
        return None


def append_call(path, row):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def usage_total(raw):
    recovery = raw.get("repair_response", raw.get("recovery_response"))
    responses = (
        [raw["initial_response"], recovery]
        if "initial_response" in raw and recovery is not None
        else [raw]
    )
    return {
        key: sum(r.get("usage", {}).get(key, 0) for r in responses)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }


def export_tracks(session, run_id):
    tracks = list(
        session.scalars(
            select(CandidateTrack)
            .where(CandidateTrack.updated_by_run_id == run_id)
            .order_by(CandidateTrack.name, CandidateTrack.id)
        )
    )
    memberships = defaultdict(lambda: defaultdict(list))
    for row in session.scalars(
        select(CandidateTrackTopic).where(CandidateTrackTopic.track_build_run_id == run_id)
    ):
        memberships[row.candidate_track_id][row.role].append(str(row.topic_id))
    return [
        dict(
            id=str(t.id),
            name=t.name,
            definition=t.definition,
            offering_family=t.core_products_services[0] if t.core_products_services else None,
            supplier_role=t.chain_roles[0] if t.chain_roles else None,
            topic_ids=sorted(memberships[t.id]["core"]),
            supporting_topic_ids=sorted(memberships[t.id]["supporting"]),
            evidence_event_ids=t.supporting_event_ids,
            regions=t.regions,
            status=t.status,
            confidence=t.confidence,
        )
        for t in tracks
    ]


class RecordingService(TrackService):
    def __init__(self, *, record, output, **kwargs):
        super().__init__(**kwargs)
        self.record = record
        self.output = output

    def _build_semantic_groups(self, session, pipeline, *args, **kwargs):
        run = session.scalar(
            select(TrackBuildRun).where(TrackBuildRun.pipeline_run_id == pipeline.id)
        )
        self.record(
            stage="preparing_and_embedding",
            track_build_run_id=str(run.id),
            pipeline_run_id=str(pipeline.id),
        )
        result, stats = super()._build_semantic_groups(session, pipeline, *args, **kwargs)
        best_effort(save, self.output / "candidate_tracks.json", result.model_dump(mode="json"))
        self.record(stage="candidates_ready", grouping=stats, candidate_count=len(result.tracks))
        return result, stats

    def _recover_supporting_topics(self, session, pipeline, output, *args, **kwargs):
        self.record(stage="support_recovery")
        stats = super()._recover_supporting_topics(session, pipeline, output, *args, **kwargs)
        best_effort(save, self.output / "supported_tracks.json", output.model_dump(mode="json"))
        self.record(support_recovery=stats)
        return stats

    def _define_tracks(self, session, pipeline, output, *args, **kwargs):
        self.record(stage="definitions", definition_batches=math.ceil(len(output.tracks) / 8))
        super()._define_tracks(session, pipeline, output, *args, **kwargs)
        best_effort(save, self.output / "defined_tracks.json", output.model_dump(mode="json"))

    def _normalize_markets(self, session, pipeline, output, *args, **kwargs):
        self.record(stage="market_normalization", candidate_count=len(output.tracks))
        result = super()._normalize_markets(session, pipeline, output, *args, **kwargs)
        best_effort(save, self.output / "merged_tracks.json", output.model_dump(mode="json"))
        self.record(stage="persisting", market_normalization=result)
        return result


def monitor_client(active, config, output, state, record):
    structured = active.complete_structured
    embed = active.embed_texts
    progress_record = record

    def record(**kwargs):
        return best_effort(progress_record, **kwargs)

    task_by_prompt = {
        getattr(config.stage4.prompts, name).read_text(encoding="utf-8"): name
        for name in (
            "track_generation",
            "track_definition",
            "track_support_recovery",
            "track_market_catalog",
            "track_market_audit",
        )
    }

    def log_call(task, usage, requests, status, **extra):
        row = dict(
            task=task,
            usage=usage,
            requests=requests,
            status=status,
            finished_at=datetime.now(UTC).isoformat(),
            **extra,
        )
        best_effort(append_call, output / "calls.jsonl", row)
        if task == "embedding":
            state["embedding_calls_completed"] += status == "completed"
            state["embedding_tokens"] += usage.get("total_tokens", 0)
        else:
            state["structured_requests"] += requests
            for key, value in usage.items():
                state["structured_usage"][key] += value
            if status == "completed":
                state["completed_calls_by_task"][task] = (
                    state["completed_calls_by_task"].get(task, 0) + 1
                )
        record(last_call=row, active_call=None)

    def complete(**kwargs):
        task = task_by_prompt.get(kwargs["messages"][0]["content"], "structured")
        if (
            task == "track_market_catalog"
            and getattr(config.stage4, "market_merge_strategy", "global") == "global"
        ):
            # A larger ceiling avoids truncation; billing uses actual generated tokens.
            kwargs["max_tokens"] = max(kwargs["max_tokens"], 65536)
        record(
            stage=task,
            active_call=dict(
                task=task,
                started_at=datetime.now(UTC).isoformat(),
                max_output_tokens=kwargs["max_tokens"],
            ),
        )
        try:
            result = structured(**kwargs)
        except Exception as exc:
            diagnostics = getattr(exc, "diagnostics", {})
            if diagnostics:
                best_effort(save, output / "failure_diagnostics.json", diagnostics)
                raw = (
                    diagnostics
                    if "initial_response" in diagnostics
                    else diagnostics.get("response", {})
                )
                recovery = "recovery_response" in raw or "repair_response" in raw
                log_call(task, usage_total(raw), 2 if recovery else 1, "failed")
            raise
        log_call(task, usage_total(result.raw_response), result.call_count, "completed")
        return result

    def embedding(texts, *, model):
        record(
            stage="embedding",
            active_call=dict(
                task="embedding", texts=len(texts), started_at=datetime.now(UTC).isoformat()
            ),
        )
        result = embed(texts, model=model)
        log_call("embedding", result["usage"], 1, "completed")
        return result

    active.complete_structured = complete
    active.embed_texts = embedding


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-id", required=True, type=uuid.UUID)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if (output / "status.json").exists():
        raise RuntimeError("Use a new output directory; production candidate batches can be reused")
    config = load_poc_config(Path("config/national.yaml"))
    state = dict(
        status="running",
        stage="preflight",
        plan_id=str(args.plan_id),
        pid=os.getpid(),
        started_at=datetime.now(UTC).isoformat(),
        model=config.stage4.model,
        embedding_model=config.stage4.grouping_embedding_model,
        embedding_calls_completed=0,
        embedding_tokens=0,
        structured_requests=0,
        structured_usage={k: 0 for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
        completed_calls_by_task={},
        detailed_analysis_started=False,
    )

    def record(**extra):
        state.update(extra)
        state["updated_at"] = datetime.now(UTC).isoformat()
        best_effort(save, output / "status.json", state)

    record()
    with get_engine().connect() as guard:
        acquired = guard.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:key))"),
            {"key": f"track-full:{args.plan_id}"},
        )
        if not acquired:
            record(status="needs_human", reason="Another full Track runner holds the plan lock")
            return
        active = None
        try:
            with session_scope() as session:
                running = session.scalar(
                    select(TrackBuildRun).where(
                        TrackBuildRun.research_plan_id == args.plan_id,
                        TrackBuildRun.status == "running",
                    )
                )
                if running:
                    raise RuntimeError("A Track build is already running; inspect before resuming")
                count = session.scalar(
                    select(func.count())
                    .select_from(Topic)
                    .where(
                        Topic.research_plan_id == args.plan_id,
                        Topic.status == "candidate",
                    )
                )
            if not count:
                raise RuntimeError("No candidate Topics")
            save(
                output / "manifest.json",
                dict(
                    plan_id=str(args.plan_id),
                    input_topic_count=count,
                    config=config.model_dump(mode="json"),
                    prompt_fingerprints={
                        name: fingerprint(
                            getattr(config.stage4.prompts, name).read_text(encoding="utf-8")
                        )
                        for name in (
                            "track_generation",
                            "track_definition",
                            "track_support_recovery",
                            "track_market_catalog",
                            "track_market_audit",
                        )
                    },
                    operational_override={"market_catalog_max_output_tokens": 65536},
                    experimental_identity_selection=False,
                ),
            )
            record(topic_count=count)
            active = client(config)
            monitor_client(active, config, output, state, record)
            service = RecordingService(client=active, config=config, record=record, output=output)
            with session_scope() as session:
                result = service.build(session, plan_id=args.plan_id)
            record(stage="exporting", result=result)
            with session_scope() as session:
                tracks = export_tracks(session, uuid.UUID(result["track_build_run_id"]))
            if len(tracks) != result["track_count"]:
                raise RuntimeError("Persisted Track count differs from build result")
            save(output / "tracks.json", tracks)
            save(
                output / "summary.json",
                dict(
                    result=result,
                    supplier_role_counts=dict(Counter(t["supplier_role"] for t in tracks)),
                    structured_usage=state["structured_usage"],
                    embedding_tokens=state["embedding_tokens"],
                    quality_status="draft_not_reviewed",
                    detailed_analysis_started=False,
                ),
            )
            record(
                status="completed",
                stage="completed",
                track_count=len(tracks),
                tracks_file=str(output / "tracks.json"),
                finished_at=datetime.now(UTC).isoformat(),
            )
        except (Exception, KeyboardInterrupt) as exc:
            if state.get("track_build_run_id") and state.get("pipeline_run_id"):
                try:
                    with session_scope() as session:
                        close_interrupted_build(
                            session,
                            uuid.UUID(state["track_build_run_id"]),
                            uuid.UUID(state["pipeline_run_id"]),
                            exc,
                        )
                except Exception:
                    logger.exception("Could not reconcile failed Track runner")
            record(
                status="needs_human",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=datetime.now(UTC).isoformat(),
            )
            raise
        finally:
            if active is not None:
                active.close()
            guard.execute(
                text("SELECT pg_advisory_unlock(hashtext(:key))"),
                {"key": f"track-full:{args.plan_id}"},
            )


if __name__ == "__main__":
    main()
