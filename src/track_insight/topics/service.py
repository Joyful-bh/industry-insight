import hashlib
import json
import math
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from track_insight.core.enums import TaskStatus
from track_insight.core.errors import ContractError, ModelCallError
from track_insight.core.fingerprints import fingerprint
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.infrastructure.events import record_event
from track_insight.infrastructure.jobs import complete_job, enqueue_job, fail_job
from track_insight.infrastructure.models import (
    Event,
    Job,
    ModelRun,
    PageAnalysis,
    PageCapture,
    PipelineRun,
    ResearchWorkPackage,
    Topic,
    TopicBuildRun,
    TopicCandidate,
    TopicEvent,
    TopicMergeDecision,
    UrlDiscovery,
)
from track_insight.settings import PocConfig
from track_insight.topics.contracts import (
    TopicGenerationOutput,
    TopicReconciliationOutput,
    TopicStatus,
)

TOPIC_PROCESSOR_VERSION = "event-topic-v7-direct"


class TopicService:
    def __init__(self, *, client: BailianClient, config: PocConfig) -> None:
        self.client = client
        self.config = config

    def build(
        self,
        session: Session,
        *,
        plan_id: uuid.UUID,
        max_events: int | None = None,
        batch_size: int | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        event_limit = min(
            max_events or self.config.stage3.max_events_per_run,
            self.config.stage3.max_events_per_run,
        )
        effective_batch_size = min(
            batch_size or self.config.stage3.event_batch_size, self.config.stage3.event_batch_size
        )
        events = _select_events(
            session, plan_id, event_limit, self.config.stage3.minimum_event_confidence
        )
        if not events:
            raise ContractError("no eligible events found for the research plan")

        generation_prompt = self.config.stage3.prompts.topic_generation.read_text(encoding="utf-8")
        generation_version = _prompt_version(
            self.config.stage3.prompts.topic_generation, generation_prompt
        )
        reconciliation_version = "direct-finalization-v1"
        input_fingerprint = fingerprint(
            [(str(event.id), event.event_fingerprint) for event in events],
            self.config.stage3.model,
            generation_version,
            reconciliation_version,
            effective_batch_size,
        )
        build_run = session.scalar(
            select(TopicBuildRun).where(
                TopicBuildRun.research_plan_id == plan_id,
                TopicBuildRun.input_fingerprint == input_fingerprint,
                TopicBuildRun.processor_version == TOPIC_PROCESSOR_VERSION,
            )
        )
        if build_run is not None and build_run.status == TaskStatus.COMPLETED:
            return _run_payload(build_run, resumed=True)
        if build_run is not None and build_run.status == TaskStatus.FAILED_TERMINAL:
            raise ContractError("Topic build failed terminally; change inputs or processor version")
        if build_run is not None and not resume:
            raise ContractError("an unfinished Topic build exists; rerun with --resume")

        pipeline = PipelineRun(
            run_type="event_to_topic",
            status=TaskStatus.RUNNING,
            input_payload={
                "plan_id": str(plan_id),
                "event_count": len(events),
                "batch_size": effective_batch_size,
            },
            started_at=datetime.now(UTC),
        )
        session.add(pipeline)
        session.flush()
        if build_run is None:
            build_run = TopicBuildRun(
                research_plan_id=plan_id,
                pipeline_run_id=pipeline.id,
                status=TaskStatus.RUNNING,
                input_fingerprint=input_fingerprint,
                processor_version=TOPIC_PROCESSOR_VERSION,
                generation_prompt_version=generation_version,
                reconciliation_prompt_version=reconciliation_version,
                model=self.config.stage3.model,
                event_ids=[str(event.id) for event in events],
                event_count=len(events),
                batch_count=math.ceil(len(events) / effective_batch_size),
                started_at=datetime.now(UTC),
            )
            session.add(build_run)
            session.flush()
        else:
            build_run.pipeline_run_id = pipeline.id
            build_run.status = TaskStatus.RUNNING
            build_run.error_code = None
            build_run.error_message = None
        record_event(
            session,
            event_type="topic.run.started",
            message="Event-to-Topic run started",
            pipeline_run_id=pipeline.id,
            entity_type="topic_build_run",
            entity_id=build_run.id,
            details=pipeline.input_payload,
        )
        session.commit()

        try:
            for batch_no, start in enumerate(range(0, len(events), effective_batch_size), start=1):
                batch = events[start : start + effective_batch_size]
                self._generate_batch(
                    session,
                    pipeline,
                    build_run,
                    batch_no,
                    batch,
                    generation_prompt,
                    generation_version,
                )
            candidates = list(
                session.scalars(
                    select(TopicCandidate)
                    .where(TopicCandidate.topic_build_run_id == build_run.id)
                    .order_by(TopicCandidate.batch_no, TopicCandidate.candidate_key)
                )
            )
            self._finalize_candidates(session, pipeline, build_run, candidates)
            _remove_stale_memberships(session, build_run, events)
        except (ModelCallError, ContractError) as error:
            build_run.status = (
                TaskStatus.FAILED_RETRYABLE
                if isinstance(error, ModelCallError) and error.retryable
                else TaskStatus.FAILED_TERMINAL
            )
            build_run.error_code = (
                error.code if isinstance(error, ModelCallError) else "topic_contract_error"
            )
            build_run.error_message = str(error)
            pipeline.status = build_run.status
            pipeline.finished_at = datetime.now(UTC)
            record_event(
                session,
                event_type="topic.run.failed",
                message="Event-to-Topic run failed",
                level="ERROR",
                pipeline_run_id=pipeline.id,
                entity_type="topic_build_run",
                entity_id=build_run.id,
                details={"error_code": build_run.error_code, "error_message": str(error)},
            )
            session.commit()
            raise

        assigned = set(
            session.scalars(
                select(TopicEvent.event_id)
                .join(Topic, Topic.id == TopicEvent.topic_id)
                .where(Topic.updated_by_run_id == build_run.id)
            )
        )
        build_run.candidate_count = len(candidates)
        build_run.topic_count = int(
            session.scalar(
                select(func.count(Topic.id)).where(Topic.updated_by_run_id == build_run.id)
            )
            or 0
        )
        build_run.unassigned_event_count = len(set(event.id for event in events) - assigned)
        build_run.status = TaskStatus.COMPLETED
        build_run.finished_at = datetime.now(UTC)
        pipeline.status = TaskStatus.COMPLETED
        pipeline.counters = {
            "events": len(events),
            "candidates": build_run.candidate_count,
            "topics": build_run.topic_count,
            "unassigned_events": build_run.unassigned_event_count,
        }
        pipeline.finished_at = datetime.now(UTC)
        record_event(
            session,
            event_type="topic.run.completed",
            message="Event-to-Topic run completed",
            pipeline_run_id=pipeline.id,
            entity_type="topic_build_run",
            entity_id=build_run.id,
            details=pipeline.counters,
        )
        session.commit()
        return _run_payload(build_run, resumed=False)

    def _generate_batch(
        self,
        session: Session,
        pipeline: PipelineRun,
        build_run: TopicBuildRun,
        batch_no: int,
        events: list[Event],
        prompt: str,
        prompt_version: str,
    ) -> None:
        batch_fingerprint = fingerprint(
            [(str(event.id), event.event_fingerprint) for event in events],
            prompt_version,
        )
        job = enqueue_job(
            session,
            job_type="topic_generate",
            object_type="topic_batch",
            object_id=f"{build_run.id}:{batch_no}",
            input_fingerprint=batch_fingerprint,
            processor_version=TOPIC_PROCESSOR_VERSION,
        )
        if job.status == TaskStatus.COMPLETED:
            return
        if job.status == TaskStatus.FAILED_TERMINAL:
            raise ContractError(f"Topic generation batch {batch_no} failed terminally")
        job.pipeline_run_id = pipeline.id
        job.status = TaskStatus.RUNNING
        job.attempts += 1
        model_run = _new_model_run(
            pipeline.id,
            "topic_generation",
            self.config.stage3.model,
            prompt_version,
            batch_fingerprint,
        )
        session.add(model_run)
        session.flush()
        started = time.monotonic()
        record_event(
            session,
            event_type="topic.batch.started",
            message="Topic generation batch started",
            pipeline_run_id=pipeline.id,
            job_id=job.id,
            model_run_id=model_run.id,
            entity_type="topic_build_run",
            entity_id=build_run.id,
            details={"batch_no": batch_no, "event_count": len(events)},
        )
        session.commit()
        try:
            result = self.client.complete_structured(
                messages=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"events": [_event_payload(event) for event in events]},
                            ensure_ascii=False,
                        ),
                    },
                ],
                output_model=TopicGenerationOutput,
                model=self.config.stage3.model,
                max_tokens=self.config.stage3.generation_max_output_tokens,
                enable_thinking=False,
            )
            _validate_generation(result.output, {event.id for event in events}, self.config)
        except (ModelCallError, ContractError) as error:
            _fail_model_and_job(session, model_run, job, error)
            record_event(
                session,
                event_type="topic.batch.failed",
                message="Topic generation batch failed",
                level="ERROR",
                pipeline_run_id=pipeline.id,
                job_id=job.id,
                model_run_id=model_run.id,
                entity_type="topic_build_run",
                entity_id=build_run.id,
                details={"batch_no": batch_no, "error_message": str(error)},
            )
            session.commit()
            raise
        _complete_model_run(model_run, result)
        event_map = {event.id: event for event in events}
        for proposed in result.output.candidates:
            global_key = f"b{batch_no}_{proposed.candidate_key}"
            member_events = [event_map[event_id] for event_id in proposed.event_ids]
            keywords = _ordered_union(
                [event.industry_objects + ([event.topic_hint] if event.topic_hint else []) for event in member_events]
            )[:30]
            session.add(
                TopicCandidate(
                    topic_build_run_id=build_run.id,
                    model_run_id=model_run.id,
                    batch_no=batch_no,
                    candidate_key=global_key,
                    label=proposed.label,
                    definition=proposed.definition,
                    aliases=[],
                    keywords=keywords,
                    regions=_ordered_union(event.regions for event in member_events),
                    industries=_ordered_union(event.industries for event in member_events),
                    summary=proposed.summary,
                    proposed_memberships=[
                        {
                            "event_id": str(event_id),
                            "relationship": "core_signal",
                            "relevance_score": proposed.confidence,
                            "reason": "Direct Topic grouping",
                        }
                        for event_id in proposed.event_ids
                    ],
                    confidence=proposed.confidence,
                    candidate_fingerprint=fingerprint(proposed.model_dump(mode="json")),
                )
            )
        complete_job(
            session,
            job,
            {
                "event_count": len(events),
                "candidate_count": len(result.output.candidates),
            },
        )
        record_event(
            session,
            event_type="topic.batch.completed",
            message="Topic generation batch completed",
            pipeline_run_id=pipeline.id,
            job_id=job.id,
            model_run_id=model_run.id,
            entity_type="topic_build_run",
            entity_id=build_run.id,
            duration_ms=int((time.monotonic() - started) * 1000),
            details={"batch_no": batch_no, "candidate_count": len(result.output.candidates)},
        )
        session.commit()

    def _finalize_candidates(
        self,
        session: Session,
        pipeline: PipelineRun,
        build_run: TopicBuildRun,
        candidates: list[TopicCandidate],
    ) -> None:
        """Persist generated Topic candidates directly without a second model pass."""
        for candidate in candidates:
            topic_fp = fingerprint(candidate.label.casefold(), candidate.definition.casefold())
            canonical_key = f"topic_{topic_fp[:20]}"
            topic = session.scalar(
                select(Topic).where(
                    Topic.research_plan_id == build_run.research_plan_id,
                    Topic.topic_fingerprint == topic_fp,
                )
            )
            if topic is None:
                topic = Topic(
                    research_plan_id=build_run.research_plan_id,
                    created_by_run_id=build_run.id,
                    updated_by_run_id=build_run.id,
                    canonical_key=canonical_key,
                    label=candidate.label,
                    definition=candidate.definition,
                    aliases=candidate.aliases,
                    keywords=candidate.keywords,
                    regions=[],
                    industries=[],
                    summary=candidate.summary,
                    first_seen_at=None,
                    latest_seen_at=None,
                    event_count=0,
                    confidence=candidate.confidence,
                    topic_fingerprint=topic_fp,
                )
                session.add(topic)
                session.flush()
            else:
                topic.updated_by_run_id = build_run.id
                topic.status = "candidate"
                topic.label = candidate.label
                topic.definition = candidate.definition
                topic.summary = candidate.summary
                topic.confidence = max(topic.confidence, candidate.confidence)
                topic.aliases = _ordered_union([topic.aliases, candidate.aliases])
                topic.keywords = _ordered_union([topic.keywords, candidate.keywords])[:50]

            for membership in candidate.proposed_memberships:
                event_id = uuid.UUID(membership["event_id"])
                relation = session.scalar(
                    select(TopicEvent).where(
                        TopicEvent.topic_id == topic.id,
                        TopicEvent.event_id == event_id,
                    )
                )
                if relation is None:
                    session.add(
                        TopicEvent(
                            topic_id=topic.id,
                            event_id=event_id,
                            topic_build_run_id=build_run.id,
                            relationship="core_signal",
                            relevance_score=candidate.confidence,
                            assignment_reason="Direct Topic grouping",
                            processor_version=TOPIC_PROCESSOR_VERSION,
                        )
                    )
                else:
                    relation.topic_build_run_id = build_run.id
                    relation.relevance_score = max(
                        relation.relevance_score, candidate.confidence
                    )
                    relation.processor_version = TOPIC_PROCESSOR_VERSION

            candidate.status = "merged"
            session.add(
                TopicMergeDecision(
                    topic_build_run_id=build_run.id,
                    model_run_id=candidate.model_run_id,
                    candidate_id=candidate.id,
                    action="merge_into",
                    target_topic_id=topic.id,
                    canonical_key=canonical_key,
                    reason="Persisted directly from the generation batch",
                )
            )
            session.flush()
            topic_events = list(
                session.scalars(
                    select(Event)
                    .join(TopicEvent, TopicEvent.event_id == Event.id)
                    .where(TopicEvent.topic_id == topic.id)
                )
            )
            dates = [event.signal_date for event in topic_events if event.signal_date]
            topic.regions = _ordered_union(event.regions for event in topic_events)
            topic.industries = _ordered_union(event.industries for event in topic_events)
            topic.first_seen_at = min(dates) if dates else None
            topic.latest_seen_at = max(dates) if dates else None
            topic.event_count = len(topic_events)

        record_event(
            session,
            event_type="topic.finalization.completed",
            message="Topic candidates persisted directly",
            pipeline_run_id=pipeline.id,
            entity_type="topic_build_run",
            entity_id=build_run.id,
            details={"topic_candidate_count": len(candidates)},
        )
        session.commit()

    def _reconcile(
        self,
        session: Session,
        pipeline: PipelineRun,
        build_run: TopicBuildRun,
        events: list[Event],
        candidates: list[TopicCandidate],
        prompt: str,
        prompt_version: str,
    ) -> None:
        if session.scalar(
            select(func.count(TopicMergeDecision.id)).where(
                TopicMergeDecision.topic_build_run_id == build_run.id
            )
        ):
            return
        if not candidates:
            return
        candidate_fingerprint = fingerprint(
            [candidate.candidate_fingerprint for candidate in candidates], prompt_version
        )
        job = enqueue_job(
            session,
            job_type="topic_reconcile",
            object_type="topic_build_run",
            object_id=str(build_run.id),
            input_fingerprint=candidate_fingerprint,
            processor_version=TOPIC_PROCESSOR_VERSION,
        )
        if job.status == TaskStatus.COMPLETED:
            return
        if job.status == TaskStatus.FAILED_TERMINAL:
            raise ContractError("Topic reconciliation failed terminally")
        job.pipeline_run_id = pipeline.id
        job.status = TaskStatus.RUNNING
        job.attempts += 1
        model_run = _new_model_run(
            pipeline.id,
            "topic_reconciliation",
            self.config.stage3.model,
            prompt_version,
            candidate_fingerprint,
        )
        session.add(model_run)
        session.flush()
        record_event(
            session,
            event_type="topic.reconciliation.started",
            message="Topic reconciliation started",
            pipeline_run_id=pipeline.id,
            job_id=job.id,
            model_run_id=model_run.id,
            entity_type="topic_build_run",
            entity_id=build_run.id,
            details={"candidate_count": len(candidates)},
        )
        session.commit()
        existing_topics = list(
            session.scalars(
                select(Topic).where(
                    Topic.research_plan_id == build_run.research_plan_id,
                    Topic.status == "candidate",
                )
            )
        )
        try:
            result = self._reconcile_hierarchically(
                session,
                pipeline,
                build_run,
                candidates,
                existing_topics,
                prompt,
            )
            _validate_reconciliation(
                result.output, {candidate.candidate_key for candidate in candidates}
            )
        except (ModelCallError, ContractError) as error:
            _fail_model_and_job(session, model_run, job, error)
            record_event(
                session,
                event_type="topic.reconciliation.failed",
                message="Topic reconciliation failed",
                level="ERROR",
                pipeline_run_id=pipeline.id,
                job_id=job.id,
                model_run_id=model_run.id,
                entity_type="topic_build_run",
                entity_id=build_run.id,
                details={"error_message": str(error)},
            )
            session.commit()
            raise
        _complete_model_run(model_run, result)
        topic_map: dict[str, Topic] = {}
        candidate_map = {candidate.candidate_key: candidate for candidate in candidates}
        for proposed in result.output.topics:
            topic = session.scalar(
                select(Topic).where(
                    Topic.research_plan_id == build_run.research_plan_id,
                    Topic.canonical_key == proposed.canonical_key,
                )
            )
            topic_fp = fingerprint(proposed.label.casefold(), proposed.definition.casefold())
            if topic is None:
                topic = Topic(
                    research_plan_id=build_run.research_plan_id,
                    created_by_run_id=build_run.id,
                    updated_by_run_id=build_run.id,
                    canonical_key=proposed.canonical_key,
                    label=proposed.label,
                    definition=proposed.definition,
                    aliases=proposed.aliases,
                    keywords=proposed.keywords,
                    regions=[],
                    industries=[],
                    summary=proposed.summary,
                    first_seen_at=None,
                    latest_seen_at=None,
                    event_count=0,
                    confidence=proposed.confidence,
                    topic_fingerprint=topic_fp,
                )
                session.add(topic)
                session.flush()
            else:
                topic.updated_by_run_id = build_run.id
                topic.status = "candidate"
                topic.label = proposed.label
                topic.definition = proposed.definition
                topic.aliases = proposed.aliases
                topic.keywords = proposed.keywords
                topic.summary = proposed.summary
                topic.confidence = proposed.confidence
                topic.topic_fingerprint = topic_fp
            topic_map[proposed.canonical_key] = topic
            for membership in _memberships_for_candidates(
                proposed.source_candidate_keys, candidate_map
            ):
                relation = session.scalar(
                    select(TopicEvent).where(
                        TopicEvent.topic_id == topic.id,
                        TopicEvent.event_id == membership["event_id"],
                    )
                )
                if relation is None:
                    relation = TopicEvent(
                        topic_id=topic.id,
                        event_id=membership["event_id"],
                        topic_build_run_id=build_run.id,
                        relationship=membership["relationship"],
                        relevance_score=membership["relevance_score"],
                        assignment_reason=membership["reason"],
                        processor_version=TOPIC_PROCESSOR_VERSION,
                    )
                    session.add(relation)
                else:
                    relation.topic_build_run_id = build_run.id
                    relation.relationship = membership["relationship"]
                    relation.relevance_score = membership["relevance_score"]
                    relation.assignment_reason = membership["reason"]
                    relation.processor_version = TOPIC_PROCESSOR_VERSION
            session.flush()
            all_topic_events = list(
                session.scalars(
                    select(Event)
                    .join(TopicEvent, TopicEvent.event_id == Event.id)
                    .where(TopicEvent.topic_id == topic.id)
                )
            )
            dates = [event.signal_date for event in all_topic_events if event.signal_date]
            topic.regions = _ordered_union(event.regions for event in all_topic_events)
            topic.industries = _ordered_union(event.industries for event in all_topic_events)
            topic.first_seen_at = min(dates) if dates else None
            topic.latest_seen_at = max(dates) if dates else None
            topic.event_count = len(all_topic_events)
        candidate_targets = {
            source_key: proposed.canonical_key
            for proposed in result.output.topics
            for source_key in proposed.source_candidate_keys
        }
        for candidate in candidates:
            target_key = candidate_targets.get(candidate.candidate_key)
            target = topic_map.get(target_key or "")
            action = "reject" if target is None else "merge_into"
            candidate.status = "rejected" if target is None else "merged"
            session.add(
                TopicMergeDecision(
                    topic_build_run_id=build_run.id,
                    model_run_id=model_run.id,
                    candidate_id=candidate.id,
                    action=action,
                    target_topic_id=target.id if target else None,
                    canonical_key=target_key,
                    reason=(
                        "Program-derived from source_candidate_keys"
                        if target
                        else "Not retained by reconciliation"
                    ),
                )
            )
        complete_job(session, job, {"topic_count": len(result.output.topics)})
        record_event(
            session,
            event_type="topic.reconciliation.completed",
            message="Topic reconciliation completed",
            pipeline_run_id=pipeline.id,
            job_id=job.id,
            model_run_id=model_run.id,
            entity_type="topic_build_run",
            entity_id=build_run.id,
            details={"topic_count": len(result.output.topics)},
        )
        session.commit()

    def _reconcile_hierarchically(
        self,
        session: Session,
        pipeline: PipelineRun,
        build_run: TopicBuildRun,
        candidates: list[TopicCandidate],
        existing_topics: list[Topic],
        prompt: str,
    ):
        batch_size = self.config.stage3.reconciliation_batch_size
        payloads = [_candidate_payload(candidate) for candidate in candidates]
        source_map = {
            candidate.candidate_key: [candidate.candidate_key] for candidate in candidates
        }

        if len(payloads) > batch_size:
            intermediate: list[dict[str, Any]] = []
            intermediate_sources: dict[str, list[str]] = {}
            for group_no, start in enumerate(range(0, len(payloads), batch_size), start=1):
                group = payloads[start : start + batch_size]
                group_result = self._call_reconciliation(prompt, group, [])
                _validate_reconciliation(
                    group_result.output, {item["candidate_key"] for item in group}
                )
                record_event(
                    session,
                    event_type="topic.reconciliation.group.completed",
                    message="Topic reconciliation group completed",
                    pipeline_run_id=pipeline.id,
                    entity_type="topic_build_run",
                    entity_id=build_run.id,
                    details={
                        "group_no": group_no,
                        "input_count": len(group),
                        "output_count": len(group_result.output.topics),
                    },
                )
                for index, topic in enumerate(group_result.output.topics, start=1):
                    key = f"g{group_no}_t{index}"
                    originals = [
                        original
                        for source_key in topic.source_candidate_keys
                        for original in source_map[source_key]
                    ]
                    intermediate_sources[key] = list(dict.fromkeys(originals))
                    intermediate.append(_reconciled_topic_payload(key, topic))
            payloads = intermediate
            source_map = intermediate_sources
            session.commit()

        result = self._call_reconciliation(
            prompt,
            payloads,
            [_existing_topic_payload(topic) for topic in existing_topics],
        )
        _validate_reconciliation(result.output, {item["candidate_key"] for item in payloads})
        for topic in result.output.topics:
            topic.source_candidate_keys = list(
                dict.fromkeys(
                    original
                    for source_key in topic.source_candidate_keys
                    for original in source_map[source_key]
                )
            )
        return result

    def _call_reconciliation(
        self,
        prompt: str,
        candidates: list[dict[str, Any]],
        existing_topics: list[dict[str, Any]],
    ):
        return self.client.complete_structured(
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"candidates": candidates, "existing_topics": existing_topics},
                        ensure_ascii=False,
                    ),
                },
            ],
            output_model=TopicReconciliationOutput,
            model=self.config.stage3.model,
            max_tokens=self.config.stage3.reconciliation_max_output_tokens,
            enable_thinking=False,
        )


def _select_events(
    session: Session, plan_id: uuid.UUID, limit: int, minimum_confidence: float
) -> list[Event]:
    events = list(
        session.scalars(
            select(Event)
            .join(PageAnalysis, PageAnalysis.id == Event.page_analysis_id)
            .join(PageCapture, PageCapture.id == PageAnalysis.page_capture_id)
            .where(
                PageAnalysis.status == TaskStatus.COMPLETED,
                Event.confidence >= minimum_confidence,
                select(UrlDiscovery.id)
                .join(
                    ResearchWorkPackage,
                    ResearchWorkPackage.id == UrlDiscovery.research_work_package_id,
                )
                .where(
                    UrlDiscovery.url_candidate_id == PageCapture.url_candidate_id,
                    ResearchWorkPackage.research_plan_id == plan_id,
                )
                .exists(),
            )
            .order_by(Event.signal_date.asc().nulls_last(), Event.created_at, Event.id)
            .limit(limit)
        )
    )
    return sorted(
        events,
        key=lambda event: (
            (event.topic_hint or "").casefold(),
            ((event.industry_objects or event.industries or [""])[0]).casefold(),
            event.signal_date is None,
            event.signal_date or datetime.max.date(),
            str(event.id),
        ),
    )


def _remove_stale_memberships(
    session: Session, build_run: TopicBuildRun, events: list[Event]
) -> None:
    event_ids = [event.id for event in events]
    if not event_ids:
        return
    stale_topic_ids = list(
        session.scalars(
            select(TopicEvent.topic_id)
            .join(Topic, Topic.id == TopicEvent.topic_id)
            .where(
                Topic.research_plan_id == build_run.research_plan_id,
                TopicEvent.event_id.in_(event_ids),
                Topic.updated_by_run_id != build_run.id,
            )
            .distinct()
        )
    )
    if not stale_topic_ids:
        return
    session.execute(
        delete(TopicEvent).where(
            TopicEvent.topic_id.in_(stale_topic_ids), TopicEvent.event_id.in_(event_ids)
        )
    )
    for topic in session.scalars(select(Topic).where(Topic.id.in_(stale_topic_ids))):
        remaining_events = list(
            session.scalars(
                select(Event)
                .join(TopicEvent, TopicEvent.event_id == Event.id)
                .where(TopicEvent.topic_id == topic.id)
            )
        )
        dates = [event.signal_date for event in remaining_events if event.signal_date]
        topic.regions = _ordered_union(event.regions for event in remaining_events)
        topic.industries = _ordered_union(event.industries for event in remaining_events)
        topic.first_seen_at = min(dates) if dates else None
        topic.latest_seen_at = max(dates) if dates else None
        topic.event_count = len(remaining_events)
        if not remaining_events:
            topic.status = "archived"


def _event_payload(event: Event) -> dict[str, Any]:
    return {
        "event_id": str(event.id),
        "event_type": event.event_type,
        "event_status": event.event_status,
        "title": event.title,
        "summary": event.summary,
        "signal_date": event.signal_date.isoformat() if event.signal_date else None,
        "regions": event.regions,
        "industries": event.industries,
        "industry_objects": event.industry_objects,
        "topic_hint": event.topic_hint,
        "entities": event.entities,
        "chain_roles": event.chain_roles,
        "smb_relevance": event.smb_relevance,
        "confidence": event.confidence,
    }


def _candidate_payload(candidate: TopicCandidate) -> dict[str, Any]:
    return {
        "candidate_key": candidate.candidate_key,
        "label": candidate.label,
        "definition": candidate.definition,
        "aliases": candidate.aliases,
        "keywords": candidate.keywords,
        "regions": candidate.regions,
        "industries": candidate.industries,
        "summary": candidate.summary,
        "confidence": candidate.confidence,
    }


def _reconciled_topic_payload(candidate_key: str, topic) -> dict[str, Any]:
    return {
        "candidate_key": candidate_key,
        "label": topic.label,
        "definition": topic.definition,
        "aliases": topic.aliases,
        "keywords": topic.keywords,
        "regions": topic.regions,
        "industries": topic.industries,
        "summary": topic.summary,
        "confidence": topic.confidence,
    }


def _memberships_for_candidates(
    candidate_keys: list[str], candidate_map: dict[str, TopicCandidate]
) -> list[dict[str, Any]]:
    relationship_rank = {"context_signal": 0, "supporting_signal": 1, "core_signal": 2}
    memberships: dict[uuid.UUID, dict[str, Any]] = {}
    for candidate_key in candidate_keys:
        for item in candidate_map[candidate_key].proposed_memberships:
            event_id = uuid.UUID(item["event_id"])
            current = memberships.get(event_id)
            if current is None or (
                relationship_rank[item["relationship"]], item["relevance_score"]
            ) > (
                relationship_rank[current["relationship"]], current["relevance_score"]
            ):
                memberships[event_id] = {
                    "event_id": event_id,
                    "relationship": item["relationship"],
                    "relevance_score": item["relevance_score"],
                    "reason": item["reason"],
                }
    return list(memberships.values())


def _existing_topic_payload(topic: Topic) -> dict[str, Any]:
    return {
        "canonical_key": topic.canonical_key,
        "label": topic.label,
        "definition": topic.definition,
        "aliases": topic.aliases,
        "keywords": topic.keywords,
    }


def _validate_generation(
    output: TopicGenerationOutput,
    events: set[uuid.UUID],
    config: PocConfig,
) -> None:
    membership_ids = [event_id for candidate in output.candidates for event_id in candidate.event_ids]
    if not set(membership_ids).issubset(events):
        raise ContractError("topic candidate references an unknown event")
    counts: dict[uuid.UUID, int] = {}
    for event_id in membership_ids:
        counts[event_id] = counts.get(event_id, 0) + 1
    if any(count > config.stage3.max_topics_per_event for count in counts.values()):
        raise ContractError("an event is assigned to too many Topic candidates")


def _validate_reconciliation(
    output: TopicReconciliationOutput, candidate_keys: set[str]
) -> None:
    occurrences: dict[str, int] = {}
    for topic in output.topics:
        if not set(topic.source_candidate_keys).issubset(candidate_keys):
            raise ContractError("reconciled Topic references an unknown candidate")
        if len(topic.source_candidate_keys) != len(set(topic.source_candidate_keys)):
            raise ContractError("a reconciled Topic contains duplicate source candidates")
        for candidate_key in topic.source_candidate_keys:
            occurrences[candidate_key] = occurrences.get(candidate_key, 0) + 1
    if any(count > 1 for count in occurrences.values()):
        raise ContractError("a candidate is assigned to multiple reconciled Topics")


def _new_model_run(
    pipeline_run_id: uuid.UUID, task_type: str, model: str, prompt_version: str, input_fp: str
) -> ModelRun:
    return ModelRun(
        pipeline_run_id=pipeline_run_id,
        task_type=task_type,
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


def _fail_model_and_job(session: Session, model_run: ModelRun, job: Job, error: Exception) -> None:
    retryable = isinstance(error, ModelCallError) and error.retryable
    model_run.status = TaskStatus.FAILED_RETRYABLE if retryable else TaskStatus.FAILED_TERMINAL
    model_run.error_code = (
        error.code if isinstance(error, ModelCallError) else "topic_contract_error"
    )
    model_run.error_message = str(error)
    if isinstance(error, ModelCallError):
        model_run.request_id, model_run.usage, model_run.raw_response = error.response_metadata()
    model_run.finished_at = datetime.now(UTC)
    fail_job(
        session, job, error_code=model_run.error_code, error_message=str(error), retryable=retryable
    )
    session.commit()


def _prompt_version(path: Path, prompt: str) -> str:
    return f"{path.stem}:{hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:12]}"


def _ordered_union(values: Any) -> list[str]:
    return list(dict.fromkeys(item for group in values for item in group))


def _run_payload(run: TopicBuildRun, *, resumed: bool) -> dict[str, Any]:
    return {
        "topic_build_run_id": str(run.id),
        "status": str(run.status),
        "event_count": run.event_count,
        "batch_count": run.batch_count,
        "candidate_count": run.candidate_count,
        "topic_count": run.topic_count,
        "unassigned_event_count": run.unassigned_event_count,
        "resumed": resumed,
    }


def topic_status(session: Session, plan_id: uuid.UUID) -> TopicStatus:
    run = session.scalar(
        select(TopicBuildRun)
        .where(TopicBuildRun.research_plan_id == plan_id)
        .order_by(TopicBuildRun.created_at.desc())
        .limit(1)
    )
    topic_count = int(
        session.scalar(select(func.count(Topic.id)).where(Topic.research_plan_id == plan_id)) or 0
    )
    assigned_ids = set(
        session.scalars(
            select(TopicEvent.event_id)
            .join(Topic, Topic.id == TopicEvent.topic_id)
            .where(Topic.research_plan_id == plan_id)
        )
    )
    failed_jobs = 0
    processed_event_count = 0
    candidate_count = 0
    if run is not None:
        generation_jobs = list(
            session.scalars(
                select(Job).where(
                    Job.object_id.like(f"{run.id}:%"),
                    Job.job_type == "topic_generate",
                )
            )
        )
        processed_event_count = sum(
            int((job.output or {}).get("event_count", 0))
            for job in generation_jobs
            if job.status == TaskStatus.COMPLETED
        )
        candidate_count = int(
            session.scalar(
                select(func.count(TopicCandidate.id)).where(
                    TopicCandidate.topic_build_run_id == run.id
                )
            )
            or 0
        )
        failed_jobs = int(
            session.scalar(
                select(func.count(Job.id)).where(
                    Job.object_id.like(f"{run.id}%"),
                    Job.status.in_([TaskStatus.FAILED_RETRYABLE, TaskStatus.FAILED_TERMINAL]),
                )
            )
            or 0
        )
    return TopicStatus(
        plan_id=str(plan_id),
        run_id=str(run.id) if run else None,
        status=str(run.status) if run else None,
        event_count=run.event_count if run else 0,
        processed_event_count=processed_event_count,
        candidate_count=candidate_count,
        topic_count=topic_count,
        assigned_event_count=len(assigned_ids),
        unassigned_event_count=run.unassigned_event_count if run else 0,
        failed_job_count=failed_jobs,
    )
