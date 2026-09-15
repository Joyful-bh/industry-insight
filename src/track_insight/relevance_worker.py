import hashlib
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import exists, select

from track_insight.config import Settings, get_settings
from track_insight.database import session_scope
from track_insight.enums import ParseStatus
from track_insight.jobs import claim_job, complete_job, enqueue_job, fail_job
from track_insight.llm_client import PROMPT_VERSION, LlmClassificationError, LlmClient
from track_insight.models import Document, DocumentRelevanceAssessment, DocumentVersion, Source
from track_insight.relevance import classify_relevance, load_relevance_rules


@dataclass(frozen=True, slots=True)
class RelevanceWorkResult:
    processed: int
    succeeded: int
    failed: int


def enqueue_relevance_jobs(
    rules_path: Path = Path("config/relevance_rules.yaml"),
    *,
    limit: int = 10000,
    settings: Settings | None = None,
) -> int:
    rules = load_relevance_rules(rules_path)
    active_settings = settings or get_settings()
    classifier_version = _classifier_version(rules.fingerprint, active_settings)
    with session_scope() as session:
        versions = session.scalars(
            select(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Source, Source.id == Document.source_id)
            .where(
                DocumentVersion.parse_status == ParseStatus.SUCCEEDED,
                Source.enabled.is_(True),
                ~exists().where(
                    DocumentRelevanceAssessment.document_version_id == DocumentVersion.id,
                    DocumentRelevanceAssessment.classifier_version == classifier_version,
                ),
            )
            .order_by(DocumentVersion.discovered_at)
            .limit(limit)
        ).all()
        for version in versions:
            enqueue_job(
                session,
                job_type="document_relevance",
                object_type="document_version",
                object_id=str(version.id),
                input_fingerprint=version.body_sha256 or version.content_sha256,
                processor_version=classifier_version,
            )
        return len(versions)


def run_relevance_jobs(
    *,
    limit: int = 500,
    lease_seconds: int = 300,
    rules_path: Path = Path("config/relevance_rules.yaml"),
    worker_id: str | None = None,
    settings: Settings | None = None,
) -> RelevanceWorkResult:
    rules = load_relevance_rules(rules_path)
    active_settings = settings or get_settings()
    classifier_version = _classifier_version(rules.fingerprint, active_settings)
    llm = _build_llm_client(active_settings)
    identity = worker_id or f"{socket.gethostname()}-relevance"
    processed = succeeded = failed = 0
    for _ in range(limit):
        with session_scope() as session:
            job = claim_job(
                session,
                identity,
                lease_seconds=lease_seconds,
                job_type="document_relevance",
                processor_version=classifier_version,
            )
            if job is None:
                break
            processed += 1
            try:
                version = session.get(DocumentVersion, uuid.UUID(job.object_id))
                if version is None or not version.normalized_text:
                    raise ValueError("document has no normalized text")
                result = classify_relevance(version.title, version.normalized_text, rules)
                classifier_type = "rules"
                if result.label.value == "possibly_relevant" and llm is not None:
                    result = llm.classify(
                        title=version.title,
                        text=version.normalized_text,
                        source_name=version.document.source.name,
                        source_type=version.document.source.source_type,
                        rule_result=result,
                    )
                    classifier_type = "rules+llm"
                assessment = DocumentRelevanceAssessment(
                    document_version_id=version.id,
                    label=result.label,
                    score=result.score,
                    signal_types=result.signal_types,
                    industries=result.industries,
                    regions=version.region_codes,
                    matched_positive_rules=result.matched_positive_rules,
                    matched_negative_rules=result.matched_negative_rules,
                    reason=result.reason,
                    evidence=result.evidence,
                    classifier_type=classifier_type,
                    classifier_version=classifier_version,
                )
                session.add(assessment)
                session.flush()
            except LlmClassificationError as error:
                failed += 1
                fail_job(
                    session,
                    job,
                    error_code="llm_classification_error",
                    error_message=str(error),
                    retryable=error.retryable,
                )
            except (ValueError, TypeError) as error:
                failed += 1
                fail_job(
                    session,
                    job,
                    error_code="relevance_error",
                    error_message=str(error),
                    retryable=False,
                )
            else:
                succeeded += 1
                complete_job(
                    session,
                    job,
                    {"label": result.label.value, "score": result.score},
                )
    return RelevanceWorkResult(processed, succeeded, failed)


def _classifier_version(rules_fingerprint: str, settings: Settings) -> str:
    if not settings.llm_enabled():
        return rules_fingerprint
    identity = "\x1f".join(
        (
            rules_fingerprint,
            PROMPT_VERSION,
            "enabled-sources-only-v1",
            settings.llm_base_url or "",
            settings.llm_model or "",
        )
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _build_llm_client(settings: Settings) -> LlmClient | None:
    if not settings.llm_enabled():
        return None
    assert settings.llm_base_url and settings.llm_api_key and settings.llm_model
    return LlmClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_input_chars=settings.llm_max_input_chars,
    )
