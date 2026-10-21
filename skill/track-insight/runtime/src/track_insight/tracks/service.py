import json
import logging
import re
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from track_insight.core.enums import TaskStatus
from track_insight.core.errors import ContractError, ModelCallError
from track_insight.core.fingerprints import fingerprint
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.infrastructure.events import record_event
from track_insight.infrastructure.jobs import reconcile_stale_pipeline_runs
from track_insight.infrastructure.models import (
    CandidateTrack,
    CandidateTrackAnalysis,
    CandidateTrackTopic,
    Event,
    EventEvidence,
    ModelRun,
    PageAnalysis,
    PageCapture,
    PipelineRun,
    ResearchPlan,
    ResearchWorkPackage,
    Topic,
    TopicEvent,
    TrackBuildRun,
    UrlDiscovery,
)
from track_insight.settings import PocConfig
from track_insight.tracks.bounded_markets import (
    CLUSTER_PARTITION_VERSION,
    comparison_packages,
    identity_text,
    merge_packages,
    merge_packages_direct,
)
from track_insight.tracks.contracts import (
    IdentifiedCandidateTrackOutput,
    IdentifiedTrackBuildOutput,
    SmbAssociationOutput,
    TrackBuildOutput,
    TrackDefinitionOutput,
    TrackStatus,
)
from track_insight.tracks.evidence_strength import SCORING_VERSION, recalculate_evidence_strength
from track_insight.tracks.markets import market_output_model, normalize_markets
from track_insight.tracks.preparation import (
    cached_embeddings,
    coalesce_compatible_names,
    event_ref,
    expand_observation_output,
    group_output_model,
    prepare_observations,
    semantic_groups,
)
from track_insight.tracks.support import apply_support_recovery, support_recovery_model

TRACK_PROCESSOR_VERSION = "topic-track-v21-evidence-backed-groups"
ANALYSIS_PROCESSOR_VERSION = "track-analysis-v6-smb-associations"
logger = logging.getLogger(__name__)

NON_TRACK_IDENTITY_TERMS = (
    "政策支持",
    "财政资金",
    "税收优惠",
    "加计扣除",
    "留抵退税",
    "购置补贴",
    "项目支持",
    "上市培育",
    "人才培训",
    "技能大师",
    "基金投资",
    "REITs试点",
    "平稳运行支持",
    "盈利改善",
    "融资升温",
    "人才培养",
    "产业人才",
    "产业空间",
    "产业集群",
    "集群与产业链",
    "智慧城市",
    "首台套",
    "首批次",
    "首流片",
)


class TrackService:
    def __init__(self, *, client: BailianClient, config: PocConfig) -> None:
        self.client = client
        self.config = config

    def build(
        self, session: Session, *, plan_id: uuid.UUID, max_topics: int | None = None
    ) -> dict[str, Any]:
        reconcile_stale_pipeline_runs(session)
        total_topic_count = int(
            session.scalar(
                select(func.count(Topic.id)).where(
                    Topic.research_plan_id == plan_id, Topic.status == "candidate"
                )
            )
            or 0
        )
        statement = (
            select(Topic)
            .where(Topic.research_plan_id == plan_id, Topic.status == "candidate")
            .order_by(Topic.label, Topic.id)
        )
        if max_topics is not None:
            statement = statement.limit(max_topics)
        topics = list(session.scalars(statement))
        if not topics:
            raise ContractError("no candidate Topics found for the research plan")
        # Preserve the input numbering of an unchanged previous pool for batch cache reuse.
        previous = session.scalar(
            select(TrackBuildRun)
            .where(
                TrackBuildRun.research_plan_id == plan_id,
                TrackBuildRun.processor_version == TRACK_PROCESSOR_VERSION,
                TrackBuildRun.topic_count == len(topics),
            )
            .order_by(TrackBuildRun.created_at.desc())
            .limit(1)
        )
        by_id = {str(topic.id): topic for topic in topics}
        if (
            previous
            and len(previous.topic_ids) == len(topics)
            and set(previous.topic_ids) == set(by_id)
        ):
            topics = [by_id[tid] for tid in previous.topic_ids]
        plan = session.get(ResearchPlan, plan_id)
        if plan is None:
            raise ContractError("research plan not found")
        scope = _track_scope_payload(plan, self.config)
        prompt = self.config.stage4.prompts.track_generation.read_text(encoding="utf-8")
        prompt_version = _prompt_version(self.config.stage4.prompts.track_generation, prompt)
        definition_path = self.config.stage4.prompts.track_definition
        definition_prompt = definition_path.read_text(encoding="utf-8")
        definition_version = _prompt_version(definition_path, definition_prompt)
        support_path = self.config.stage4.prompts.track_support_recovery
        support_prompt = support_path.read_text(encoding="utf-8")
        support_version = _prompt_version(support_path, support_prompt)
        build_prompt_version = f"{prompt_version}|{definition_version}|{support_version}"
        market_prompts = {
            name: path.read_text(encoding="utf-8")
            for name, path in {
                "catalog": self.config.stage4.prompts.track_market_catalog,
                "audit": self.config.stage4.prompts.track_market_audit,
            }.items()
        }
        build_prompt_version += "|markets:" + fingerprint(market_prompts)[:12]
        build_prompt_version += "|evidence:" + SCORING_VERSION
        input_fp = fingerprint(
            [(str(x.id), x.topic_fingerprint) for x in topics],
            build_prompt_version,
            self.config.stage4.model,
            self.config.stage4.minimum_supporting_events,
            scope,
            self.config.stage4.grouping_embedding_model,
            self.config.stage4.generation_batch_topics,
            self.config.stage4.generation_batch_chars,
            self.config.stage4.market_merge_strategy,
            CLUSTER_PARTITION_VERSION
            if self.config.stage4.market_merge_strategy == "bounded_direct"
            else "star-v1",
        )
        existing_run = session.scalar(
            select(TrackBuildRun).where(
                TrackBuildRun.research_plan_id == plan_id,
                TrackBuildRun.input_fingerprint == input_fp,
                TrackBuildRun.processor_version == TRACK_PROCESSOR_VERSION,
            )
        )
        if existing_run and existing_run.status == TaskStatus.COMPLETED:
            return _build_payload(existing_run, resumed=True)
        pipeline = PipelineRun(
            run_type="topic_to_track",
            status=TaskStatus.RUNNING,
            input_payload={
                "plan_id": str(plan_id),
                "topic_count": len(topics),
                "scope": scope,
                "prompt_version": build_prompt_version,
            },
            started_at=datetime.now(UTC),
        )
        session.add(pipeline)
        session.flush()
        run = existing_run or TrackBuildRun(
            research_plan_id=plan_id,
            pipeline_run_id=pipeline.id,
            status=TaskStatus.RUNNING,
            input_fingerprint=input_fp,
            processor_version=TRACK_PROCESSOR_VERSION,
            prompt_version=_stored_build_prompt_version(build_prompt_version),
            model=self.config.stage4.model,
            topic_ids=[str(x.id) for x in topics],
            topic_count=len(topics),
            started_at=datetime.now(UTC),
        )
        if existing_run:
            run.pipeline_run_id = pipeline.id
            run.status = TaskStatus.RUNNING
            run.error_code = None
            run.error_message = None
            run.finished_at = None
        else:
            session.add(run)
        model_run = _new_model_run(
            pipeline.id, "track_generation", self.config.stage4.model, prompt_version, input_fp
        )
        session.add(model_run)
        session.flush()
        run.model_run_id = model_run.id
        record_event(
            session,
            event_type="track.run.started",
            message="Topic-to-Track run started",
            pipeline_run_id=pipeline.id,
            model_run_id=model_run.id,
            entity_type="track_build_run",
            entity_id=run.id,
            details=pipeline.input_payload,
        )
        session.commit()
        build_run_id, pipeline_id = run.id, pipeline.id
        try:
            topic_refs = {f"T{index}": topic.id for index, topic in enumerate(topics, 1)}
            try:
                if len(topics) > self.config.stage4.generation_batch_topics:
                    output, fallback_stats = self._build_semantic_groups(
                        session,
                        pipeline,
                        topics,
                        topic_refs,
                        scope,
                        prompt,
                        prompt_version,
                    )
                    model_run.status = TaskStatus.COMPLETED
                    model_run.structured_output = output.model_dump(mode="json")
                    model_run.finished_at = datetime.now(UTC)
                    build_mode = "semantic_groups"
                else:
                    result = self._call_track_model(
                        session=session,
                        prompt=prompt,
                        topics=topics,
                        topic_refs=topic_refs,
                        scope=scope,
                    )
                    _complete_model_run(model_run, result)
                    identified_tracks = _combine_exact_track_identities(result.output.tracks)
                    output = _expand_topic_refs(
                        session,
                        IdentifiedTrackBuildOutput(tracks=identified_tracks),
                        topic_refs,
                        {topic.id: topic for topic in topics},
                    )
                    build_mode = "global"
                    fallback_stats = None
            except (ModelCallError, ContractError) as global_error:
                if len(topics) > self.config.stage4.generation_batch_topics:
                    raise
                _fail_model_run_only(model_run, global_error)
                record_event(
                    session,
                    event_type="track.fallback.started",
                    message="Global Track generation failed; fallback started",
                    level="WARNING",
                    pipeline_run_id=pipeline.id,
                    model_run_id=model_run.id,
                    entity_type="track_build_run",
                    entity_id=run.id,
                    details={
                        "error_code": getattr(global_error, "code", "track_contract_error"),
                        "error_message": str(global_error),
                    },
                )
                session.commit()
                output, fallback_model_run_id, fallback_stats = self._build_with_fallback(
                    session=session,
                    pipeline=pipeline,
                    run=run,
                    topics=topics,
                    topic_refs=topic_refs,
                    generation_prompt=prompt,
                    generation_prompt_version=prompt_version,
                    scope=scope,
                )
                if fallback_model_run_id is not None:
                    run.model_run_id = fallback_model_run_id
                build_mode = "fallback"

            support_stats = self._recover_supporting_topics(
                session, pipeline, output, topics, scope, support_prompt, support_version
            )
            adjustments = _normalize_build_output(session, output, topics)
            if adjustments:
                record_event(
                    session,
                    event_type="track.output.normalized",
                    message="Candidate Track output normalized",
                    level="WARNING",
                    pipeline_run_id=pipeline.id,
                    model_run_id=model_run.id,
                    entity_type="track_build_run",
                    entity_id=run.id,
                    details={"adjustments": adjustments},
                )
            _validate_build(session, output, topics, self.config)
            self._define_tracks(
                session,
                pipeline,
                output,
                topics,
                definition_prompt,
                definition_version,
                scope,
            )
            market_stats = self._normalize_markets(session, pipeline, output, scope, market_prompts)
            recalculate_evidence_strength(session, output.tracks)
            _validate_build(
                session,
                output,
                topics,
                self.config,
                allow_shared_market_evidence=(
                    self.config.stage4.market_merge_strategy == "bounded_direct"
                ),
            )
            self._persist_tracks(
                session,
                run,
                topics,
                output,
                archive_stale=len(topics) == total_topic_count,
            )
        except (ModelCallError, ContractError) as error:
            _fail(model_run, run, pipeline, error)
            record_event(
                session,
                event_type="track.run.failed",
                message="Topic-to-Track run failed",
                level="ERROR",
                pipeline_run_id=pipeline.id,
                model_run_id=model_run.id,
                entity_type="track_build_run",
                entity_id=run.id,
                details={"error_code": run.error_code, "error_message": str(error)},
            )
            session.commit()
            raise
        except (Exception, KeyboardInterrupt) as error:
            try:
                close_interrupted_build(session, build_run_id, pipeline_id, error)
                session.commit()
            except Exception:
                logger.exception("Could not record interrupted Track build %s", build_run_id)
            raise
        assigned = {
            topic_id
            for track in output.tracks
            for topic_id in [*track.topic_ids, *track.supporting_topic_ids]
        }
        run.track_count = len(output.tracks)
        run.unassigned_topic_count = len(set(x.id for x in topics) - assigned)
        run.status = TaskStatus.COMPLETED
        run.finished_at = datetime.now(UTC)
        pipeline.status = TaskStatus.COMPLETED
        pipeline.finished_at = datetime.now(UTC)
        pipeline.counters = {
            "topics": len(topics),
            "tracks": run.track_count,
            "unassigned_topics": run.unassigned_topic_count,
            "build_mode": build_mode,
            "fallback": fallback_stats,
            "market_normalization": market_stats,
            "support_recovery": support_stats,
        }
        record_event(
            session,
            event_type="track.run.completed",
            message="Topic-to-Track run completed",
            pipeline_run_id=pipeline.id,
            model_run_id=run.model_run_id,
            entity_type="track_build_run",
            entity_id=run.id,
            details=pipeline.counters,
        )
        session.commit()
        return _build_payload(run, resumed=False)

    def _build_semantic_groups(
        self,
        session,
        pipeline,
        topics,
        topic_refs,
        scope,
        prompt,
        prompt_version,
    ):
        observations = prepare_observations(session, topics)
        vectors, embedding_usage = cached_embeddings(
            observations,
            self.client,
            self.config.stage4.grouping_embedding_model,
        )
        observations, vectors = coalesce_compatible_names(observations, vectors)
        groups = semantic_groups(
            observations,
            vectors,
            max_topics=self.config.stage4.generation_batch_topics,
            max_chars=self.config.stage4.generation_batch_chars,
        )
        tracks = []
        for group in groups:
            payload = {"scope": scope, "topics": [o.payload for o in group]}
            input_fp = fingerprint(payload, prompt_version, self.config.stage4.model)
            cached = session.scalar(
                select(ModelRun)
                .where(
                    ModelRun.task_type == "track_generation_semantic_group",
                    ModelRun.input_fingerprint == input_fp,
                    ModelRun.status == TaskStatus.COMPLETED,
                )
                .order_by(ModelRun.created_at.desc())
                .limit(1)
            )
            if cached:
                result_output = IdentifiedTrackBuildOutput.model_validate(cached.structured_output)
            else:
                child = _new_model_run(
                    pipeline.id,
                    "track_generation_semantic_group",
                    self.config.stage4.model,
                    prompt_version,
                    input_fp,
                )
                session.add(child)
                session.commit()
                try:
                    result = self.client.complete_structured(
                        messages=[
                            {"role": "system", "content": prompt},
                            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                        ],
                        output_model=group_output_model(group),
                        model=self.config.stage4.model,
                        max_tokens=self.config.stage4.generation_max_output_tokens,
                        enable_thinking=False,
                    )
                    expand_observation_output(result.output, group, topic_refs)
                    _complete_model_run(child, result)
                    session.commit()
                    result_output = result.output
                except (ModelCallError, ContractError) as error:
                    _fail_model_run_only(child, error)
                    session.commit()
                    raise
            expanded = expand_observation_output(result_output, group, topic_refs)
            tracks.extend(expanded.tracks)
        output = _expand_topic_refs(
            session,
            IdentifiedTrackBuildOutput(tracks=_combine_exact_track_identities(tracks)),
            topic_refs,
            {topic.id: topic for topic in topics},
        )
        return output, {
            "input_topics": len(topics),
            "compact_observations": len(observations),
            "group_count": len(groups),
            "embedding_usage": embedding_usage,
        }

    def _normalize_markets(self, session, pipeline, output, scope, prompts):
        strategy = getattr(self.config.stage4, "market_merge_strategy", "global")
        bounded = strategy in {"bounded", "bounded_direct"}

        def complete(task, payload, schema):
            prompt = prompts["catalog" if task == "direct_catalog" else task]
            version = "market:" + fingerprint(prompt)[:12]
            return self._complete_cached_track_task(
                session,
                pipeline,
                "track_market_" + task,
                prompt,
                version,
                payload,
                market_output_model(task, payload, schema),
                min(self.config.stage4.generation_max_output_tokens, 8192)
                if bounded
                else self.config.stage4.generation_max_output_tokens,
            )

        if bounded:
            if len(output.tracks) < 2:
                return {
                    "input_tracks": len(output.tracks),
                    "output_tracks": len(output.tracks),
                    "merged_groups": 0,
                }
            vectors, usage = cached_embeddings(
                [SimpleNamespace(embedding_text=identity_text(track)) for track in output.tracks],
                self.client,
                self.config.stage4.grouping_embedding_model,
            )
            packages, recall = comparison_packages(
                output.tracks, vectors,
                partitioning="cluster" if strategy == "bounded_direct" else "star",
            )
            merge = merge_packages_direct if strategy == "bounded_direct" else merge_packages
            output.tracks, stats = merge(output.tracks, scope, packages, complete)
            stats.pop("merge_groups", None)
            stats["recall"] = {key: value for key, value in recall.items() if key != "edges"}
            stats["identity_embedding_usage"] = usage
            return stats
        output.tracks, stats = normalize_markets(output.tracks, scope, complete)
        return stats

    def _complete_cached_track_task(
        self, session, pipeline, task, prompt, version, payload, schema, max_tokens
    ):
        input_fp = fingerprint(payload, version)
        cached = session.scalar(
            select(ModelRun)
            .where(
                ModelRun.task_type == task,
                ModelRun.input_fingerprint == input_fp,
                ModelRun.model == self.config.stage4.model,
                ModelRun.status == TaskStatus.COMPLETED,
            )
            .order_by(ModelRun.created_at.desc())
            .limit(1)
        )
        if cached:
            try:
                return schema.model_validate(cached.structured_output)
            except ValueError:
                logger.warning("Cached %s output failed current validation; calling again", task)
        child = _new_model_run(pipeline.id, task, self.config.stage4.model, version, input_fp)
        session.add(child)
        session.commit()
        try:
            result = self.client.complete_structured(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                output_model=schema,
                model=self.config.stage4.model,
                max_tokens=max_tokens,
                enable_thinking=False,
            )
            _complete_model_run(child, result)
            session.commit()
            return result.output
        except (ModelCallError, ContractError) as error:
            _fail_model_run_only(child, error)
            session.commit()
            raise

    def _recover_supporting_topics(
        self,
        session: Session,
        pipeline: PipelineRun,
        output: TrackBuildOutput,
        topics: list[Topic],
        scope: dict[str, Any],
        prompt: str,
        prompt_version: str,
    ) -> dict:
        refs = {f"T{i}": topic.id for i, topic in enumerate(topics)}
        reverse = {value: key for key, value in refs.items()}
        compact = IdentifiedTrackBuildOutput(
            tracks=[
                IdentifiedCandidateTrackOutput(
                    name=track.name,
                    offering_family=track.offering_family,
                    supplier_role=track.supplier_role,
                    refs=[reverse[x] for x in track.topic_ids],
                    supporting_refs=[reverse[x] for x in track.supporting_topic_ids],
                    core_event_refs=[event_ref(x) for x in track.identity_event_ids],
                )
                for track in output.tracks
            ]
        )
        used = {ref for track in compact.tracks for ref in [*track.refs, *track.supporting_refs]}
        pending = [topic for topic in topics if reverse[topic.id] not in used]
        if not compact.tracks:
            return {"batches": 0, "skipped_batches": 0}
        skipped_batches = 0
        for start in range(0, len(pending), 24):
            batch = pending[start : start + 24]
            payload = {
                "scope": scope,
                "tracks": [
                    dict(
                        track_ref=f"R{i}",
                        name=track.name,
                        offering_family=track.offering_family,
                        supplier_role=track.supplier_role,
                    )
                    for i, track in enumerate(compact.tracks)
                ],
                "topics": [
                    _topic_payload(session, topic, topic_ref=reverse[topic.id]) for topic in batch
                ],
            }
            expected = {reverse[x.id] for x in batch}
            try:
                recovery = self._complete_cached_track_task(
                    session,
                    pipeline,
                    "track_support_recovery",
                    prompt,
                    prompt_version,
                    payload,
                    support_recovery_model(expected, {f"R{i}" for i in range(len(compact.tracks))}),
                    4096,
                )
                proposed = compact.model_copy(deep=True)
                apply_support_recovery(proposed, recovery, expected, payload["topics"])
                compact = proposed
            except (ModelCallError, ContractError) as error:
                if (
                    isinstance(error, ModelCallError)
                    and error.code != "invalid_model_output_after_recovery"
                ):
                    raise
                # Optional evidence is omitted on failure, never fabricated to satisfy coverage.
                record_event(
                    session,
                    event_type="track.support_recovery.skipped",
                    level="WARNING",
                    message="Optional support batch not applied",
                    pipeline_run_id=pipeline.id,
                    details={"topic_refs": sorted(expected), "error": str(error)},
                )
                session.commit()
                logger.warning("Skipped optional support batch %s: %s", start // 24, error)
                skipped_batches += 1
            session.commit()
        for track, recovered in zip(output.tracks, compact.tracks, strict=True):
            track.supporting_topic_ids = [refs[x] for x in recovered.supporting_refs]
        return {"batches": (len(pending) + 23) // 24, "skipped_batches": skipped_batches}

    def _define_tracks(
        self,
        session: Session,
        pipeline: PipelineRun,
        output: TrackBuildOutput,
        topics: list[Topic],
        prompt: str,
        prompt_version: str,
        scope: dict[str, Any],
    ) -> None:
        topic_map = {topic.id: topic for topic in topics}
        for start in range(0, len(output.tracks), 8):
            batch = output.tracks[start : start + 8]
            refs = {f"R{start + index}": track for index, track in enumerate(batch)}
            payload = {
                "scope": scope,
                "tracks": [
                    {
                        "track_ref": ref,
                        "name": track.name,
                        "topics": [
                            _topic_payload(
                                session,
                                topic_map[x],
                                identity_event_ids=track.identity_event_ids or None,
                            )
                            for x in track.topic_ids
                        ],
                    }
                    for ref, track in refs.items()
                ],
            }
            definitions = self._complete_cached_track_task(
                session,
                pipeline,
                "track_definition",
                prompt,
                prompt_version,
                payload,
                _definition_output_model(refs),
                min(4096, self.config.stage4.generation_max_output_tokens),
            )
            _apply_definitions(refs, definitions)

    def _call_track_model(
        self,
        *,
        session: Session,
        prompt: str,
        topics: list[Topic],
        topic_refs: dict[str, uuid.UUID],
        scope: dict[str, Any],
    ):
        ref_by_topic_id = {topic_id: ref for ref, topic_id in topic_refs.items()}
        result = self.client.complete_structured(
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "scope": scope,
                            "topics": [
                                _topic_payload(
                                    session,
                                    topic,
                                    topic_ref=ref_by_topic_id[topic.id],
                                )
                                for topic in topics
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            output_model=IdentifiedTrackBuildOutput,
            model=self.config.stage4.model,
            max_tokens=self.config.stage4.generation_max_output_tokens,
            enable_thinking=False,
        )
        _validate_generation_reference_coverage(result.output, set(topic_refs))
        return result

    def _build_with_fallback(
        self,
        *,
        session: Session,
        pipeline: PipelineRun,
        run: TrackBuildRun,
        topics: list[Topic],
        topic_refs: dict[str, uuid.UUID],
        generation_prompt: str,
        generation_prompt_version: str,
        scope: dict[str, Any],
    ) -> tuple[TrackBuildOutput, uuid.UUID | None, dict[str, Any]]:
        groups = _topic_work_package_groups(session, run.research_plan_id, topics)
        completed_local_outputs = []
        completed_model_run_id = None
        completed_groups = 0
        failed_groups = 0
        for group_name, group_topics in groups:
            group_ref_map = {
                ref: topic_id
                for ref, topic_id in topic_refs.items()
                if topic_id in {topic.id for topic in group_topics}
            }
            group_fp = fingerprint(
                group_name,
                [(str(topic.id), topic.topic_fingerprint) for topic in group_topics],
                generation_prompt_version,
                scope,
            )
            local_model_run = _new_model_run(
                pipeline.id,
                "track_generation_fallback_local",
                self.config.stage4.model,
                generation_prompt_version,
                group_fp,
            )
            session.add(local_model_run)
            session.flush()
            try:
                local_result = self._call_track_model(
                    session=session,
                    prompt=generation_prompt,
                    topics=group_topics,
                    topic_refs=group_ref_map,
                    scope=scope,
                )
                _complete_model_run(local_model_run, local_result)
                completed_local_outputs.extend(local_result.output.tracks)
                completed_model_run_id = local_model_run.id
                completed_groups += 1
                record_event(
                    session,
                    event_type="track.fallback.local.completed",
                    message="Local Track fallback group completed",
                    pipeline_run_id=pipeline.id,
                    model_run_id=local_model_run.id,
                    entity_type="track_build_run",
                    entity_id=run.id,
                    details={
                        "group": group_name,
                        "topic_count": len(group_topics),
                        "track_count": len(local_result.output.tracks),
                    },
                )
            except (ModelCallError, ContractError) as error:
                failed_groups += 1
                _fail_model_run_only(local_model_run, error)
                record_event(
                    session,
                    event_type="track.fallback.local.failed",
                    message="Local Track fallback group failed and was skipped",
                    level="WARNING",
                    pipeline_run_id=pipeline.id,
                    model_run_id=local_model_run.id,
                    entity_type="track_build_run",
                    entity_id=run.id,
                    details={"group": group_name, "error_message": str(error)},
                )
            session.commit()

        local_tracks = _combine_exact_track_identities(completed_local_outputs)
        if not local_tracks:
            raise ContractError("Track fallback produced no usable local Tracks")

        return (
            _expand_topic_refs(
                session,
                IdentifiedTrackBuildOutput(tracks=local_tracks),
                topic_refs,
                {topic.id: topic for topic in topics},
            ),
            completed_model_run_id,
            {
                "group_count": len(groups),
                "completed_groups": completed_groups,
                "failed_groups": failed_groups,
                "local_track_count": len(local_tracks),
                "consolidation": "exact_identity_only",
            },
        )

    def _persist_tracks(
        self,
        session: Session,
        run: TrackBuildRun,
        topics: list[Topic],
        output: TrackBuildOutput,
        *,
        archive_stale: bool,
    ) -> None:
        topic_map = {x.id: x for x in topics}
        membership_count = Counter(tid for proposed in output.tracks for tid in proposed.topic_ids)
        touched: set[uuid.UUID] = set()
        for proposed in output.tracks:
            member_topic_ids = proposed.topic_ids
            supporting_topic_ids = proposed.supporting_topic_ids
            all_topic_ids = list(dict.fromkeys([*member_topic_ids, *supporting_topic_ids]))
            supporting_event_ids = sorted(_event_ids_for_topic_ids(session, all_topic_ids), key=str)
            shared_ids = [tid for tid in member_topic_ids if membership_count[tid] > 1]
            if shared_ids:
                shared_events = _event_ids_for_topic_ids(session, shared_ids)
                unshared_events = _event_ids_for_topic_ids(
                    session, [tid for tid in all_topic_ids if tid not in shared_ids]
                )
                supporting_event_ids = sorted(
                    (set(supporting_event_ids) - shared_events)
                    | unshared_events
                    | set(proposed.identity_event_ids),
                    key=str,
                )
            identity_fp = fingerprint(
                re.sub(r"\s+", "", proposed.offering_family).casefold(),
                proposed.supplier_role,
            )
            canonical_key = f"track_{identity_fp[:20]}"
            track = session.scalar(
                select(CandidateTrack).where(
                    CandidateTrack.research_plan_id == run.research_plan_id,
                    CandidateTrack.canonical_key == canonical_key,
                )
            )
            fp = fingerprint(
                canonical_key,
                proposed.definition,
                supporting_event_ids,
            )
            values = dict(
                name=proposed.name,
                aliases=[],
                definition=proposed.definition,
                enterprise_archetype=proposed.definition,
                core_products_services=[proposed.offering_family],
                core_company_types=[],
                supporting_company_types=[],
                shared_demand_drivers=[],
                included_activities=[],
                excluded_activities=[],
                chain_roles=[proposed.supplier_role],
                observable_company_features=[],
                possible_it_needs=[],
                supporting_event_ids=[str(x) for x in supporting_event_ids],
                granularity_assessment={},
                regions=_ordered_union(topic_map[x].regions for x in all_topic_ids),
                confidence=proposed.confidence,
                status=proposed.status,
                track_fingerprint=fp,
                updated_by_run_id=run.id,
            )
            if track is None:
                track = CandidateTrack(
                    research_plan_id=run.research_plan_id,
                    created_by_run_id=run.id,
                    canonical_key=canonical_key,
                    **values,
                )
                session.add(track)
                session.flush()
            else:
                for key, value in values.items():
                    setattr(track, key, value)
            touched.add(track.id)
            session.execute(
                delete(CandidateTrackTopic).where(
                    CandidateTrackTopic.candidate_track_id == track.id
                )
            )
            for topic_id in member_topic_ids:
                session.add(
                    CandidateTrackTopic(
                        candidate_track_id=track.id,
                        topic_id=topic_id,
                        track_build_run_id=run.id,
                        role="core",
                        relevance_score=proposed.confidence,
                        reason="Direct Track grouping",
                    )
                )
            for topic_id in supporting_topic_ids:
                session.add(
                    CandidateTrackTopic(
                        candidate_track_id=track.id,
                        topic_id=topic_id,
                        track_build_run_id=run.id,
                        role="supporting",
                        relevance_score=proposed.confidence,
                        reason="Supporting Track signal",
                    )
                )
        if archive_stale:
            for stale in session.scalars(
                select(CandidateTrack).where(
                    CandidateTrack.research_plan_id == run.research_plan_id,
                    CandidateTrack.status.in_(["candidate", "watchlist"]),
                    CandidateTrack.id.not_in(touched),
                )
            ):
                stale.status = "archived"

    def analyze(
        self, session: Session, *, plan_id: uuid.UUID, limit: int | None = None
    ) -> dict[str, Any]:
        smb_catalog = _load_smb_product_catalog(self.config.stage4.smb_product_catalog)
        reconcile_stale_pipeline_runs(session)
        statement = (
            select(CandidateTrack)
            .where(
                CandidateTrack.research_plan_id == plan_id,
                CandidateTrack.status.in_(["candidate", "watchlist"]),
            )
            .order_by(CandidateTrack.name)
        )
        if limit is not None:
            statement = statement.limit(limit)
        tracks = list(session.scalars(statement))
        prompt = self.config.stage4.prompts.track_analysis.read_text(encoding="utf-8")
        prompt_version = _prompt_version(self.config.stage4.prompts.track_analysis, prompt)
        completed = failed = skipped = 0
        for track in tracks:
            try:
                payload = _smb_analysis_input(track, smb_catalog)
                input_fp = fingerprint(
                    payload, prompt_version, self.config.stage4.model
                )
                old = session.scalar(
                    select(CandidateTrackAnalysis).where(
                        CandidateTrackAnalysis.candidate_track_id == track.id,
                        CandidateTrackAnalysis.input_fingerprint == input_fp,
                        CandidateTrackAnalysis.processor_version == ANALYSIS_PROCESSOR_VERSION,
                    )
                )
            except (ContractError, KeyError, TypeError, ValueError) as error:
                failed += 1
                record_event(
                    session,
                    event_type="track.analysis.skipped",
                    message="Candidate Track analysis input was invalid and skipped",
                    level="WARNING",
                    entity_type="candidate_track",
                    entity_id=track.id,
                    details={"track_name": track.name, "error_message": str(error)},
                )
                session.commit()
                continue
            if old and old.status == TaskStatus.COMPLETED:
                skipped += 1
                continue
            pipeline = PipelineRun(
                run_type="track_analysis",
                status=TaskStatus.RUNNING,
                input_payload={"track_id": str(track.id)},
                started_at=datetime.now(UTC),
            )
            session.add(pipeline)
            session.flush()
            analysis = old or CandidateTrackAnalysis(
                candidate_track_id=track.id,
                pipeline_run_id=pipeline.id,
                status=TaskStatus.RUNNING,
                input_fingerprint=input_fp,
                processor_version=ANALYSIS_PROCESSOR_VERSION,
                prompt_version=prompt_version,
            )
            if old:
                analysis.pipeline_run_id = pipeline.id
                analysis.status = TaskStatus.RUNNING
            else:
                session.add(analysis)
            model_run = _new_model_run(
                pipeline.id, "track_analysis", self.config.stage4.model, prompt_version, input_fp
            )
            session.add(model_run)
            session.flush()
            analysis.model_run_id = model_run.id
            record_event(
                session,
                event_type="track.analysis.started",
                message="Candidate Track analysis started",
                pipeline_run_id=pipeline.id,
                model_run_id=model_run.id,
                entity_type="candidate_track",
                entity_id=track.id,
                details={"track_name": track.name},
            )
            session.commit()
            try:
                result = self.client.complete_structured(
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    output_model=SmbAssociationOutput,
                    model=self.config.stage4.model,
                    max_tokens=self.config.stage4.analysis_max_output_tokens,
                    enable_thinking=False,
                )
                _complete_model_run(model_run, result)
                analysis.smb_value_analysis = [
                    x.model_dump(mode="json") for x in result.output.smb_value_analysis
                ]
                analysis.uncertainties = [
                    "基于赛道经营范围推导的潜在关联，不代表已确认需求或采购意向。"
                ]
                analysis.status = TaskStatus.COMPLETED
                analysis.finished_at = datetime.now(UTC)
                pipeline.status = TaskStatus.COMPLETED
                pipeline.finished_at = datetime.now(UTC)
                record_event(
                    session,
                    event_type="track.analysis.completed",
                    message="Candidate Track analysis completed",
                    pipeline_run_id=pipeline.id,
                    model_run_id=model_run.id,
                    entity_type="candidate_track",
                    entity_id=track.id,
                    details={"association_count": len(analysis.smb_value_analysis)},
                )
                completed += 1
            except (ModelCallError, ContractError) as error:
                model_run.status = (
                    TaskStatus.FAILED_RETRYABLE
                    if isinstance(error, ModelCallError) and error.retryable
                    else TaskStatus.FAILED_TERMINAL
                )
                model_run.error_code = getattr(error, "code", "track_analysis_contract_error")
                model_run.error_message = str(error)
                if isinstance(error, ModelCallError):
                    (
                        model_run.request_id,
                        model_run.usage,
                        model_run.raw_response,
                    ) = error.response_metadata()
                model_run.finished_at = datetime.now(UTC)
                analysis.status = (
                    TaskStatus.FAILED_RETRYABLE
                    if isinstance(error, ModelCallError) and error.retryable
                    else TaskStatus.FAILED_TERMINAL
                )
                analysis.error_code = getattr(error, "code", "track_analysis_contract_error")
                analysis.error_message = str(error)
                pipeline.status = analysis.status
                pipeline.finished_at = datetime.now(UTC)
                record_event(
                    session,
                    event_type="track.analysis.failed",
                    message="Candidate Track analysis failed",
                    level="ERROR",
                    pipeline_run_id=pipeline.id,
                    model_run_id=model_run.id,
                    entity_type="candidate_track",
                    entity_id=track.id,
                    details={"error_code": analysis.error_code, "error_message": str(error)},
                )
                failed += 1
            session.commit()
        return {
            "plan_id": str(plan_id),
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
        }


def track_status(session: Session, plan_id: uuid.UUID) -> TrackStatus:
    topic_count = int(
        session.scalar(
            select(func.count(Topic.id)).where(
                Topic.research_plan_id == plan_id, Topic.status == "candidate"
            )
        )
        or 0
    )
    track_count = int(
        session.scalar(
            select(func.count(CandidateTrack.id)).where(
                CandidateTrack.research_plan_id == plan_id,
                CandidateTrack.status.in_(["candidate", "watchlist"]),
            )
        )
        or 0
    )
    assigned = int(
        session.scalar(
            select(func.count(func.distinct(CandidateTrackTopic.topic_id)))
            .join(CandidateTrack, CandidateTrack.id == CandidateTrackTopic.candidate_track_id)
            .where(
                CandidateTrack.research_plan_id == plan_id,
                CandidateTrack.status.in_(["candidate", "watchlist"]),
            )
        )
        or 0
    )
    analyzed = int(
        session.scalar(
            select(func.count(func.distinct(CandidateTrackAnalysis.candidate_track_id)))
            .join(CandidateTrack, CandidateTrack.id == CandidateTrackAnalysis.candidate_track_id)
            .where(
                CandidateTrack.research_plan_id == plan_id,
                CandidateTrack.status.in_(["candidate", "watchlist"]),
                CandidateTrackAnalysis.status == TaskStatus.COMPLETED,
            )
        )
        or 0
    )
    return TrackStatus(
        plan_id=str(plan_id),
        track_count=track_count,
        analyzed_count=analyzed,
        topic_count=topic_count,
        assigned_topic_count=assigned,
    )


def _topic_payload(
    session: Session,
    topic: Topic,
    *,
    topic_ref: str | None = None,
    identity_event_ids: list[uuid.UUID] | None = None,
) -> dict[str, Any]:
    events = list(
        session.scalars(
            select(Event)
            .join(TopicEvent, TopicEvent.event_id == Event.id)
            .where(TopicEvent.topic_id == topic.id)
            .where(Event.id.in_(identity_event_ids) if identity_event_ids is not None else True)
            .order_by(Event.signal_date.desc().nulls_last())
            .limit(12)
        )
    )
    evidence = (
        list(
            session.scalars(
                select(EventEvidence).where(EventEvidence.event_id.in_([x.id for x in events]))
            )
        )
        if events
        else []
    )
    quote_by_event: dict[uuid.UUID, str] = {}
    for item in evidence:
        quote_by_event.setdefault(item.event_id, _compact_text(item.evidence_text, 240))
    selected = []
    seen_titles = set()
    seen_types = set()
    for event in sorted(events, key=lambda x: x.event_type):
        if event.title in seen_titles or event.event_type in seen_types:
            continue
        selected.append(event)
        seen_titles.add(event.title)
        seen_types.add(event.event_type)
        if len(selected) == 2:
            break
    if len(selected) < 2:
        for event in events:
            if event.title not in seen_titles:
                selected.append(event)
                seen_titles.add(event.title)
                if len(selected) == 2:
                    break
    payload = {
        "label": topic.label,
        "definition": _compact_text(topic.definition, 260),
        "event_count": topic.event_count,
        "representative_events": [
            {
                "event_id": str(x.id),
                "event_ref": event_ref(x.id),
                "event_type": x.event_type,
                "title": _compact_text(x.title, 180),
                "summary": _compact_text(x.summary, 180),
                "quote": quote_by_event.get(x.id, ""),
            }
            for x in selected
        ],
    }
    if identity_event_ids is not None:
        payload.pop("definition")
    payload["topic_ref" if topic_ref else "topic_id"] = topic_ref or str(topic.id)
    return payload


def _track_scope_payload(plan: ResearchPlan, config: PocConfig) -> dict[str, Any]:
    return {
        "industry_scopes": list(plan.industry_scopes),
        "track_boundary": config.stage4.target_track_boundary,
    }


def _apply_definitions(refs, output: TrackDefinitionOutput) -> None:
    returned = [item.track_ref for item in output.definitions]
    if len(returned) != len(set(returned)) or set(returned) != set(refs):
        raise ContractError("Track definitions must cover every input reference exactly once")
    for item in output.definitions:
        if item.definition.startswith("围绕") and "相关产品、技术或服务" in item.definition:
            raise ContractError("Track definition is a generic template")
    for item in output.definitions:
        refs[item.track_ref].definition = item.definition


def _definition_output_model(refs):
    @model_validator(mode="after")
    def validate_definitions(self):
        actual = [item.track_ref for item in self.definitions]
        if len(actual) != len(set(actual)) or set(actual) != set(refs):
            raise ValueError(
                f"Define each input Track exactly once; missing={sorted(set(refs) - set(actual))}, "
                f"unknown={sorted(set(actual) - set(refs))}, "
                f"duplicate refs={len(actual) != len(set(actual))}"
            )
        for item in self.definitions:
            if item.definition.startswith("围绕") and "相关产品、技术或服务" in item.definition:
                raise ValueError(
                    f"Track {item.track_ref}: replace generic template with a specific definition"
                )
        return self

    return type(
        "TrackDefinitionOutput",
        (TrackDefinitionOutput,),
        {"validate_definitions": validate_definitions},
    )


def _topic_work_package_groups(
    session: Session, plan_id: uuid.UUID, topics: list[Topic]
) -> list[tuple[str, list[Topic]]]:
    topic_ids = [topic.id for topic in topics]
    rows = session.execute(
        select(
            TopicEvent.topic_id,
            ResearchWorkPackage.id,
            ResearchWorkPackage.sequence_no,
        )
        .join(Event, Event.id == TopicEvent.event_id)
        .join(PageAnalysis, PageAnalysis.id == Event.page_analysis_id)
        .join(PageCapture, PageCapture.id == PageAnalysis.page_capture_id)
        .join(UrlDiscovery, UrlDiscovery.url_candidate_id == PageCapture.url_candidate_id)
        .join(
            ResearchWorkPackage,
            ResearchWorkPackage.id == UrlDiscovery.research_work_package_id,
        )
        .where(
            TopicEvent.topic_id.in_(topic_ids),
            ResearchWorkPackage.research_plan_id == plan_id,
        )
        .order_by(ResearchWorkPackage.sequence_no, ResearchWorkPackage.id)
    )
    primary_group: dict[uuid.UUID, tuple[int, uuid.UUID]] = {}
    for topic_id, work_package_id, sequence_no in rows:
        primary_group.setdefault(topic_id, (sequence_no, work_package_id))
    grouped: dict[str, list[Topic]] = defaultdict(list)
    for topic in topics:
        group = primary_group.get(topic.id)
        key = f"W{group[0]}:{group[1]}" if group else "unscoped"
        grouped[key].append(topic)
    return sorted(grouped.items(), key=lambda item: item[0])


def _combine_exact_track_identities(
    tracks: list[IdentifiedCandidateTrackOutput],
) -> list[IdentifiedCandidateTrackOutput]:
    combined: dict[tuple[str, str], IdentifiedCandidateTrackOutput] = {}
    for track in tracks:
        family = re.sub(r"\s+", "", track.offering_family).casefold()
        key = (family, track.supplier_role)
        existing = combined.get(key)
        if existing is None:
            combined[key] = track.model_copy(deep=True)
        else:
            existing.refs = list(dict.fromkeys([*existing.refs, *track.refs]))
            existing.supporting_refs = list(
                dict.fromkeys([*existing.supporting_refs, *track.supporting_refs])
            )
            existing.supporting_refs = [
                ref for ref in existing.supporting_refs if ref not in existing.refs
            ]
            existing.core_event_refs = list(
                dict.fromkeys([*existing.core_event_refs, *track.core_event_refs])
            )
    return list(combined.values())


def _validate_generation_reference_coverage(
    output: IdentifiedTrackBuildOutput, expected_refs: set[str]
) -> None:
    returned_refs = {
        ref for track in output.tracks for ref in [*track.refs, *track.supporting_refs]
    } | set(output.excluded_refs)
    if returned_refs != expected_refs:
        raise ContractError("Track generation must account for every input Topic")


def _expand_topic_refs(
    session: Session,
    output: IdentifiedTrackBuildOutput,
    topic_refs: dict[str, uuid.UUID],
    topics: dict[uuid.UUID, Topic],
) -> TrackBuildOutput:
    """Translate compact model-facing references back to persistent Topic IDs."""
    tracks = []
    for track in output.tracks:
        topic_ids = list(dict.fromkeys(topic_refs[ref] for ref in track.refs if ref in topic_refs))
        if not topic_ids:
            continue
        supporting_topic_ids = list(
            dict.fromkeys(
                topic_refs[ref]
                for ref in track.supporting_refs
                if ref in topic_refs and topic_refs[ref] not in topic_ids
            )
        )
        event_ids = _event_ids_for_topic_ids(session, [*topic_ids, *supporting_topic_ids])
        event_count = len(event_ids)
        identity_map = {event_ref(eid): eid for eid in event_ids}
        if not set(track.core_event_refs) <= set(identity_map):
            raise ContractError("Track identity references an event outside its Topics")
        if len(topic_ids) == 1:
            definition = topics[topic_ids[0]].definition
        else:
            definition = f"围绕{track.name}开展相关产品、技术或服务研发、生产与交付的企业集合。"
        tracks.append(
            {
                "name": track.name,
                "offering_family": track.offering_family,
                "supplier_role": track.supplier_role,
                "definition": definition,
                "topic_ids": topic_ids,
                "supporting_topic_ids": supporting_topic_ids,
                "identity_event_ids": [identity_map[r] for r in track.core_event_refs],
                "status": "candidate" if event_count >= 2 else "watchlist",
                "confidence": 0.75 if event_count >= 2 else 0.55,
            }
        )
    return TrackBuildOutput.model_validate({"tracks": tracks})


def _compact_text(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1].rstrip() + "…"


def _load_smb_product_catalog(path: Path) -> dict[str, str]:
    content = path.read_text(encoding="utf-8-sig").strip()
    if not content:
        raise ContractError(f"SMB product catalog is empty: {path}")
    return {"source": path.as_posix(), "content": content}


def _smb_analysis_input(track: CandidateTrack, catalog: dict[str, str]) -> dict[str, Any]:
    if not track.name.strip() or not track.definition.strip():
        raise ContractError("SMB analysis requires a track name and definition")
    return {
        "track": {"name": track.name, "definition": track.definition},
        "smb_product_catalog": catalog,
    }




def _event_ids_for_topic_ids(session: Session, topic_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Deduplicate track events by identifier without comparing JSON columns."""
    if not topic_ids:
        return set()
    return set(
        session.scalars(
            select(TopicEvent.event_id).where(TopicEvent.topic_id.in_(topic_ids)).distinct()
        )
    )


def _validate_build(
    session: Session,
    output: TrackBuildOutput,
    topics: list[Topic],
    config: PocConfig,
    *,
    allow_shared_market_evidence: bool = False,
) -> None:
    ids = {x.id for x in topics}
    members = [topic_id for track in output.tracks for topic_id in track.topic_ids]
    if not set(members).issubset(ids):
        raise ContractError("candidate Track references an unknown Topic")
    seen = {}
    for track in output.tracks:
        for tid in track.topic_ids:
            for other in seen.get(tid, []):
                if not allow_shared_market_evidence and (
                    not track.identity_event_ids
                    or not other.identity_event_ids
                    or set(track.identity_event_ids) & set(other.identity_event_ids)
                ):
                    raise ContractError("shared Topic requires distinct identity evidence")
            seen.setdefault(tid, []).append(track)
    supporting_members = [
        topic_id for track in output.tracks for topic_id in track.supporting_topic_ids
    ]
    if not set(supporting_members).issubset(ids):
        raise ContractError("candidate Track references an unknown supporting Topic")
    topic_map = {x.id: x for x in topics}
    for track in output.tracks:
        member_topic_ids = set(track.topic_ids)
        allowed_event_ids = _event_ids_for_topic_ids(session, list(member_topic_ids))
        if not set(track.identity_event_ids) <= allowed_event_ids:
            raise ContractError("identity evidence must belong to the core Topics")
        if config.stage4.forbid_region_in_identity:
            regions = _ordered_union(topic_map[x].regions for x in member_topic_ids)
            if _identity_contains_region([track.name], regions):
                track.status = "watchlist"
                track.confidence = min(track.confidence, 0.6)


def _normalize_build_output(
    session: Session, output: TrackBuildOutput, topics: list[Topic]
) -> list[dict[str, Any]]:
    topic_map = {topic.id: topic for topic in topics}
    kept = []
    adjustments: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for track in output.tracks:
        original_membership_count = len(track.topic_ids)
        track.topic_ids = list(dict.fromkeys(x for x in track.topic_ids if x in topic_map))
        track.supporting_topic_ids = list(
            dict.fromkeys(
                x for x in track.supporting_topic_ids if x in topic_map and x not in track.topic_ids
            )
        )
        if not track.topic_ids:
            adjustments.append(
                {
                    "name": track.name,
                    "action": "dropped",
                    "reason": "no valid Topic memberships",
                }
            )
            continue
        if original_membership_count != len(track.topic_ids):
            adjustments.append(
                {
                    "name": track.name,
                    "action": "invalid_or_duplicate_memberships_removed",
                    "removed_count": original_membership_count - len(track.topic_ids),
                }
            )
        member_topic_ids = set(track.topic_ids)
        if not _event_ids_for_topic_ids(session, list(member_topic_ids)):
            adjustments.append(
                {
                    "name": track.name,
                    "action": "dropped",
                    "reason": "member Topics have no Events",
                }
            )
            continue
        regions = _ordered_union(
            topic_map[topic_id].regions for topic_id in member_topic_ids if topic_id in topic_map
        )
        original_name = track.name
        cleaned_name = _remove_region_names(track.name, regions)
        if not cleaned_name:
            adjustments.append(
                {
                    "name": original_name,
                    "action": "dropped",
                    "reason": "identity contains only region terms",
                }
            )
            continue
        if any(term.casefold() in cleaned_name.casefold() for term in NON_TRACK_IDENTITY_TERMS):
            adjustments.append(
                {
                    "name": original_name,
                    "action": "dropped",
                    "reason": "identity describes a policy instrument or transient signal",
                }
            )
            continue
        normalized_identity = re.sub(r"\s+", "", cleaned_name).casefold()
        if normalized_identity in seen_names:
            adjustments.append(
                {
                    "name": original_name,
                    "action": "dropped",
                    "reason": "duplicate Track identity",
                }
            )
            continue
        seen_names.add(normalized_identity)
        if cleaned_name != original_name:
            track.name = cleaned_name
            track.status = "watchlist"
            track.confidence = min(track.confidence, 0.6)
            adjustments.append(
                {
                    "name": original_name,
                    "action": "region_removed_and_marked_watchlist",
                    "original_name": original_name,
                    "normalized_name": cleaned_name,
                }
            )
        kept.append(track)
    output.tracks = kept
    return adjustments


def _remove_region_names(value: str, regions: list[str]) -> str:
    result = value
    terms: set[str] = set()
    suffixes = ("特别行政区", "自治区", "自治州", "省", "市", "区", "县")
    for region in regions:
        normalized = region.strip()
        if not normalized:
            continue
        terms.add(normalized)
        shortened = normalized
        for suffix in suffixes:
            if shortened.endswith(suffix) and len(shortened) > len(suffix) + 1:
                shortened = shortened[: -len(suffix)]
                terms.add(shortened)
                break
    for term in sorted(terms, key=len, reverse=True):
        result = result.replace(term, "")
    return re.sub(r"^[\s·•—–\-_:：]+|[\s·•—–\-_:：]+$", "", result).strip()


def _identity_contains_region(identity_values: list[str], regions: list[str]) -> bool:
    tokens: set[str] = set()
    for region in regions:
        normalized = region.strip()
        if not normalized:
            continue
        tokens.add(normalized)
        for suffix in ("特别行政区", "自治区", "自治州", "省", "市", "区", "县"):
            if normalized.endswith(suffix) and len(normalized) > len(suffix):
                tokens.add(normalized[: -len(suffix)])
    return any(token and token in value for value in identity_values for token in tokens)


def _new_model_run(
    pipeline_id: uuid.UUID, task: str, model: str, prompt_version: str, input_fp: str
) -> ModelRun:
    return ModelRun(
        pipeline_run_id=pipeline_id,
        task_type=task,
        status=TaskStatus.RUNNING,
        provider="bailian",
        model=model,
        prompt_version=prompt_version,
        input_fingerprint=input_fp,
        tool_config={"type": "structured_completion", "enable_thinking": False},
        started_at=datetime.now(UTC),
    )


def _complete_model_run(model_run: ModelRun, result: Any) -> None:
    model_run.status = TaskStatus.COMPLETED
    model_run.request_id = result.request_id
    model_run.usage = result.usage
    model_run.raw_response = result.raw_response
    model_run.structured_output = result.output.model_dump(mode="json")
    model_run.finished_at = datetime.now(UTC)


def _fail_model_run_only(model_run: ModelRun, error: Exception) -> None:
    model_run.status = (
        TaskStatus.FAILED_RETRYABLE
        if isinstance(error, ModelCallError) and error.retryable
        else TaskStatus.FAILED_TERMINAL
    )
    model_run.error_code = getattr(error, "code", "track_contract_error")
    model_run.error_message = str(error)
    if isinstance(error, ModelCallError):
        model_run.request_id, model_run.usage, model_run.raw_response = error.response_metadata()
    model_run.finished_at = datetime.now(UTC)


def _fail(model_run: ModelRun, run: TrackBuildRun, pipeline: PipelineRun, error: Exception) -> None:
    _fail_model_run_only(model_run, error)
    status = model_run.status
    run.status = status
    run.error_code = model_run.error_code
    run.error_message = str(error)
    run.finished_at = datetime.now(UTC)
    pipeline.status = status
    pipeline.finished_at = datetime.now(UTC)


def close_interrupted_build(session, run_id, pipeline_id, error):
    """Roll back unpublished work; retain completed batches and close only this running build."""
    session.rollback()
    run = session.get(TrackBuildRun, run_id)
    if run is None or run.pipeline_run_id != pipeline_id or run.status != TaskStatus.RUNNING:
        return False
    now = datetime.now(UTC)
    code = "track_runtime_error"
    message = f"{type(error).__name__}: {error}"
    run.status = TaskStatus.FAILED_RETRYABLE
    run.error_code, run.error_message = code, message
    run.finished_at = now
    pipeline = session.get(PipelineRun, pipeline_id)
    if pipeline is not None and pipeline.status == TaskStatus.RUNNING:
        pipeline.status = TaskStatus.FAILED_RETRYABLE
        pipeline.finished_at = now
    for child in session.scalars(
        select(ModelRun).where(
            ModelRun.pipeline_run_id == pipeline_id, ModelRun.status == TaskStatus.RUNNING
        )
    ):
        child.status = TaskStatus.FAILED_RETRYABLE
        child.error_code, child.error_message = code, message
        child.finished_at = now
    record_event(
        session,
        event_type="track.run.failed",
        message="Track build interrupted",
        level="ERROR",
        pipeline_run_id=pipeline_id,
        entity_type="track_build_run",
        entity_id=run_id,
        details={"error_code": code, "error_message": message},
    )
    return True


def _prompt_version(path: Path, content: str) -> str:
    return f"{path.stem}:{fingerprint(content)[:12]}"


def _stored_build_prompt_version(version: str) -> str:
    """The full composite remains in input hashing and the pipeline audit payload."""
    return version if len(version) <= 100 else "build:" + fingerprint(version)


def _ordered_union(values: Any) -> list[str]:
    return list(dict.fromkeys(item for group in values for item in group if item))


def _build_payload(run: TrackBuildRun, *, resumed: bool) -> dict[str, Any]:
    return {
        "track_build_run_id": str(run.id),
        "status": str(run.status),
        "topic_count": run.topic_count,
        "track_count": run.track_count,
        "unassigned_topic_count": run.unassigned_topic_count,
        "resumed": resumed,
    }
