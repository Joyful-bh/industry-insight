from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from track_insight.core.enums import TaskStatus
from track_insight.core.errors import ContractError, ModelCallError
from track_insight.core.fingerprints import fingerprint
from track_insight.directions.clustering import duplicate_components, overlapping_groups
from track_insight.directions.contracts import (
    DirectionDedupOutput,
    DirectionGroupingOutput,
    DirectionStatus,
)
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.infrastructure.events import record_event
from track_insight.infrastructure.models import (
    Direction,
    DirectionBuildRun,
    DirectionTopic,
    ModelRun,
    PipelineRun,
    ResearchPlan,
    Topic,
)
from track_insight.settings import PocConfig
from track_insight.tracks.preparation import cached_embeddings

DIRECTION_PROCESSOR_VERSION = "topic-direction-v1-overlap"


@dataclass(frozen=True)
class TopicUnit:
    ref: str
    topic_ids: list[uuid.UUID]
    name: str
    definition: str

    @property
    def embedding_text(self) -> str:
        return f"{self.name}\n{self.definition}"


@dataclass
class DirectionDraft:
    name: str
    definition: str
    topic_ids: list[uuid.UUID]

    @property
    def embedding_text(self) -> str:
        return f"{self.name}\n{self.definition}"


class DirectionService:
    """Group Topics into evidence-linked fine-grained directions."""

    def __init__(self, *, client: BailianClient, config: PocConfig) -> None:
        self.client = client
        self.config = config

    def build(
        self, session: Session, *, plan_id: uuid.UUID, max_topics: int | None = None
    ) -> dict[str, Any]:
        plan = session.get(ResearchPlan, plan_id)
        if plan is None:
            raise ContractError("research plan not found")
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

        grouping_path = self.config.directions.prompts.grouping
        dedup_path = self.config.directions.prompts.deduplication
        grouping_prompt = grouping_path.read_text(encoding="utf-8")
        dedup_prompt = dedup_path.read_text(encoding="utf-8")
        prompt_version = (
            f"group:{_prompt_version(grouping_path, grouping_prompt)}|"
            f"dedup:{_prompt_version(dedup_path, dedup_prompt)}"
        )
        input_fp = fingerprint(
            [(str(topic.id), topic.topic_fingerprint) for topic in topics],
            prompt_version,
            self.config.directions.model,
            self.config.directions.embedding_model,
            self.config.directions.group_topics,
            self.config.directions.overlap_ratio,
            self.config.directions.deduplication_similarity,
        )
        run = session.scalar(
            select(DirectionBuildRun).where(
                DirectionBuildRun.research_plan_id == plan_id,
                DirectionBuildRun.input_fingerprint == input_fp,
                DirectionBuildRun.processor_version == DIRECTION_PROCESSOR_VERSION,
            )
        )
        if run is not None and run.status == TaskStatus.COMPLETED:
            return _run_payload(run, resumed=True)

        pipeline = PipelineRun(
            run_type="topic_to_direction",
            status=TaskStatus.RUNNING,
            input_payload={"plan_id": str(plan_id), "topic_count": len(topics)},
            started_at=datetime.now(UTC),
        )
        session.add(pipeline)
        session.flush()
        if run is None:
            run = DirectionBuildRun(
                research_plan_id=plan_id,
                pipeline_run_id=pipeline.id,
                status=TaskStatus.RUNNING,
                input_fingerprint=input_fp,
                processor_version=DIRECTION_PROCESSOR_VERSION,
                prompt_version=prompt_version[:100],
                model=self.config.directions.model,
                topic_ids=[str(topic.id) for topic in topics],
                topic_count=len(topics),
                started_at=datetime.now(UTC),
            )
            session.add(run)
            session.flush()
        else:
            run.pipeline_run_id = pipeline.id
            run.status = TaskStatus.RUNNING
            run.error_code = None
            run.error_message = None
            run.finished_at = None
        record_event(
            session,
            event_type="direction.run.started",
            message="Topic-to-Direction run started",
            pipeline_run_id=pipeline.id,
            entity_type="direction_build_run",
            entity_id=run.id,
            details=pipeline.input_payload,
        )
        session.commit()

        try:
            units = _topic_units(topics)
            vectors, grouping_embedding_usage = cached_embeddings(
                units, self.client, self.config.directions.embedding_model
            )
            groups = overlapping_groups(
                vectors,
                max_topics=self.config.directions.group_topics,
                overlap_ratio=self.config.directions.overlap_ratio,
            )
            drafts: list[DirectionDraft] = []
            ref_to_unit = {unit.ref: unit for unit in units}
            for group_number, group in enumerate(groups, start=1):
                group_refs = {units[index].ref for index in group.all_indices}
                payload = {
                    "topics": [
                        {
                            "topic_ref": units[index].ref,
                            "name": units[index].name,
                            "definition": units[index].definition,
                        }
                        for index in group.all_indices
                    ],
                }
                output = self._complete_cached(
                    session,
                    pipeline,
                    task_type="topic_direction_grouping",
                    prompt=grouping_prompt,
                    prompt_version=_prompt_version(grouping_path, grouping_prompt),
                    payload=payload,
                    output_model=DirectionGroupingOutput,
                )
                _validate_group_output(output, group_refs)
                for item in output.directions:
                    topic_ids = [
                        topic_id
                        for ref in _ordered_unique(item.topic_refs)
                        for topic_id in ref_to_unit[ref].topic_ids
                    ]
                    drafts.append(
                        DirectionDraft(
                            name=item.name.strip(),
                            definition=item.definition.strip(),
                            topic_ids=_ordered_unique(topic_ids),
                        )
                    )
                record_event(
                    session,
                    event_type="direction.group.completed",
                    message=f"Direction group completed: {group_number}",
                    pipeline_run_id=pipeline.id,
                    entity_type="direction_build_run",
                    entity_id=run.id,
                    details={
                        "group_number": group_number,
                        "topics": len(group_refs),
                        "directions": len(output.directions),
                    },
                )
                session.commit()

            drafts = _combine_exact_drafts(drafts)
            drafts, dedup_stats = self._deduplicate(
                session, pipeline, drafts, dedup_prompt, dedup_path
            )
            assigned = {topic_id for draft in drafts for topic_id in draft.topic_ids}
            expected = {topic.id for topic in topics}
            discarded = expected - assigned

            for draft in drafts:
                direction_fp = fingerprint(
                    draft.name, draft.definition, sorted(map(str, draft.topic_ids))
                )
                direction = Direction(
                    research_plan_id=plan_id,
                    direction_build_run_id=run.id,
                    canonical_key=f"direction_{direction_fp[:20]}",
                    name=draft.name,
                    definition=draft.definition,
                    direction_fingerprint=direction_fp,
                )
                session.add(direction)
                session.flush()
                session.add_all(
                    DirectionTopic(
                        direction_id=direction.id,
                        topic_id=topic_id,
                        direction_build_run_id=run.id,
                    )
                    for topic_id in draft.topic_ids
                )

            run.discarded_topic_ids = sorted(map(str, discarded))
            run.direction_count = len(drafts)
            run.assigned_topic_count = len(assigned)
            run.group_count = len(groups)
            run.statistics = {
                "topic_units": len(units),
                "grouping_embedding_usage": grouping_embedding_usage,
                "deduplication": dedup_stats,
            }
            run.status = TaskStatus.COMPLETED
            run.finished_at = datetime.now(UTC)
            pipeline.status = TaskStatus.COMPLETED
            pipeline.finished_at = datetime.now(UTC)
            pipeline.counters = {
                "topics": len(topics),
                "directions": len(drafts),
                "discarded_topics": len(discarded),
                "groups": len(groups),
            }
            record_event(
                session,
                event_type="direction.run.completed",
                message="Topic-to-Direction run completed",
                pipeline_run_id=pipeline.id,
                entity_type="direction_build_run",
                entity_id=run.id,
                details=pipeline.counters,
            )
            session.commit()
            return _run_payload(run, resumed=False)
        except (Exception, KeyboardInterrupt) as error:
            session.rollback()
            failed_run = session.get(DirectionBuildRun, run.id)
            failed_pipeline = session.get(PipelineRun, pipeline.id)
            if failed_run is not None:
                failed_run.status = TaskStatus.FAILED_RETRYABLE
                failed_run.error_code = getattr(error, "code", type(error).__name__)
                failed_run.error_message = str(error)
                failed_run.finished_at = datetime.now(UTC)
            if failed_pipeline is not None:
                failed_pipeline.status = TaskStatus.FAILED_RETRYABLE
                failed_pipeline.finished_at = datetime.now(UTC)
                failed_pipeline.counters = {
                    "error": {"type": type(error).__name__, "message": str(error)}
                }
            record_event(
                session,
                event_type="direction.run.failed",
                message="Topic-to-Direction run failed",
                level="ERROR",
                pipeline_run_id=pipeline.id,
                entity_type="direction_build_run",
                entity_id=run.id,
                details={"type": type(error).__name__, "message": str(error)},
            )
            session.commit()
            raise

    def _deduplicate(
        self,
        session: Session,
        pipeline: PipelineRun,
        drafts: list[DirectionDraft],
        prompt: str,
        prompt_path: Path,
    ) -> tuple[list[DirectionDraft], dict[str, Any]]:
        if len(drafts) < 2:
            return drafts, {"candidate_components": 0, "merged_groups": 0}
        vectors, usage = cached_embeddings(
            drafts, self.client, self.config.directions.embedding_model
        )
        components = duplicate_components(
            vectors, minimum_similarity=self.config.directions.deduplication_similarity
        )
        merge_groups = []
        for component in components:
            if len(component) > self.config.directions.deduplication_batch_directions:
                raise ContractError(
                    "Direction duplicate candidate component exceeds "
                    "directions.deduplication_batch_directions; increase the configured "
                    "limit so the component is reviewed in one pass"
                )
            refs = {f"D{index + 1}": index for index in component}
            payload = {
                "directions": [
                    {
                        "direction_ref": ref,
                        "name": drafts[index].name,
                        "definition": drafts[index].definition,
                    }
                    for ref, index in refs.items()
                ]
            }
            output = self._complete_cached(
                session,
                pipeline,
                task_type="direction_deduplication",
                prompt=prompt,
                prompt_version=_prompt_version(prompt_path, prompt),
                payload=payload,
                output_model=DirectionDedupOutput,
            )
            _validate_dedup_output(output, set(refs))
            for group in output.merge_groups:
                merge_groups.append(
                    (
                        [refs[ref] for ref in group.direction_refs],
                        group.name.strip(),
                        group.definition.strip(),
                    )
                )
        return _apply_merge_groups(drafts, merge_groups), {
            "candidate_components": len(components),
            "merged_groups": len(merge_groups),
            "embedding_usage": usage,
        }

    def _complete_cached(
        self,
        session: Session,
        pipeline: PipelineRun,
        *,
        task_type: str,
        prompt: str,
        prompt_version: str,
        payload: dict[str, Any],
        output_model,
    ):
        input_fp = fingerprint(payload, prompt_version, self.config.directions.model)
        cached = session.scalar(
            select(ModelRun)
            .where(
                ModelRun.task_type == task_type,
                ModelRun.input_fingerprint == input_fp,
                ModelRun.model == self.config.directions.model,
                ModelRun.status == TaskStatus.COMPLETED,
            )
            .order_by(ModelRun.created_at.desc())
            .limit(1)
        )
        if cached is not None:
            try:
                return output_model.model_validate(cached.structured_output)
            except ValueError:
                pass
        model_run = ModelRun(
            pipeline_run_id=pipeline.id,
            task_type=task_type,
            status=TaskStatus.RUNNING,
            model=self.config.directions.model,
            prompt_version=prompt_version,
            input_fingerprint=input_fp,
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
                output_model=output_model,
                model=self.config.directions.model,
                max_tokens=self.config.directions.max_output_tokens,
                enable_thinking=False,
            )
            model_run.status = TaskStatus.COMPLETED
            model_run.request_id = result.request_id
            model_run.usage = result.usage
            model_run.raw_response = result.raw_response
            model_run.structured_output = result.output.model_dump(mode="json")
            model_run.finished_at = datetime.now(UTC)
            session.commit()
            return result.output
        except (ModelCallError, ContractError) as error:
            model_run.status = TaskStatus.FAILED_RETRYABLE
            model_run.error_code = getattr(error, "code", type(error).__name__)
            model_run.error_message = str(error)
            model_run.finished_at = datetime.now(UTC)
            session.commit()
            raise


def direction_status(session: Session, plan_id: uuid.UUID) -> DirectionStatus:
    run = session.scalar(
        select(DirectionBuildRun)
        .where(DirectionBuildRun.research_plan_id == plan_id)
        .order_by(DirectionBuildRun.created_at.desc())
        .limit(1)
    )
    if run is None:
        return DirectionStatus(
            plan_id=str(plan_id),
            run_id=None,
            status=None,
            topic_count=0,
            direction_count=0,
            assigned_topic_count=0,
            discarded_topic_count=0,
            group_count=0,
        )
    return DirectionStatus(
        plan_id=str(plan_id),
        run_id=str(run.id),
        status=str(run.status),
        topic_count=run.topic_count,
        direction_count=run.direction_count,
        assigned_topic_count=run.assigned_topic_count,
        discarded_topic_count=len(run.discarded_topic_ids),
        group_count=run.group_count,
    )


def _topic_units(topics: list[Topic]) -> list[TopicUnit]:
    buckets: dict[tuple[str, str], list[Topic]] = {}
    for topic in topics:
        key = (_normalized(topic.label), _normalized(topic.definition))
        buckets.setdefault(key, []).append(topic)
    units = []
    for grouped in buckets.values():
        first = grouped[0]
        units.append(
            TopicUnit(
                ref=f"T{len(units) + 1}",
                topic_ids=[topic.id for topic in grouped],
                name=first.label[:200],
                definition=first.definition[:300],
            )
        )
    return units


def _validate_group_output(output: DirectionGroupingOutput, available_refs: set[str]) -> None:
    referenced = {ref for item in output.directions for ref in item.topic_refs}
    unknown = sorted(referenced - available_refs)
    if unknown:
        raise ContractError(f"Direction group referenced unknown Topics: {unknown}")


def _validate_dedup_output(output: DirectionDedupOutput, available_refs: set[str]) -> None:
    refs = {ref for group in output.merge_groups for ref in group.direction_refs}
    if not refs <= available_refs:
        raise ContractError("Direction deduplication referenced an unknown direction")


def _combine_exact_drafts(drafts: list[DirectionDraft]) -> list[DirectionDraft]:
    combined: dict[tuple[str, str], DirectionDraft] = {}
    for draft in drafts:
        key = (_normalized(draft.name), _normalized(draft.definition))
        if key not in combined:
            combined[key] = DirectionDraft(draft.name, draft.definition, list(draft.topic_ids))
        else:
            combined[key].topic_ids = _ordered_unique([*combined[key].topic_ids, *draft.topic_ids])
    return list(combined.values())


def _apply_merge_groups(
    drafts: list[DirectionDraft], groups: list[tuple[list[int], str, str]]
) -> list[DirectionDraft]:
    consumed: set[int] = set()
    merged: list[DirectionDraft] = []
    for indices, name, definition in groups:
        if consumed.intersection(indices):
            raise ContractError("Direction deduplication groups overlap")
        consumed.update(indices)
        merged.append(
            DirectionDraft(
                name=name,
                definition=definition,
                topic_ids=_ordered_unique(
                    topic_id for index in indices for topic_id in drafts[index].topic_ids
                ),
            )
        )
    merged.extend(draft for index, draft in enumerate(drafts) if index not in consumed)
    return merged


def _normalized(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def _ordered_unique(values) -> list:
    return list(dict.fromkeys(values))


def _prompt_version(path: Path, prompt: str) -> str:
    return f"{path.name}:{fingerprint(prompt)[:12]}"


def _run_payload(run: DirectionBuildRun, *, resumed: bool) -> dict[str, Any]:
    return {
        "run_id": str(run.id),
        "plan_id": str(run.research_plan_id),
        "status": str(run.status),
        "topic_count": run.topic_count,
        "direction_count": run.direction_count,
        "assigned_topic_count": run.assigned_topic_count,
        "discarded_topic_count": len(run.discarded_topic_ids),
        "group_count": run.group_count,
        "resumed": resumed,
    }
