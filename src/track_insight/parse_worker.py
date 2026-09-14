import socket
from dataclasses import dataclass

from sqlalchemy import or_, select

from track_insight.blobstore import LocalBlobStore
from track_insight.config import Settings
from track_insight.database import session_scope
from track_insight.document_parser import PARSER_VERSION, DocumentParseError, DocumentParser
from track_insight.enums import ParseStatus
from track_insight.jobs import claim_job, complete_job, enqueue_job, fail_job
from track_insight.models import DocumentVersion


@dataclass(frozen=True, slots=True)
class ParseWorkResult:
    processed: int
    succeeded: int
    failed: int


def enqueue_parse_jobs(limit: int = 1000) -> int:
    with session_scope() as session:
        versions = session.scalars(
            select(DocumentVersion)
            .where(
                or_(
                    DocumentVersion.parse_status == ParseStatus.PENDING,
                    DocumentVersion.parser_version.is_(None),
                    DocumentVersion.parser_version != PARSER_VERSION,
                )
            )
            .order_by(DocumentVersion.discovered_at.asc())
            .limit(limit)
        ).all()
        for version in versions:
            enqueue_job(
                session,
                job_type="document_parse",
                object_type="document_version",
                object_id=str(version.id),
                input_fingerprint=version.content_sha256,
                processor_version=PARSER_VERSION,
            )
        return len(versions)


def run_parse_jobs(
    settings: Settings,
    *,
    limit: int = 20,
    worker_id: str | None = None,
) -> ParseWorkResult:
    parser = DocumentParser(LocalBlobStore(settings.resolved_blob_root()))
    identity = worker_id or f"{socket.gethostname()}-document-parser"
    processed = succeeded = failed = 0

    for _ in range(limit):
        with session_scope() as session:
            job = claim_job(
                session,
                identity,
                lease_seconds=settings.job_lease_seconds,
                job_type="document_parse",
            )
            if job is None:
                break
            processed += 1
            try:
                parsed = parser.parse_version_id(session, job.object_id)
            except DocumentParseError as error:
                failed += 1
                fail_job(
                    session,
                    job,
                    error_code=error.code,
                    error_message=str(error),
                    retryable=error.retryable,
                )
            except (OSError, RuntimeError) as error:
                failed += 1
                fail_job(
                    session,
                    job,
                    error_code="temporary_parse_error",
                    error_message=str(error),
                    retryable=True,
                )
            else:
                succeeded += 1
                complete_job(
                    session,
                    job,
                    {
                        "document_version_id": job.object_id,
                        "text_length": len(parsed.text),
                        "parse_quality": parsed.quality,
                        "parser_version": parser.parser_version,
                    },
                )

    return ParseWorkResult(processed=processed, succeeded=succeeded, failed=failed)
