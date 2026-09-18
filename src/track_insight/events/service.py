import hashlib
import json
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from track_insight.content.contracts import QualityStatus
from track_insight.core.enums import DatePrecision, SmbRelevance, TaskStatus
from track_insight.core.errors import ContractError, ModelCallError
from track_insight.core.fingerprints import fingerprint
from track_insight.events.contracts import (
    DirectEventOutput,
    ExtractedEvent,
    EventEvidenceCoverage,
    PageEventOutput,
    Stage2Status,
)
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.infrastructure.database import session_scope
from track_insight.infrastructure.events import record_event
from track_insight.infrastructure.jobs import (
    claim_job,
    complete_job,
    enqueue_job,
    fail_job,
    requeue_expired_jobs,
)
from track_insight.infrastructure.models import (
    Event,
    EventEvidence,
    Job,
    ModelRun,
    PageAnalysis,
    PageCapture,
    PipelineRun,
    ResearchPlan,
    ResearchWorkPackage,
    SearchTask,
    Topic,
    TopicEvent,
    UrlCandidate,
    UrlDiscovery,
)
from track_insight.settings import PocConfig, get_settings

ANALYSIS_PROCESSOR_VERSION = "page-analysis-v15-quality"


def enqueue_page_analysis(session: Session, capture: PageCapture, config: PocConfig) -> Job:
    prompt = config.stage2.prompts.event_extraction.read_text(encoding="utf-8")
    prompt_version = _prompt_version(config.stage2.prompts.event_extraction, prompt)
    return enqueue_job(
        session,
        job_type="page_analyze",
        object_type="page_capture",
        object_id=str(capture.id),
        input_fingerprint=fingerprint(
            capture.content_hash,
            prompt_version,
            config.stage2.analysis_model,
            config.stage2.max_model_input_chars,
        ),
        processor_version=ANALYSIS_PROCESSOR_VERSION,
        max_attempts=config.bailian.max_retries + 1,
    )


class EventService:
    def __init__(self, *, client: BailianClient, config: PocConfig) -> None:
        self.client = client
        self.config = config

    def extract_work(self, *, limit: int = 5) -> dict[str, int | str]:
        if limit < 1:
            raise ValueError("limit must be positive")
        pipeline_run_id = uuid.uuid4()
        counts = {"completed": 0, "failed": 0, "skipped": 0, "event_count": 0}
        with session_scope() as session:
            _enqueue_current_analysis_jobs(session, self.config)
            run = PipelineRun(
                id=pipeline_run_id,
                run_type="page_event_extraction",
                status=TaskStatus.RUNNING,
                input_payload={"limit": limit, "model": self.config.stage2.analysis_model},
                started_at=datetime.now(UTC),
            )
            session.add(run)
            session.commit()
            record_event(
                session,
                event_type="event_extract.run.started",
                message="Page review and event extraction run started",
                pipeline_run_id=run.id,
                details=run.input_payload,
            )
            session.commit()

        for _ in range(limit):
            with session_scope() as session:
                requeue_expired_jobs(session)
                job = claim_job(
                    session,
                    worker_id=f"event-extract-{pipeline_run_id}",
                    lease_seconds=get_settings().job_lease_seconds,
                    job_type="page_analyze",
                    processor_version=ANALYSIS_PROCESSOR_VERSION,
                )
                if job is None:
                    break
                job.pipeline_run_id = pipeline_run_id
                capture = session.get(PageCapture, uuid.UUID(job.object_id))
                if capture is None:
                    fail_job(
                        session,
                        job,
                        error_code="page_capture_missing",
                        error_message="page capture row does not exist",
                        retryable=False,
                    )
                    counts["failed"] += 1
                    session.commit()
                    continue
                if (
                    capture.status != TaskStatus.COMPLETED
                    or capture.quality_status != QualityStatus.USABLE
                ):
                    fail_job(
                        session,
                        job,
                        error_code="page_not_usable",
                        error_message="page capture is not complete and usable",
                        retryable=False,
                    )
                    counts["skipped"] += 1
                    session.commit()
                    continue
                analysis = session.scalar(
                    select(PageAnalysis).where(PageAnalysis.page_capture_id == capture.id)
                )
                if analysis is None:
                    analysis = PageAnalysis(page_capture_id=capture.id, status=TaskStatus.RUNNING)
                    session.add(analysis)
                    session.flush()
                else:
                    analysis.status = TaskStatus.RUNNING
                    analysis.error_code = None
                    analysis.error_message = None
                started = time.monotonic()
                prompt = self.config.stage2.prompts.event_extraction.read_text(
                    encoding="utf-8"
                )
                prompt_version = _prompt_version(
                    self.config.stage2.prompts.event_extraction, prompt
                )
                model_run = ModelRun(
                    pipeline_run_id=pipeline_run_id,
                    task_type="page_event_extraction",
                    status=TaskStatus.RUNNING,
                    provider="bailian",
                    model=self.config.stage2.analysis_model,
                    prompt_version=prompt_version,
                    input_fingerprint=fingerprint(
                        capture.content_hash,
                        self.config.stage2.analysis_model,
                        prompt_version,
                        ANALYSIS_PROCESSOR_VERSION,
                        self.config.stage2.max_model_input_chars,
                    ),
                    tool_config={"type": "structured_completion", "enable_thinking": False},
                    started_at=datetime.now(UTC),
                )
                session.add(model_run)
                session.flush()
                analysis.model_run_id = model_run.id
                record_event(
                    session,
                    event_type="event_extract.page.started",
                    message="Page review and event extraction started",
                    pipeline_run_id=pipeline_run_id,
                    job_id=job.id,
                    model_run_id=model_run.id,
                    entity_type="page_capture",
                    entity_id=capture.id,
                    details={"url": capture.requested_url, "model": model_run.model},
                )
                session.commit()
                output = None
                result = None
                try:
                    output, result = self._analyze(session, capture)
                    _sanitize_evidence(
                        output,
                        capture.content_text or "",
                        title=capture.title,
                        published_at=capture.published_at,
                    )
                except (ModelCallError, ContractError) as error:
                    if isinstance(error, ModelCallError):
                        retryable = error.retryable and job.attempts < job.max_attempts
                        model_run.status = (
                            TaskStatus.FAILED_RETRYABLE if retryable else TaskStatus.FAILED_TERMINAL
                        )
                        model_run.error_code = error.code
                        model_run.error_message = str(error)
                        request_id, usage, raw = error.response_metadata()
                        model_run.request_id = request_id
                        model_run.usage = usage
                        model_run.raw_response = raw
                        error_code = error.code
                    else:
                        model_run.status = TaskStatus.FAILED_TERMINAL
                        model_run.error_code = "invalid_event_evidence"
                        model_run.error_message = str(error)
                        if result is not None and output is not None:
                            model_run.request_id = result.request_id
                            model_run.usage = result.usage
                            model_run.raw_response = result.raw_response
                            model_run.structured_output = output.model_dump(mode="json")
                        error_code = "invalid_event_evidence"
                        retryable = False
                    model_run.finished_at = datetime.now(UTC)
                    analysis.status = model_run.status
                    analysis.error_code = error_code
                    analysis.error_message = str(error)
                    fail_job(
                        session,
                        job,
                        error_code=error_code,
                        error_message=str(error),
                        retryable=retryable,
                    )
                    counts["failed"] += 1
                    record_event(
                        session,
                        event_type="event_extract.page.failed",
                        message="Page review and event extraction failed",
                        level="ERROR",
                        pipeline_run_id=pipeline_run_id,
                        job_id=job.id,
                        model_run_id=model_run.id,
                        entity_type="page_analysis",
                        entity_id=analysis.id,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        details={
                            "url": capture.requested_url,
                            "error_code": error_code,
                            "retryable": retryable,
                            "error_message": str(error),
                        },
                    )
                    session.commit()
                    continue

                model_run.status = TaskStatus.COMPLETED
                model_run.request_id = result.request_id
                model_run.usage = result.usage
                model_run.raw_response = result.raw_response
                model_run.structured_output = output.model_dump(mode="json")
                model_run.finished_at = datetime.now(UTC)
                analysis.status = TaskStatus.COMPLETED
                analysis.relevance = output.page_review.relevance
                analysis.document_type = output.page_review.document_type
                analysis.reason = output.page_review.reason
                analysis.regions = output.page_review.regions
                analysis.industries = output.page_review.industries
                analysis.content_sufficient = output.page_review.content_sufficient
                analysis.structured_output = output.model_dump(mode="json")
                analysis.error_code = None
                analysis.error_message = None
                event_count = self._replace_events(session, analysis, capture, output)
                complete_job(
                    session, job, {"event_count": event_count, "relevance": str(analysis.relevance)}
                )
                counts["completed"] += 1
                counts["event_count"] += event_count
                record_event(
                    session,
                    event_type="event_extract.page.completed",
                    message="Page review and event extraction completed",
                    pipeline_run_id=pipeline_run_id,
                    job_id=job.id,
                    model_run_id=model_run.id,
                    entity_type="page_analysis",
                    entity_id=analysis.id,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    details={
                        "url": capture.requested_url,
                        "relevance": str(analysis.relevance),
                        "document_type": str(analysis.document_type),
                        "event_count": event_count,
                        "request_id": result.request_id,
                        "usage": result.usage,
                    },
                )
                session.commit()

        with session_scope() as session:
            run = session.get(PipelineRun, pipeline_run_id)
            if run:
                run.counters = counts.copy()
                run.status = (
                    TaskStatus.COMPLETED if counts["failed"] == 0 else TaskStatus.FAILED_RETRYABLE
                )
                run.finished_at = datetime.now(UTC)
                record_event(
                    session,
                    event_type=(
                        "event_extract.run.completed"
                        if counts["failed"] == 0
                        else "event_extract.run.failed"
                    ),
                    message="Page review and event extraction run finished",
                    pipeline_run_id=run.id,
                    details=counts.copy(),
                )
        return {"pipeline_run_id": str(pipeline_run_id), **counts}

    def _analyze(self, session: Session, capture: PageCapture):
        prompt = self.config.stage2.prompts.event_extraction.read_text(encoding="utf-8")
        context = _research_context(session, capture.url_candidate_id)
        body = capture.content_text or ""
        if len(body) > self.config.stage2.max_model_input_chars:
            body = body[: self.config.stage2.max_model_input_chars]
        source_payload = {
            "candidate": {
                "url": capture.final_url or capture.requested_url,
                "title": capture.title,
                "published_at": capture.published_at.isoformat() if capture.published_at else None,
                "acquisition_method": capture.acquisition_method,
                "source_class": context["source_classes"],
            },
            "research_contexts": context["contexts"],
            "page_text": body,
        }
        result = self.client.complete_structured(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(source_payload, ensure_ascii=False)},
            ],
            output_model=DirectEventOutput,
            model=self.config.stage2.analysis_model,
            max_tokens=self.config.stage2.analysis_max_output_tokens,
            enable_thinking=False,
        )
        review = result.output.page_review
        events = [
            ExtractedEvent(
                event_type=event.event_type,
                event_status=event.event_status,
                title=event.title,
                summary=event.summary,
                signal_date=event.signal_date,
                date_precision=(
                    DatePrecision.DAY if event.signal_date is not None else DatePrecision.UNKNOWN
                ),
                regions=event.regions,
                industries=review.industries,
                industry_objects=event.industry_objects,
                topic_hint=event.topic_hint,
                entities=[],
                chain_roles=[],
                smb_relevance=SmbRelevance.UNCLEAR,
                smb_reason="留待 Track 阶段评估",
                confidence=0.8,
                evidence=event.evidence,
                evidence_coverage=EventEvidenceCoverage(),
            )
            for event in result.output.events
        ]
        output = PageEventOutput(page_review=review, events=events)
        result.output = output
        return output, result

    @staticmethod
    def _replace_events(
        session: Session,
        analysis: PageAnalysis,
        capture: PageCapture,
        output: PageEventOutput,
    ) -> int:
        previous = list(
            session.scalars(select(Event.id).where(Event.page_analysis_id == analysis.id))
        )
        if previous:
            affected_topic_ids = list(
                session.scalars(
                    select(TopicEvent.topic_id).where(TopicEvent.event_id.in_(previous)).distinct()
                )
            )
            session.execute(delete(TopicEvent).where(TopicEvent.event_id.in_(previous)))
            session.execute(delete(EventEvidence).where(EventEvidence.event_id.in_(previous)))
            session.execute(delete(Event).where(Event.id.in_(previous)))
            session.flush()
            for topic_id in affected_topic_ids:
                topic = session.get(Topic, topic_id)
                if topic is None:
                    continue
                remaining = list(
                    session.scalars(
                        select(Event)
                        .join(TopicEvent, TopicEvent.event_id == Event.id)
                        .where(TopicEvent.topic_id == topic_id)
                    )
                )
                dates = [event.signal_date for event in remaining if event.signal_date]
                topic.event_count = len(remaining)
                topic.first_seen_at = min(dates) if dates else None
                topic.latest_seen_at = max(dates) if dates else None
                topic.regions = list(
                    dict.fromkeys(item for event in remaining for item in event.regions)
                )
                topic.industries = list(
                    dict.fromkeys(item for event in remaining for item in event.industries)
                )
                if not remaining:
                    topic.status = "archived"
        evidence_type = capture.acquisition_method or "http_html"
        seen_fingerprints: set[str] = set()
        persisted_count = 0
        for proposed in output.events:
            event_fingerprint = fingerprint(
                proposed.event_type,
                proposed.event_status,
                proposed.title.strip().casefold(),
                proposed.summary.strip().casefold(),
                proposed.signal_date,
                proposed.industry_objects,
            )
            if event_fingerprint in seen_fingerprints:
                continue
            seen_fingerprints.add(event_fingerprint)
            persisted_count += 1
            event = Event(
                page_analysis_id=analysis.id,
                sequence_no=persisted_count,
                event_type=proposed.event_type,
                event_status=proposed.event_status,
                title=proposed.title,
                summary=proposed.summary,
                signal_date=proposed.signal_date,
                date_precision=proposed.date_precision,
                regions=proposed.regions,
                industries=proposed.industries,
                industry_objects=proposed.industry_objects,
                topic_hint=proposed.topic_hint,
                entities=[entity.model_dump(mode="json") for entity in proposed.entities],
                chain_roles=proposed.chain_roles,
                smb_relevance=proposed.smb_relevance,
                smb_reason=proposed.smb_reason,
                confidence=proposed.confidence,
                event_fingerprint=event_fingerprint,
            )
            session.add(event)
            session.flush()
            seen: set[str] = set()
            for quote in proposed.evidence:
                evidence_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()
                if evidence_hash in seen:
                    continue
                seen.add(evidence_hash)
                start, end = _find_evidence(capture.content_text or "", quote)
                session.add(
                    EventEvidence(
                        event_id=event.id,
                        page_capture_id=capture.id,
                        evidence_text=quote,
                        evidence_type=str(evidence_type),
                        start_offset=start,
                        end_offset=end,
                        source_url=capture.final_url or capture.requested_url,
                        evidence_hash=evidence_hash,
                    )
                )
        return persisted_count


def stage2_status(session: Session, plan_id: uuid.UUID) -> Stage2Status:
    candidate_ids = (
        select(UrlCandidate.id.label("candidate_id"))
        .join(UrlDiscovery, UrlDiscovery.url_candidate_id == UrlCandidate.id)
        .join(ResearchWorkPackage, ResearchWorkPackage.id == UrlDiscovery.research_work_package_id)
        .where(
            ResearchWorkPackage.research_plan_id == plan_id,
            UrlCandidate.url_type == "content_page",
            UrlCandidate.decision.in_(["keep", "maybe"]),
        )
        .distinct()
        .subquery()
    )
    candidate_count = int(session.scalar(select(func.count()).select_from(candidate_ids)) or 0)
    capture_rows = list(
        session.execute(
            select(
                PageCapture.status, PageCapture.acquisition_method, PageCapture.quality_status
            ).join(candidate_ids, candidate_ids.c.candidate_id == PageCapture.url_candidate_id)
        )
    )
    capture_status_counts = _count_values(row.status for row in capture_rows)
    method_counts = _count_values(row.acquisition_method for row in capture_rows)
    quality_counts = _count_values(row.quality_status for row in capture_rows)
    analysis_rows = list(
        session.execute(
            select(PageAnalysis.status, PageAnalysis.relevance)
            .join(PageCapture, PageCapture.id == PageAnalysis.page_capture_id)
            .join(candidate_ids, candidate_ids.c.candidate_id == PageCapture.url_candidate_id)
        )
    )
    analysis_status_counts = _count_values(row.status for row in analysis_rows)
    relevance_counts = _count_values(row.relevance for row in analysis_rows)
    event_query = (
        select(Event.event_type, func.count(Event.id))
        .join(PageAnalysis, PageAnalysis.id == Event.page_analysis_id)
        .join(PageCapture, PageCapture.id == PageAnalysis.page_capture_id)
        .join(candidate_ids, candidate_ids.c.candidate_id == PageCapture.url_candidate_id)
        .group_by(Event.event_type)
    )
    event_type_counts = {str(kind): int(count) for kind, count in session.execute(event_query)}
    failure_counts: dict[str, int] = {}
    for capture in session.scalars(
        select(PageCapture).join(
            candidate_ids, candidate_ids.c.candidate_id == PageCapture.url_candidate_id
        )
    ):
        if capture.error_code:
            failure_counts[capture.error_code] = failure_counts.get(capture.error_code, 0) + 1
    for analysis in session.scalars(
        select(PageAnalysis)
        .join(PageCapture, PageCapture.id == PageAnalysis.page_capture_id)
        .join(candidate_ids, candidate_ids.c.candidate_id == PageCapture.url_candidate_id)
    ):
        if analysis.error_code:
            failure_counts[analysis.error_code] = failure_counts.get(analysis.error_code, 0) + 1
    return Stage2Status(
        plan_id=str(plan_id),
        candidate_count=candidate_count,
        enqueued_capture_count=len(capture_rows),
        capture_status_counts=capture_status_counts,
        acquisition_method_counts=method_counts,
        quality_status_counts=quality_counts,
        analysis_status_counts=analysis_status_counts,
        relevance_counts=relevance_counts,
        event_count=sum(event_type_counts.values()),
        event_type_counts=event_type_counts,
        failure_counts=failure_counts,
    )


def list_events(session: Session, plan_id: uuid.UUID, limit: int = 50) -> list[dict[str, Any]]:
    events = session.scalars(
        select(Event)
        .join(PageAnalysis, PageAnalysis.id == Event.page_analysis_id)
        .join(PageCapture, PageCapture.id == PageAnalysis.page_capture_id)
        .where(
            select(UrlDiscovery.id)
            .join(
                ResearchWorkPackage,
                ResearchWorkPackage.id == UrlDiscovery.research_work_package_id,
            )
            .where(
                UrlDiscovery.url_candidate_id == PageCapture.url_candidate_id,
                ResearchWorkPackage.research_plan_id == plan_id,
            )
            .exists()
        )
        .order_by(Event.created_at.desc(), Event.sequence_no)
        .limit(limit)
    )
    rows = []
    seen: set[uuid.UUID] = set()
    for event in events:
        if event.id in seen:
            continue
        seen.add(event.id)
        analysis = session.get(PageAnalysis, event.page_analysis_id)
        capture = session.get(PageCapture, analysis.page_capture_id) if analysis else None
        evidence = list(
            session.scalars(select(EventEvidence).where(EventEvidence.event_id == event.id))
        )
        rows.append(
            {
                "event_id": str(event.id),
                "event_type": event.event_type,
                "event_status": event.event_status,
                "title": event.title,
                "summary": event.summary,
                "signal_date": event.signal_date.isoformat() if event.signal_date else None,
                "date_precision": event.date_precision,
                "regions": event.regions,
                "industries": event.industries,
                "industry_objects": event.industry_objects,
                "topic_hint": event.topic_hint,
                "entities": event.entities,
                "chain_roles": event.chain_roles,
                "smb_relevance": event.smb_relevance,
                "smb_reason": event.smb_reason,
                "confidence": event.confidence,
                "source_url": capture.final_url or capture.requested_url if capture else None,
                "evidence": [
                    {"text": item.evidence_text, "type": item.evidence_type} for item in evidence
                ],
            }
        )
    return rows


def _research_context(session: Session, candidate_id: uuid.UUID) -> dict[str, Any]:
    rows = session.execute(
        select(ResearchPlan, ResearchWorkPackage, SearchTask)
        .join(ResearchWorkPackage, ResearchWorkPackage.research_plan_id == ResearchPlan.id)
        .join(SearchTask, SearchTask.research_work_package_id == ResearchWorkPackage.id)
        .join(UrlDiscovery, UrlDiscovery.search_task_id == SearchTask.id)
        .where(UrlDiscovery.url_candidate_id == candidate_id)
    ).all()
    contexts = [
        {
            "regions": plan.regions,
            "industries": plan.industry_scopes,
            "date_range": [plan.start_date.isoformat(), plan.end_date.isoformat()],
            "work_package_objective": work_package.objective,
            "search_purpose": task.purpose,
            "signal_types": work_package.signal_types,
        }
        for plan, work_package, task in rows
    ]
    candidate = session.get(UrlCandidate, candidate_id)
    return {
        "contexts": contexts,
        "source_classes": [candidate.source_class] if candidate else [],
    }


def _sanitize_evidence(
    output: PageEventOutput,
    text: str,
    *,
    title: str | None = None,
    published_at: datetime | None = None,
) -> None:
    """Validate each Event independently using only verbatim text and numeric claims."""
    del published_at
    normalized_title = _normalize_evidence(title or "")
    supported_events = []
    for event in output.events:
        matched_quotes: list[str] = []
        for quote in event.evidence:
            for matched in _match_evidence_fragments(text, quote):
                if matched not in matched_quotes:
                    matched_quotes.append(matched)
        substantive = [
            quote
            for quote in matched_quotes
            if len(_normalize_evidence(quote)) >= 20
            and _normalize_evidence(quote) != normalized_title
        ]
        material_numbers = _material_number_tokens(f"{event.title} {event.summary}")
        evidence_text = _normalize_evidence("".join(matched_quotes))
        unsupported_number = any(
            _normalize_evidence(token) not in evidence_text for token in material_numbers
        )
        if substantive and not unsupported_number:
            event.evidence = matched_quotes
            indexes = list(range(len(matched_quotes)))
            event.evidence_coverage.subject = indexes.copy()
            event.evidence_coverage.action = indexes.copy()
            event.evidence_coverage.date = []
            event.evidence_coverage.numbers = []
            supported_events.append(event)
    output.events = supported_events


def _material_number_tokens(value: str) -> list[str]:
    pattern = re.compile(
        r"\d+(?:\.\d+)?\s*(?:%|％|万元|亿元|万家|万台|万套|万平方米|万|亿|家|个|项|条|人|台|套|平方米)"
    )
    return list(dict.fromkeys(match.group(0) for match in pattern.finditer(value)))


def _find_evidence(text: str, quote: str) -> tuple[int | None, int | None]:
    start = text.find(quote)
    if start >= 0:
        return start, start + len(quote)
    return None, None


def _normalize_evidence(value: str) -> str:
    # Bailian's web extractor may append citation markers such as ^[1]^ to
    # otherwise verbatim text. They are transport artifacts, not page facts.
    without_citations = re.sub(r"\^?\[\d+\]\^?", "", value)
    return "".join(character.lower() for character in without_citations if character.isalnum())


def _match_evidence(text: str, quote: str) -> str | None:
    direct_start = text.find(quote)
    if direct_start >= 0:
        return text[direct_start : direct_start + len(quote)]

    normalized_quote = _normalize_evidence(quote)
    if not normalized_quote:
        return None
    normalized_chars: list[str] = []
    original_positions: list[int] = []
    citation_spans = [match.span() for match in re.finditer(r"\^?\[\d+\]\^?", text)]
    citation_index = 0
    for position, character in enumerate(text):
        while (
            citation_index < len(citation_spans) and position >= citation_spans[citation_index][1]
        ):
            citation_index += 1
        if (
            citation_index < len(citation_spans)
            and citation_spans[citation_index][0] <= position < citation_spans[citation_index][1]
        ):
            continue
        if character.isalnum():
            normalized_chars.append(character.lower())
            original_positions.append(position)
    normalized_body = "".join(normalized_chars)
    normalized_start = normalized_body.find(normalized_quote)
    if normalized_start < 0:
        return None
    original_start = original_positions[normalized_start]
    original_end = original_positions[normalized_start + len(normalized_quote) - 1] + 1
    return text[original_start:original_end]


def _match_evidence_fragments(text: str, quote: str) -> list[str]:
    fragments = [part.strip() for part in re.split(r"(?:\.\.\.|…+)", quote)]
    if len(fragments) == 1:
        matched = _match_evidence(text, quote)
        return [matched] if matched is not None else []
    matches: list[str] = []
    for fragment in fragments:
        if len(_normalize_evidence(fragment)) < 8:
            continue
        matched = _match_evidence(text, fragment)
        if matched is not None:
            matches.append(matched)
    return matches


def _enqueue_current_analysis_jobs(session: Session, config: PocConfig) -> None:
    captures = list(
        session.scalars(
            select(PageCapture).where(
                PageCapture.status == TaskStatus.COMPLETED,
                PageCapture.quality_status == QualityStatus.USABLE,
            )
        )
    )
    for capture in captures:
        job = enqueue_page_analysis(session, capture, config)
        analysis = session.scalar(
            select(PageAnalysis).where(PageAnalysis.page_capture_id == capture.id)
        )
        if (
            analysis is not None
            and analysis.status == TaskStatus.FAILED_TERMINAL
            and job.status in {TaskStatus.PENDING, TaskStatus.FAILED_RETRYABLE}
        ):
            analysis.status = TaskStatus.PENDING
            analysis.error_code = None
            analysis.error_message = None
    session.commit()


def _count_values(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        if value is not None:
            key = str(value)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _prompt_version(path: Path, content: str) -> str:
    return f"{path.stem}:{fingerprint(content)[:12]}"
