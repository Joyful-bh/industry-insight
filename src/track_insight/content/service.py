import hashlib
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from track_insight.content.contracts import AcquisitionMethod, PageContent, QualityStatus
from track_insight.content.fetcher import HttpFetchResult, PageFetcher
from track_insight.content.html_text import extract_html_text
from track_insight.content.pdf_text import extract_pdf_text
from track_insight.content.quality import assess_text_quality
from track_insight.core.enums import SearchDecision, TaskStatus, UrlType
from track_insight.core.errors import ModelCallError, PageFetchError
from track_insight.core.fingerprints import fingerprint
from track_insight.events.service import enqueue_page_analysis
from track_insight.infrastructure.bailian.client import BailianClient
from track_insight.infrastructure.database import session_scope
from track_insight.infrastructure.events import record_event
from track_insight.infrastructure.jobs import claim_job, complete_job, enqueue_job, fail_job
from track_insight.infrastructure.models import (
    Job,
    ModelRun,
    PageCapture,
    PipelineRun,
    ResearchWorkPackage,
    UrlCandidate,
    UrlDiscovery,
)
from track_insight.settings import PocConfig, get_settings

CAPTURE_PROCESSOR_VERSION = "page-capture-v2"


class PageService:
    def __init__(self, *, config: PocConfig, client: BailianClient | None = None) -> None:
        self.config = config
        self.client = client

    def enqueue(
        self,
        session: Session,
        *,
        plan_id: uuid.UUID,
        decisions: list[str] | None = None,
        limit: int = 30,
    ) -> dict[str, int]:
        eligible_decisions = decisions or [SearchDecision.KEEP, SearchDecision.MAYBE]
        eligible = (
            select(UrlCandidate)
            .join(UrlDiscovery, UrlDiscovery.url_candidate_id == UrlCandidate.id)
            .join(
                ResearchWorkPackage,
                ResearchWorkPackage.id == UrlDiscovery.research_work_package_id,
            )
            .where(
                ResearchWorkPackage.research_plan_id == plan_id,
                UrlCandidate.url_type == UrlType.CONTENT_PAGE,
                UrlCandidate.decision.in_(eligible_decisions),
            )
            .distinct()
        )
        candidate_count = int(
            session.scalar(select(func.count()).select_from(eligible.subquery())) or 0
        )
        eligible_rows = eligible.subquery()
        existing_count = int(
            session.scalar(
                select(func.count()).select_from(
                    eligible_rows.join(
                        PageCapture,
                        PageCapture.url_candidate_id == eligible_rows.c.id,
                    )
                )
            )
            or 0
        )
        candidates = list(
            session.scalars(
                eligible.outerjoin(
                    PageCapture,
                    PageCapture.url_candidate_id == UrlCandidate.id,
                )
                .where(
                    (PageCapture.id.is_(None))
                    | (PageCapture.status == TaskStatus.FAILED_TERMINAL)
                )
                .order_by(UrlCandidate.created_at, UrlCandidate.id)
                .limit(limit)
            )
        )
        enqueued = 0
        recovered = 0
        for candidate in candidates:
            capture = session.scalar(
                select(PageCapture).where(PageCapture.url_candidate_id == candidate.id)
            )
            if capture is None:
                capture = PageCapture(
                    url_candidate_id=candidate.id,
                    requested_url=candidate.canonical_url,
                    status=TaskStatus.PENDING,
                )
                session.add(capture)
                session.flush()
            job = enqueue_page_capture(session, capture, self.config)
            if job.status in {TaskStatus.PENDING, TaskStatus.FAILED_RETRYABLE}:
                if capture.status == TaskStatus.FAILED_TERMINAL:
                    capture.status = TaskStatus.PENDING
                    capture.error_code = None
                    capture.error_message = None
                    recovered += 1
                enqueued += 1
        session.commit()
        return {
            "candidate_count": candidate_count,
            "enqueued_count": enqueued,
            "existing_count": existing_count,
            "selected_count": len(candidates),
            "recovered_count": recovered,
        }

    def fetch_work(self, *, limit: int = 20) -> dict[str, int | str]:
        if limit < 1:
            raise ValueError("limit must be positive")
        pipeline_run_id = uuid.uuid4()
        counts = {"completed": 0, "failed": 0, "skipped": 0}
        with session_scope() as session:
            run = PipelineRun(
                id=pipeline_run_id,
                run_type="page_fetch",
                status=TaskStatus.RUNNING,
                input_payload={"limit": limit},
                started_at=datetime.now(UTC),
            )
            session.add(run)
            session.commit()
            record_event(
                session,
                event_type="page_fetch.run.started",
                message="Page acquisition run started",
                pipeline_run_id=run.id,
                details={"limit": limit},
            )
            session.commit()

        fetcher = PageFetcher(
            timeout_seconds=self.config.stage2.http_timeout_seconds,
            max_retries=self.config.stage2.http_max_retries,
            max_response_bytes=self.config.stage2.max_response_bytes,
            min_interval_per_host_seconds=(self.config.stage2.http_min_interval_per_host_seconds),
        )
        try:
            for _ in range(limit):
                with session_scope() as session:
                    job = claim_job(
                        session,
                        worker_id=f"page-fetch-{pipeline_run_id}",
                        lease_seconds=get_settings().job_lease_seconds,
                        job_type="page_acquire",
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
                    capture.status = TaskStatus.RUNNING
                    capture.error_code = None
                    capture.error_message = None
                    started = time.monotonic()
                    record_event(
                        session,
                        event_type="page_fetch.started",
                        message="Page acquisition started",
                        pipeline_run_id=pipeline_run_id,
                        job_id=job.id,
                        entity_type="page_capture",
                        entity_id=capture.id,
                        details={"url": capture.requested_url},
                    )
                    session.commit()
                    try:
                        result = self._acquire(session, fetcher, capture, job, pipeline_run_id)
                        if result.quality_status != QualityStatus.USABLE:
                            self._read_remote_if_needed(
                                session, capture, job, pipeline_run_id, result
                            )
                        if capture.quality_status != QualityStatus.USABLE:
                            raise PageFetchError(
                                "page content did not pass quality checks",
                                code="unusable_page_content",
                            )
                        capture.status = TaskStatus.COMPLETED
                        capture.error_code = None
                        capture.error_message = None
                        capture.fetched_at = datetime.now(UTC)
                        enqueue_page_analysis(session, capture, self.config)
                        complete_job(
                            session,
                            job,
                            {
                                "quality_status": capture.quality_status,
                                "acquisition_method": capture.acquisition_method,
                                "content_chars": capture.content_chars,
                            },
                        )
                        counts["completed"] += 1
                        record_event(
                            session,
                            event_type="page_fetch.completed",
                            message="Page acquisition completed",
                            pipeline_run_id=pipeline_run_id,
                            job_id=job.id,
                            entity_type="page_capture",
                            entity_id=capture.id,
                            duration_ms=int((time.monotonic() - started) * 1000),
                            details={
                                "url": capture.requested_url,
                                "method": capture.acquisition_method,
                                "content_chars": capture.content_chars,
                                "quality_reasons": capture.quality_reasons,
                            },
                        )
                    except (PageFetchError, ModelCallError) as error:
                        retryable = getattr(error, "retryable", False)
                        capture.status = (
                            TaskStatus.FAILED_RETRYABLE
                            if retryable and job.attempts < job.max_attempts
                            else TaskStatus.FAILED_TERMINAL
                        )
                        capture.error_code = getattr(error, "code", "page_acquisition_error")
                        capture.error_message = str(error)
                        capture.fetched_at = datetime.now(UTC)
                        fail_job(
                            session,
                            job,
                            error_code=capture.error_code,
                            error_message=str(error),
                            retryable=retryable,
                        )
                        counts["failed"] += 1
                        record_event(
                            session,
                            event_type="page_fetch.failed",
                            message="Page acquisition failed",
                            level="ERROR",
                            pipeline_run_id=pipeline_run_id,
                            job_id=job.id,
                            entity_type="page_capture",
                            entity_id=capture.id,
                            duration_ms=int((time.monotonic() - started) * 1000),
                            details={
                                "url": capture.requested_url,
                                "error_code": capture.error_code,
                                "retryable": retryable,
                                "error_message": str(error),
                            },
                        )
                    session.commit()
        finally:
            fetcher.close()

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
                        "page_fetch.run.completed"
                        if counts["failed"] == 0
                        else "page_fetch.run.failed"
                    ),
                    message="Page acquisition run finished",
                    pipeline_run_id=run.id,
                    details=counts.copy(),
                )
        return {"pipeline_run_id": str(pipeline_run_id), **counts}

    def _acquire(
        self,
        session: Session,
        fetcher: PageFetcher,
        capture: PageCapture,
        job: Job,
        pipeline_run_id: uuid.UUID,
    ) -> PageContent:
        try:
            fetched = fetcher.fetch(capture.requested_url)
        except PageFetchError as error:
            if error.code != "http_forbidden":
                raise
            candidate = session.get(UrlCandidate, capture.url_candidate_id)
            fallback = PageContent(
                url_candidate_id=str(capture.url_candidate_id),
                requested_url=capture.requested_url,
                title=candidate.title if candidate else None,
                content_text="",
                quality_status=QualityStatus.REMOTE_READER_REQUIRED,
                quality_reasons=["http_forbidden"],
                raw_metadata={"http_error": str(error)},
            )
            capture.quality_status = fallback.quality_status
            capture.quality_reasons = fallback.quality_reasons
            capture.raw_metadata = fallback.raw_metadata
            session.commit()
            return fallback
        parsed = self._parse_fetch_result(fetched)
        candidate = session.get(UrlCandidate, capture.url_candidate_id)
        title = parsed.get("title") or (candidate.title if candidate else None)
        published_at = parsed.get("published_at")
        if published_at is None and candidate is not None:
            published_at = _parse_candidate_date(candidate.possible_published_at)
        text = parsed.get("content_text", "")
        is_pdf = fetched.content_type == "application/pdf"
        quality_status, reasons = assess_text_quality(
            text,
            min_chars=self.config.stage2.min_usable_text_chars,
            pdf=is_pdf,
        )
        method = AcquisitionMethod.HTTP_PDF if is_pdf else AcquisitionMethod.HTTP_HTML
        page_content = PageContent(
            url_candidate_id=str(capture.url_candidate_id),
            requested_url=fetched.requested_url,
            final_url=fetched.final_url,
            acquisition_method=method,
            mime_type=fetched.content_type,
            http_status=fetched.status_code,
            title=title,
            published_at=published_at,
            content_text=text,
            content_chars=len(text),
            quality_status=quality_status,
            quality_reasons=reasons,
            raw_metadata={"headers": fetched.headers, **parsed.get("metadata", {})},
        )
        _save_page_content(capture, page_content)
        session.commit()
        return page_content

    def _parse_fetch_result(self, fetched: HttpFetchResult) -> dict[str, Any]:
        try:
            if fetched.content_type == "application/pdf":
                parsed = extract_pdf_text(fetched.content)
                return {
                    "title": None,
                    "published_at": None,
                    "content_text": parsed["content_text"],
                    "metadata": {
                        "pdf_page_count": parsed["page_count"],
                        "pdf_encrypted": parsed["is_encrypted"],
                    },
                }
            parsed = extract_html_text(
                fetched.content,
                fetched.headers.get("content-type") or fetched.content_type,
            )
            return {**parsed, "metadata": {}}
        except Exception as error:
            raise PageFetchError(
                f"content parser failed: {error}", code="content_parse_error"
            ) from error

    def _read_remote_if_needed(
        self,
        session: Session,
        capture: PageCapture,
        job: Job,
        pipeline_run_id: uuid.UUID,
        page_content: PageContent,
    ) -> None:
        if self.client is None:
            raise PageFetchError(
                f"page requires {page_content.quality_status} but no Bailian client is configured",
                code="remote_reader_required",
                retryable=True,
            )
        is_pdf = page_content.quality_status == QualityStatus.OCR_REQUIRED
        model = self.config.stage2.ocr_model if is_pdf else self.config.stage2.web_extractor_model
        operation_type = "page_pdf_ocr" if is_pdf else "page_web_extractor"
        model_run = ModelRun(
            pipeline_run_id=pipeline_run_id,
            task_type=operation_type,
            status=TaskStatus.RUNNING,
            provider="bailian",
            model=model,
            prompt_version="page-content-v1",
            input_fingerprint=fingerprint(capture.requested_url, operation_type, model),
            tool_config={"type": "ocr" if is_pdf else "web_extractor"},
            started_at=datetime.now(UTC),
        )
        session.add(model_run)
        session.flush()
        record_event(
            session,
            event_type=f"{operation_type}.started",
            message="Remote page content extraction started",
            pipeline_run_id=pipeline_run_id,
            job_id=job.id,
            model_run_id=model_run.id,
            entity_type="page_capture",
            entity_id=capture.id,
            details={"url": capture.requested_url, "model": model},
        )
        session.commit()
        started = time.monotonic()
        try:
            if is_pdf:
                result = self.client.extract_pdf_ocr(
                    url=capture.requested_url,
                    model=model,
                    max_output_tokens=self.config.stage2.analysis_max_output_tokens,
                )
                evidence_type = AcquisitionMethod.BAILIAN_OCR
            else:
                result = self.client.extract_web_page(
                    url=capture.requested_url,
                    goal=(
                        "提取此页面的完整正文，保留标题、发布日期、政策措施、企业和项目事实、"
                        "地区、产业名称及关键数值。"
                    ),
                    model=model,
                    max_output_tokens=self.config.stage2.web_extractor_max_output_tokens,
                    reasoning_effort=self.config.stage2.web_extractor_reasoning_effort,
                )
                evidence_type = AcquisitionMethod.BAILIAN_WEB_EXTRACTOR
        except ModelCallError as error:
            model_run.status = (
                TaskStatus.FAILED_RETRYABLE if error.retryable else TaskStatus.FAILED_TERMINAL
            )
            model_run.error_code = error.code
            model_run.error_message = str(error)
            request_id, usage, raw = error.response_metadata()
            model_run.request_id = request_id
            model_run.usage = usage
            model_run.raw_response = raw
            model_run.finished_at = datetime.now(UTC)
            record_event(
                session,
                event_type=f"{operation_type}.failed",
                message="Remote page content extraction failed",
                level="ERROR",
                pipeline_run_id=pipeline_run_id,
                job_id=job.id,
                model_run_id=model_run.id,
                entity_type="page_capture",
                entity_id=capture.id,
                duration_ms=int((time.monotonic() - started) * 1000),
                details={"error_code": error.code, "error_message": str(error)},
            )
            session.commit()
            raise
        model_run.status = TaskStatus.COMPLETED
        model_run.request_id = result.request_id
        model_run.usage = result.usage
        model_run.raw_response = result.raw_response
        model_run.finished_at = datetime.now(UTC)
        remote_status, remote_reasons = assess_text_quality(
            result.extracted_text,
            min_chars=self.config.stage2.min_usable_text_chars,
            pdf=is_pdf,
        )
        fallback_content = PageContent(
            url_candidate_id=str(capture.url_candidate_id),
            requested_url=capture.requested_url,
            final_url=capture.final_url or capture.requested_url,
            acquisition_method=evidence_type,
            mime_type=capture.mime_type,
            http_status=capture.http_status,
            title=capture.title,
            published_at=capture.published_at,
            content_text=result.extracted_text,
            content_chars=len(result.extracted_text),
            quality_status=remote_status,
            quality_reasons=remote_reasons,
            raw_metadata={
                **(capture.raw_metadata or {}),
                "bailian_request_id": result.request_id,
            },
        )
        _save_page_content(capture, fallback_content)
        session.commit()
        if remote_status != QualityStatus.USABLE:
            raise PageFetchError(
                "remote extraction did not produce usable page text",
                code="unusable_remote_content",
            )


def enqueue_page_capture(session: Session, capture: PageCapture, config: PocConfig) -> Job:
    return enqueue_job_for_capture(session, capture, config)


def enqueue_job_for_capture(session: Session, capture: PageCapture, config: PocConfig) -> Job:
    return enqueue_job(
        session,
        job_type="page_acquire",
        object_type="page_capture",
        object_id=str(capture.id),
        input_fingerprint=fingerprint(capture.requested_url),
        processor_version=CAPTURE_PROCESSOR_VERSION,
        max_attempts=config.stage2.http_max_retries + 1,
    )


def _save_page_content(capture: PageCapture, content: PageContent) -> None:
    capture.acquisition_method = content.acquisition_method
    capture.requested_url = content.requested_url
    capture.final_url = content.final_url
    capture.http_status = content.http_status
    capture.mime_type = content.mime_type
    capture.title = content.title
    capture.published_at = content.published_at
    capture.content_text = content.content_text
    capture.content_chars = content.content_chars
    capture.content_hash = hashlib.sha256(content.content_text.encode("utf-8")).hexdigest()
    capture.quality_status = content.quality_status
    capture.quality_reasons = content.quality_reasons
    capture.raw_metadata = content.raw_metadata


def _parse_candidate_date(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
