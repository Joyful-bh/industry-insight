import json
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from track_insight.acquisition import Collector
from track_insight.adapters import build_adapter, default_cutoff
from track_insight.blobstore import LocalBlobStore
from track_insight.config import get_settings
from track_insight.database import get_engine, session_scope
from track_insight.models import CrawlRun, Document, Source, SourceObservation
from track_insight.scope_config import CollectionScope

BLOCK_MARKERS = (
    "403",
    "429",
    "access blocked",
    "captcha",
    "验证码",
    "certificate",
    "证书",
    "ssl",
    "tls",
)
READINESS_LOCK_ID = 742_019_381


@contextmanager
def readiness_lock() -> Iterator[None]:
    with get_engine().connect() as connection:
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": READINESS_LOCK_ID}
        )
        if not acquired:
            raise RuntimeError("another source readiness audit is already running")
        try:
            yield
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": READINESS_LOCK_ID}
            )


@dataclass(slots=True)
class SourceReadiness:
    code: str
    name: str
    status: str
    pages_checked: int
    distinct_pages: int
    documents_requested: int
    fetched: int
    attachments_fetched: int
    failed: int
    dated_documents: int
    date_status: str
    pagination_status: str
    cursor_recoverable: bool
    errors: list[dict[str, str]]
    crawl_run_id: str


def classify_readiness(
    *, fetched: int, failed: int, dated: int, snapshot: bool, errors: list[dict[str, str]]
) -> str:
    error_text = " ".join(error.get("error", "") for error in errors).lower()
    if fetched == 0 and any(marker in error_text for marker in BLOCK_MARKERS):
        return "blocked"
    if fetched == 0:
        return "invalid"
    if failed or snapshot or dated == 0:
        return "limited"
    return "ready"


def run_readiness_audit(
    scope: CollectionScope,
    *,
    max_pages: int = 3,
    max_documents: int = 3,
    max_attachments: int = 2,
    progress: Callable[[SourceReadiness, int, int], None] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    with session_scope() as session:
        source_codes = list(
            session.scalars(
                select(Source.code).where(Source.enabled.is_(True)).order_by(Source.code)
            )
        )

    results: list[SourceReadiness] = []
    cutoff = default_cutoff(scope)
    for code in source_codes:
        events: list[dict[str, Any]] = []
        with session_scope() as session:
            source = session.scalar(select(Source).where(Source.code == code))
            assert source is not None
            adapter = build_adapter(
                source, scope, max_pages=max_pages, max_items=max_documents
            )
            result = Collector(LocalBlobStore(settings.resolved_blob_root())).run(
                session,
                source,
                adapter,
                mode="readiness",
                cutoff=cutoff,
                max_attachments_per_document=max_attachments,
                progress=events.append,
            )
            crawl = session.get(CrawlRun, result.crawl_run_id)
            assert crawl is not None
            observation = session.scalar(
                select(SourceObservation).where(SourceObservation.crawl_run_id == crawl.id)
            )
            details = observation.details if observation else {}
            errors = details.get("errors", [])
            page_reports = details.get("page_reports", [])
            document_urls = {
                event["url"] for event in events if event.get("kind") == "document"
            }
            documents = (
                list(
                    session.scalars(
                        select(Document).where(
                            Document.source_id == source.id,
                            Document.canonical_url.in_(document_urls),
                        )
                    )
                )
                if document_urls
                else []
            )
            dated = sum(any(version.published_at for version in doc.versions) for doc in documents)
            distinct_pages = len(
                {report.get("url") for report in page_reports if report.get("status") == "ok"}
            )
            pagination_status = (
                "verified"
                if distinct_pages >= 2
                else "single_page"
                if result.cursor and result.cursor.get("complete")
                else "not_verified"
            )
            snapshot = source.adapter_key == "snapshot_page"
            status = classify_readiness(
                fetched=result.fetched,
                failed=result.failed,
                dated=dated,
                snapshot=snapshot,
                errors=errors,
            )
            source_result = SourceReadiness(
                    code=source.code,
                    name=source.name,
                    status=status,
                    pages_checked=len(page_reports),
                    distinct_pages=distinct_pages,
                    documents_requested=len(document_urls),
                    fetched=result.fetched,
                    attachments_fetched=max(0, result.fetched - len(document_urls)),
                    failed=result.failed,
                    dated_documents=dated,
                    date_status="available" if dated else "missing",
                    pagination_status=pagination_status,
                    cursor_recoverable=bool(result.cursor),
                    errors=errors,
                    crawl_run_id=result.crawl_run_id,
            )
            results.append(source_result)
            if progress:
                progress(source_result, len(results), len(source_codes))

    counts = Counter(item.status for item in results)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "limits": {
            "max_pages_per_source": max_pages,
            "max_documents_per_source": max_documents,
            "max_attachments_per_document": max_attachments,
            "same_domain_concurrency": 1,
        },
        "source_count": len(results),
        "summary": {key: counts.get(key, 0) for key in ("ready", "limited", "blocked", "invalid")},
        "sources": [asdict(item) for item in results],
    }


def write_readiness_reports(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "source_readiness.json"
    markdown_path = output_dir / "source_readiness.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = report["summary"]
    lines = [
        "# 来源测试回采与就绪审计",
        "",
        f"生成时间：{report['generated_at']}",
        "",
        (
            f"来源总数：{report['source_count']}；ready {summary['ready']}；"
            f"limited {summary['limited']}；blocked {summary['blocked']}；"
            f"invalid {summary['invalid']}。"
        ),
        "",
        "| 来源 | 状态 | 页面 | 正文/附件 | 日期 | 分页 | 失败 |",
        "|---|---|---:|---:|---|---|---:|",
    ]
    for item in report["sources"]:
        lines.append(
            f"| {item['code']} | {item['status']} | {item['pages_checked']} | "
            f"{item['documents_requested']}/{item['attachments_fetched']} | "
            f"{item['date_status']} | {item['pagination_status']} | {item['failed']} |"
        )
    lines.extend(["", "## 异常详情", ""])
    abnormal = [item for item in report["sources"] if item["status"] != "ready"]
    if not abnormal:
        lines.append("无。")
    for item in abnormal:
        reason = "; ".join(error.get("error", "") for error in item["errors"])
        if not reason:
            reason = "可访问，但发布时间或逐条详情不足。"
        lines.append(f"- `{item['code']}`（{item['status']}）：{reason}")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path
