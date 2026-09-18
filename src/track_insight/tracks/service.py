import json
import re
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
    Topic,
    TopicEvent,
    TrackBuildRun,
    UrlCandidate,
)
from track_insight.settings import PocConfig
from track_insight.tracks.contracts import (
    CompactTrackBuildOutput,
    TrackAnalysisOutput,
    TrackBuildOutput,
    TrackStatus,
)

TRACK_PROCESSOR_VERSION = "topic-track-v7-short-refs"
ANALYSIS_PROCESSOR_VERSION = "track-analysis-v4-snapshotted-events"

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
            .order_by(Topic.label)
        )
        if max_topics is not None:
            statement = statement.limit(max_topics)
        topics = list(session.scalars(statement))
        if not topics:
            raise ContractError("no candidate Topics found for the research plan")
        prompt = self.config.stage4.prompts.track_generation.read_text(encoding="utf-8")
        prompt_version = _prompt_version(self.config.stage4.prompts.track_generation, prompt)
        input_fp = fingerprint(
            [(str(x.id), x.topic_fingerprint) for x in topics],
            prompt_version,
            self.config.stage4.model,
            self.config.stage4.minimum_supporting_events,
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
            input_payload={"plan_id": str(plan_id), "topic_count": len(topics)},
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
            prompt_version=prompt_version,
            model=self.config.stage4.model,
            topic_ids=[str(x.id) for x in topics],
            topic_count=len(topics),
            started_at=datetime.now(UTC),
        )
        if existing_run:
            run.pipeline_run_id = pipeline.id
            run.status = TaskStatus.RUNNING
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
        try:
            topic_refs = {f"T{index}": topic.id for index, topic in enumerate(topics, 1)}
            result = self.client.complete_structured(
                messages=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "topics": [
                                    _topic_payload(session, topic, topic_ref=topic_ref)
                                    for topic_ref, topic in zip(topic_refs, topics, strict=True)
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                output_model=CompactTrackBuildOutput,
                model=self.config.stage4.model,
                max_tokens=self.config.stage4.generation_max_output_tokens,
                enable_thinking=False,
            )
            result.output = _expand_topic_refs(
                session,
                result.output,
                topic_refs,
                {topic.id: topic for topic in topics},
            )
            adjustments = _normalize_build_output(session, result.output, topics)
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
            _validate_build(session, result.output, topics, self.config)
            _complete_model_run(model_run, result)
            self._persist_tracks(
                session,
                run,
                topics,
                result.output,
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
        assigned = {topic_id for track in result.output.tracks for topic_id in track.topic_ids}
        run.track_count = len(result.output.tracks)
        run.unassigned_topic_count = len(set(x.id for x in topics) - assigned)
        run.status = TaskStatus.COMPLETED
        run.finished_at = datetime.now(UTC)
        pipeline.status = TaskStatus.COMPLETED
        pipeline.finished_at = datetime.now(UTC)
        pipeline.counters = {
            "topics": len(topics),
            "tracks": run.track_count,
            "unassigned_topics": run.unassigned_topic_count,
        }
        record_event(
            session,
            event_type="track.run.completed",
            message="Topic-to-Track run completed",
            pipeline_run_id=pipeline.id,
            model_run_id=model_run.id,
            entity_type="track_build_run",
            entity_id=run.id,
            details=pipeline.counters,
        )
        session.commit()
        return _build_payload(run, resumed=False)

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
        touched: set[uuid.UUID] = set()
        for proposed in output.tracks:
            member_topic_ids = proposed.topic_ids
            supporting_event_ids = sorted(
                _event_ids_for_topic_ids(session, member_topic_ids), key=str
            )
            identity_fp = fingerprint(
                proposed.name.casefold(), proposed.definition.casefold()
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
                core_products_services=[],
                core_company_types=[],
                supporting_company_types=[],
                shared_demand_drivers=[],
                included_activities=[],
                excluded_activities=[],
                chain_roles=[],
                observable_company_features=[],
                possible_it_needs=[],
                supporting_event_ids=[str(x) for x in supporting_event_ids],
                granularity_assessment={},
                regions=_ordered_union(topic_map[x].regions for x in member_topic_ids),
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
            payload, event_ids, stats = _analysis_input(session, track)
            input_fp = fingerprint(
                track.track_fingerprint, payload, prompt_version, self.config.stage4.model
            )
            old = session.scalar(
                select(CandidateTrackAnalysis).where(
                    CandidateTrackAnalysis.candidate_track_id == track.id,
                    CandidateTrackAnalysis.input_fingerprint == input_fp,
                    CandidateTrackAnalysis.processor_version == ANALYSIS_PROCESSOR_VERSION,
                )
            )
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
                details={"track_name": track.name, "event_count": len(event_ids)},
            )
            session.commit()
            try:
                result = self.client.complete_structured(
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    output_model=TrackAnalysisOutput,
                    model=self.config.stage4.model,
                    max_tokens=self.config.stage4.analysis_max_output_tokens,
                    enable_thinking=False,
                )
                if not set(result.output.evidence_event_ids).issubset(event_ids):
                    raise ContractError("Track analysis references an unknown Event")
                _complete_model_run(model_run, result)
                analysis.summary = result.output.summary
                analysis.why_now = result.output.why_now
                track.enterprise_archetype = result.output.enterprise_archetype
                track.core_products_services = result.output.core_products_services
                track.core_company_types = result.output.core_company_types
                track.supporting_company_types = result.output.supporting_company_types
                track.shared_demand_drivers = result.output.shared_demand_drivers
                track.included_activities = result.output.included_activities
                track.excluded_activities = result.output.excluded_activities
                track.chain_roles = result.output.chain_roles
                track.observable_company_features = result.output.observable_company_features
                track.possible_it_needs = result.output.possible_it_needs
                analysis.signal_statistics = stats
                analysis.activity_assessment = result.output.activity_assessment.model_dump(
                    mode="json"
                )
                analysis.industry_chain_analysis = result.output.industry_chain_analysis
                analysis.smb_value_analysis = [
                    x.model_dump(mode="json") for x in result.output.smb_value_analysis
                ]
                analysis.evidence_event_ids = [str(x) for x in result.output.evidence_event_ids]
                analysis.uncertainties = result.output.uncertainties
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
                    details={"evidence_event_count": len(analysis.evidence_event_ids)},
                )
                completed += 1
            except (ModelCallError, ContractError) as error:
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
    session: Session, topic: Topic, *, topic_ref: str | None = None
) -> dict[str, Any]:
    events = list(
        session.scalars(
            select(Event)
            .join(TopicEvent, TopicEvent.event_id == Event.id)
            .where(TopicEvent.topic_id == topic.id)
            .order_by(Event.signal_date.desc().nulls_last())
            .limit(1)
        )
    )
    payload = {
        "label": topic.label,
        "definition": _compact_text(topic.definition, 260),
        "event_count": topic.event_count,
        "representative_events": [
            {
                "event_id": str(x.id),
                "event_type": x.event_type,
                "title": _compact_text(x.title, 180),
            }
            for x in events
        ],
    }
    payload["topic_ref" if topic_ref else "topic_id"] = topic_ref or str(topic.id)
    return payload


def _expand_topic_refs(
    session: Session,
    output: CompactTrackBuildOutput,
    topic_refs: dict[str, uuid.UUID],
    topics: dict[uuid.UUID, Topic],
) -> TrackBuildOutput:
    """Translate compact model-facing references back to persistent Topic IDs."""
    tracks = []
    for track in output.tracks:
        topic_ids = list(
            dict.fromkeys(topic_refs[ref] for ref in track.refs if ref in topic_refs)
        )
        if not topic_ids:
            continue
        event_count = len(_event_ids_for_topic_ids(session, topic_ids))
        if len(topic_ids) == 1:
            definition = topics[topic_ids[0]].definition
        else:
            definition = (
                f"围绕{track.name}开展相关产品、技术或服务研发、生产与交付的企业集合。"
            )
        tracks.append(
            {
                "name": track.name,
                "definition": definition,
                "topic_ids": topic_ids,
                "status": "candidate" if event_count >= 2 else "watchlist",
                "confidence": 0.75 if event_count >= 2 else 0.55,
            }
        )
    return TrackBuildOutput.model_validate({"tracks": tracks})


def _compact_text(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1].rstrip() + "…"


def _analysis_input(
    session: Session, track: CandidateTrack
) -> tuple[dict[str, Any], set[uuid.UUID], dict[str, Any]]:
    rows = list(
        session.execute(
            select(CandidateTrackTopic, Topic)
            .join(Topic, Topic.id == CandidateTrackTopic.topic_id)
            .where(CandidateTrackTopic.candidate_track_id == track.id)
        )
    )
    declared_event_ids = {uuid.UUID(x) for x in track.supporting_event_ids}
    events = (
        list(
            session.scalars(
                select(Event)
                .where(Event.id.in_(declared_event_ids))
                .order_by(Event.signal_date.desc().nulls_last(), Event.id)
            )
        )
        if declared_event_ids
        else []
    )
    # supporting_event_ids is the evidence snapshot saved when this Track was
    # built. Topic memberships are mutable across later Topic rebuilds, so they
    # must not be used to decide whether the Track's original evidence remains
    # available for analysis.
    event_ids = {event.id for event in events}
    evidence = (
        list(session.scalars(select(EventEvidence).where(EventEvidence.event_id.in_(event_ids))))
        if event_ids
        else []
    )
    source_urls = {x.source_url for x in evidence}
    source_classes = (
        sorted(
            set(
                session.scalars(
                    select(UrlCandidate.source_class)
                    .join(PageCapture, PageCapture.url_candidate_id == UrlCandidate.id)
                    .join(PageAnalysis, PageAnalysis.page_capture_id == PageCapture.id)
                    .join(Event, Event.page_analysis_id == PageAnalysis.id)
                    .where(Event.id.in_(event_ids))
                )
            )
        )
        if event_ids
        else []
    )
    event_types = sorted({x.event_type for x in events})
    regions = _ordered_union(x.regions for x in events)
    stats = {
        "reviewed_page_count": len(source_urls),
        "independent_event_count": len(events),
        "source_count": len(source_urls),
        "source_classes": source_classes,
        "event_types": event_types,
        "regions": regions,
        "new_regions": [],
    }
    payload = {
        "track": {
            "track_id": str(track.id),
            "name": track.name,
            "definition": track.definition,
            "enterprise_archetype": track.enterprise_archetype,
            "core_products_services": track.core_products_services,
            "core_company_types": track.core_company_types,
            "supporting_company_types": track.supporting_company_types,
            "shared_demand_drivers": track.shared_demand_drivers,
            "included_activities": track.included_activities,
            "excluded_activities": track.excluded_activities,
            "chain_roles": track.chain_roles,
            "observable_company_features": track.observable_company_features,
            "possible_it_needs": track.possible_it_needs,
        },
        "topics": [
            {**_topic_payload(session, topic), "role": relation.role} for relation, topic in rows
        ],
        "events": [
            {
                "event_id": str(x.id),
                "event_type": x.event_type,
                "title": x.title,
                "summary": x.summary,
                "signal_date": x.signal_date.isoformat() if x.signal_date else None,
                "regions": x.regions,
                "industries": x.industries,
                "chain_roles": x.chain_roles,
                "smb_relevance": x.smb_relevance,
            }
            for x in events
        ],
        "signal_statistics": stats,
    }
    return payload, event_ids, stats


def _event_ids_for_topic_ids(
    session: Session, topic_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """Deduplicate track events by identifier without comparing JSON columns."""
    if not topic_ids:
        return set()
    return set(
        session.scalars(
            select(TopicEvent.event_id)
            .where(TopicEvent.topic_id.in_(topic_ids))
            .distinct()
        )
    )


def _validate_build(
    session: Session,
    output: TrackBuildOutput,
    topics: list[Topic],
    config: PocConfig,
) -> None:
    ids = {x.id for x in topics}
    members = [topic_id for track in output.tracks for topic_id in track.topic_ids]
    if not set(members).issubset(ids):
        raise ContractError("candidate Track references an unknown Topic")
    topic_map = {x.id: x for x in topics}
    for track in output.tracks:
        member_topic_ids = set(track.topic_ids)
        allowed_event_ids = _event_ids_for_topic_ids(session, list(member_topic_ids))
        if len(allowed_event_ids) < config.stage4.minimum_supporting_events:
            track.status = "watchlist"
        if config.stage4.forbid_region_in_identity:
            regions = _ordered_union(topic_map[x].regions for x in member_topic_ids)
            if _identity_contains_region([track.name], regions):
                track.status = "watchlist"


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
            topic_map[topic_id].regions
            for topic_id in member_topic_ids
            if topic_id in topic_map
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


def _fail(model_run: ModelRun, run: TrackBuildRun, pipeline: PipelineRun, error: Exception) -> None:
    status = (
        TaskStatus.FAILED_RETRYABLE
        if isinstance(error, ModelCallError) and error.retryable
        else TaskStatus.FAILED_TERMINAL
    )
    model_run.status = status
    model_run.error_code = getattr(error, "code", "track_contract_error")
    model_run.error_message = str(error)
    model_run.finished_at = datetime.now(UTC)
    run.status = status
    run.error_code = model_run.error_code
    run.error_message = str(error)
    run.finished_at = datetime.now(UTC)
    pipeline.status = status
    pipeline.finished_at = datetime.now(UTC)


def _prompt_version(path: Path, content: str) -> str:
    return f"{path.stem}:{fingerprint(content)[:12]}"


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
