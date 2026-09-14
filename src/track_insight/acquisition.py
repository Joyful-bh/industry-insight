import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import Message
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

import truststore
from bs4 import BeautifulSoup
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from track_insight.blobstore import LocalBlobStore
from track_insight.enums import ParseStatus, RunStatus, SourceObservationStatus
from track_insight.jobs import enqueue_job
from track_insight.models import (
    CrawlRun,
    Document,
    DocumentVersion,
    RawObject,
    Source,
    SourceObservation,
)

DEFAULT_USER_AGENT = "TrackInsightPOC/0.1 (+controlled research collector)"
SUPPORTED_MIME_TYPES = {"text/html": "html", "application/pdf": "pdf"}


class CollectionError(RuntimeError):
    pass


class AccessBlockedError(CollectionError):
    pass


def _is_tls_protocol_error(error: Exception) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in ("bad ecpoint", "handshake failure", "unexpected_eof_while_reading")
    )


@dataclass(frozen=True, slots=True)
class FetchResponse:
    url: str
    status_code: int
    headers: dict[str, str]
    content: bytes

    @property
    def mime_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()


class Transport(Protocol):
    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        user_agent: str,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse: ...

    def post_form(
        self,
        url: str,
        fields: list[tuple[str, str]],
        *,
        timeout_seconds: float,
        user_agent: str,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse: ...


class UrllibTransport:
    def __init__(self) -> None:
        self.ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        user_agent: str,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        request_headers = {"User-Agent": user_agent, "Accept": "text/html,application/pdf"}
        request_headers.update(headers or {})
        request = urllib.request.Request(url, headers=request_headers)
        try:
            return self._open(request, url, timeout_seconds)
        except CollectionError as error:
            if not _is_tls_protocol_error(error) or not shutil.which("curl.exe"):
                raise
            return self._curl_get(url, timeout_seconds, request_headers)

    def post_form(
        self,
        url: str,
        fields: list[tuple[str, str]],
        *,
        timeout_seconds: float,
        user_agent: str,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        request_headers = {
            "User-Agent": user_agent,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        request_headers.update(headers or {})
        request = urllib.request.Request(
            url, data=urlencode(fields).encode(), headers=request_headers
        )
        return self._open(request, url, timeout_seconds)

    def _open(
        self, request: urllib.request.Request, url: str, timeout_seconds: float
    ) -> FetchResponse:
        try:
            with urllib.request.urlopen(
                request, timeout=timeout_seconds, context=self.ssl_context
            ) as response:
                headers = {key.lower(): value for key, value in response.headers.items()}
                return FetchResponse(
                    url=response.geturl(),
                    status_code=response.status,
                    headers=headers,
                    content=response.read(),
                )
        except urllib.error.HTTPError as error:
            if error.code in {403, 429}:
                raise AccessBlockedError(f"access blocked with HTTP {error.code}: {url}") from error
            raise CollectionError(f"HTTP {error.code}: {url}") from error
        except urllib.error.URLError as error:
            raise CollectionError(f"request failed for {url}: {error.reason}") from error

    @staticmethod
    def _curl_get(
        url: str, timeout_seconds: float, headers: dict[str, str]
    ) -> FetchResponse:
        marker = b"\nTRACK_INSIGHT_CURL_META:"
        command = [
            "curl.exe",
            "--location",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            str(max(1, int(timeout_seconds))),
            "--proto",
            "=http,https",
        ]
        for key, value in headers.items():
            command.extend(["--header", f"{key}: {value}"])
        command.extend(
            [
                "--write-out",
                "\nTRACK_INSIGHT_CURL_META:%{http_code}|%{content_type}|%{url_effective}",
                url,
            ]
        )
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode != 0 or marker not in result.stdout:
            message = result.stderr.decode("utf-8", "replace").strip()
            raise CollectionError(f"curl request failed for {url}: {message}")
        content, metadata = result.stdout.rsplit(marker, 1)
        status, content_type, effective_url = metadata.decode("utf-8", "replace").split(
            "|", 2
        )
        return FetchResponse(
            url=effective_url,
            status_code=int(status),
            headers={"content-type": content_type},
            content=content,
        )


class HttpFetcher:
    def __init__(
        self,
        source: Source,
        *,
        transport: Transport | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 20,
        max_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        self.transport = transport or UrllibTransport()
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        requests_per_second = float(source.rate_limit.get("requests_per_second", 0.5))
        self.minimum_interval = 1 / requests_per_second if requests_per_second > 0 else 0
        self.max_attempts = int(source.retry_policy.get("max_attempts", 2))
        self.backoff_seconds = float(source.retry_policy.get("backoff_seconds", 2))
        self._last_request_at: float | None = None

    def fetch(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResponse:
        return self._request(url, headers=headers)

    def post_form(
        self,
        url: str,
        fields: list[tuple[str, str]],
        *,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        return self._request(url, fields=fields, headers=headers)

    def _request(
        self,
        url: str,
        *,
        fields: list[tuple[str, str]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        _validate_http_url(url)
        last_error: CollectionError | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._wait_for_rate_limit()
            try:
                if fields is None:
                    response = self.transport.get(
                        url,
                        timeout_seconds=self.timeout_seconds,
                        user_agent=self.user_agent,
                        headers=headers,
                    )
                else:
                    response = self.transport.post_form(
                        url,
                        fields,
                        timeout_seconds=self.timeout_seconds,
                        user_agent=self.user_agent,
                        headers=headers,
                    )
                self._last_request_at = time.monotonic()
                if not 200 <= response.status_code < 300:
                    raise CollectionError(f"HTTP {response.status_code}: {url}")
                if len(response.content) > self.max_bytes:
                    raise CollectionError(f"response exceeds {self.max_bytes} bytes: {url}")
                return response
            except AccessBlockedError:
                raise
            except CollectionError as error:
                self._last_request_at = time.monotonic()
                last_error = error
                if attempt < self.max_attempts:
                    time.sleep(self.backoff_seconds * attempt)
        assert last_error is not None
        raise last_error

    def _wait_for_rate_limit(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.minimum_interval - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)


@dataclass(frozen=True, slots=True)
class DiscoveredItem:
    url: str
    title: str | None = None
    published_at: datetime | None = None
    external_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    inline_content: bytes | None = None
    inline_mime_type: str | None = None


@dataclass(frozen=True, slots=True)
class HtmlLink:
    href: str
    title: str
    context: str


@dataclass(frozen=True, slots=True)
class DiscoveryBatch:
    items: list[DiscoveredItem]
    next_cursor: dict[str, Any] | None = None
    page_reports: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)


class SourceAdapter(Protocol):
    def discover(
        self,
        source: Source,
        fetcher: HttpFetcher,
        *,
        cutoff: datetime | None = None,
        cursor: dict[str, Any] | None = None,
        known_urls: set[str] | None = None,
        stop_after_known: int = 0,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> DiscoveryBatch: ...


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        self._href = dict(attrs).get("href")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._text).split())))
        if tag.lower() == "a":
            self._href = None
            self._text = []


class HtmlLinkAdapter:
    """从配置入口页发现符合 URL 正式的文档链接。"""

    def __init__(self, url_pattern: str, *, max_items: int = 1) -> None:
        self.pattern = re.compile(url_pattern)
        self.max_items = max_items

    def discover(
        self,
        source: Source,
        fetcher: HttpFetcher,
        *,
        cutoff: datetime | None = None,
        cursor: dict[str, Any] | None = None,
        known_urls: set[str] | None = None,
        stop_after_known: int = 0,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> DiscoveryBatch:
        items: list[DiscoveredItem] = []
        seen: set[str] = set()
        for entry_url in source.entry_urls:
            response = fetcher.fetch(entry_url)
            if response.mime_type != "text/html":
                raise CollectionError(f"list page is not HTML: {entry_url}")
            for href, title in extract_html_links(response):
                try:
                    candidate = canonicalize_url(urljoin(response.url, href))
                except CollectionError:
                    continue
                if candidate in seen or not self.pattern.search(candidate):
                    continue
                seen.add(candidate)
                items.append(
                    DiscoveredItem(
                        url=candidate,
                        title=title or None,
                        published_at=published_at_from_url(candidate),
                    )
                )
                if len(items) >= self.max_items:
                    return DiscoveryBatch(items)
        return DiscoveryBatch(items)


@dataclass(frozen=True, slots=True)
class CollectionResult:
    crawl_run_id: str
    discovered: int
    fetched: int
    new_versions: int
    unchanged: int
    failed: int
    cursor: dict[str, Any] | None


class Collector:
    def __init__(
        self, blob_store: LocalBlobStore, *, parser_version: str = "document-parser-v4"
    ) -> None:
        self.blob_store = blob_store
        self.parser_version = parser_version

    def run(
        self,
        session: Session,
        source: Source,
        adapter: SourceAdapter,
        *,
        mode: str = "incremental",
        fetcher: HttpFetcher | None = None,
        cutoff: datetime | None = None,
        max_attachments_per_document: int = 5,
        cursor: dict[str, Any] | None = None,
        stop_after_known: int = 0,
        progress: Callable[[dict[str, Any]], None] | None = None,
        pipeline_run_id: uuid.UUID | None = None,
    ) -> CollectionResult:
        started_at = datetime.now(UTC)
        crawl = CrawlRun(
            source_id=source.id,
            pipeline_run_id=pipeline_run_id,
            mode=mode,
            status=RunStatus.RUNNING,
            started_at=started_at,
            scheduled_at=started_at,
        )
        session.add(crawl)
        session.flush()
        active_fetcher = fetcher or HttpFetcher(source)
        new_versions = unchanged = failed = fetched = 0
        errors: list[dict[str, str]] = []

        try:
            known_urls = set(
                session.scalars(
                    select(Document.canonical_url).where(Document.source_id == source.id)
                ).all()
            )
            crawl.cursor_before = cursor
            batch = adapter.discover(
                source,
                active_fetcher,
                cutoff=cutoff,
                cursor=cursor,
                known_urls=known_urls,
                stop_after_known=stop_after_known,
                progress=progress,
            )
            crawl.discovered_count = len(batch.items)
            crawl.cursor_after = batch.next_cursor
            errors.extend(batch.failures)
            failed += len(batch.failures)
            source_blocked = False
            for item in batch.items:
                try:
                    response = (
                        FetchResponse(
                            url=item.url,
                            status_code=200,
                            headers={"content-type": item.inline_mime_type or "text/html"},
                            content=item.inline_content,
                        )
                        if item.inline_content is not None
                        else active_fetcher.fetch(item.url)
                    )
                    required_selector = item.metadata.get("required_selector")
                    if required_selector and response.mime_type == "text/html":
                        soup = BeautifulSoup(response.content, "html.parser")
                        selected = soup.select_one(required_selector)
                        if selected is None or not selected.get_text(" ", strip=True):
                            raise CollectionError(
                                "detail page is not materialized yet; missing "
                                f"{required_selector}: "
                                f"{item.url}"
                            )
                    fetched += 1
                    created = self._store_document(session, source, item, response)
                    new_versions += int(created)
                    unchanged += int(not created)
                    if progress:
                        progress(
                            {
                                "kind": "document",
                                "url": item.url,
                                "status": "new" if created else "unchanged",
                                "fetched": fetched,
                                "list_page": item.metadata.get("list_page"),
                                "source_section": item.metadata.get("source_section"),
                            }
                        )
                    if response.mime_type == "text/html":
                        attachments = _extract_pdf_attachments(
                            response,
                            parent_url=item.url,
                            published_at=item.published_at,
                            limit=max_attachments_per_document,
                        )
                        crawl.discovered_count += len(attachments)
                        for attachment in attachments:
                            try:
                                attachment_response = active_fetcher.fetch(attachment.url)
                                fetched += 1
                                attachment_created = self._store_document(
                                    session, source, attachment, attachment_response
                                )
                                new_versions += int(attachment_created)
                                unchanged += int(not attachment_created)
                            except AccessBlockedError as error:
                                failed += 1
                                errors.append({"url": attachment.url, "error": str(error)})
                                source_blocked = True
                                break
                            except CollectionError as error:
                                failed += 1
                                errors.append({"url": attachment.url, "error": str(error)})
                except AccessBlockedError as error:
                    failed += 1
                    errors.append({"url": item.url, "error": str(error)})
                    break
                except CollectionError as error:
                    failed += 1
                    errors.append({"url": item.url, "error": str(error)})
                    if progress:
                        progress({"kind": "document", "url": item.url, "status": "failed"})
                if source_blocked:
                    break
        except CollectionError as error:
            failed += 1
            errors.append({"url": source.entry_urls[0], "error": str(error)})

        finished_at = datetime.now(UTC)
        if finished_at <= started_at:
            finished_at = started_at + timedelta(microseconds=1)
        crawl.fetched_count = fetched
        crawl.failed_count = failed
        crawl.finished_at = finished_at
        crawl.status = (
            RunStatus.FAILED
            if fetched == 0 and failed
            else (RunStatus.PARTIAL if failed else RunStatus.SUCCEEDED)
        )
        crawl.error_message = errors[0]["error"] if errors else None
        observation_status = (
            SourceObservationStatus.COLLECTION_FAILED
            if crawl.status == RunStatus.FAILED
            else SourceObservationStatus.HEALTHY_WITH_NEW
            if new_versions
            else SourceObservationStatus.HEALTHY_NO_NEW
        )
        session.add(
            SourceObservation(
                source_id=source.id,
                crawl_run_id=crawl.id,
                period_start=started_at,
                period_end=finished_at,
                status=observation_status,
                details={
                    "new_versions": new_versions,
                    "unchanged": unchanged,
                    "errors": errors,
                    "page_reports": batch.page_reports if "batch" in locals() else [],
                    "cursor": crawl.cursor_after,
                },
            )
        )
        session.flush()
        return CollectionResult(
            crawl_run_id=str(crawl.id),
            discovered=crawl.discovered_count,
            fetched=fetched,
            new_versions=new_versions,
            unchanged=unchanged,
            failed=failed,
            cursor=crawl.cursor_after,
        )

    def _store_document(
        self,
        session: Session,
        source: Source,
        item: DiscoveredItem,
        response: FetchResponse,
    ) -> bool:
        mime_type = response.mime_type
        if mime_type not in SUPPORTED_MIME_TYPES and response.content.startswith(b"%PDF-"):
            mime_type = "application/pdf"
        content_kind = SUPPORTED_MIME_TYPES.get(mime_type)
        if content_kind is None:
            raise CollectionError(f"unsupported content type {response.mime_type!r}: {item.url}")
        stored = self.blob_store.put_bytes(response.content)
        raw_object = session.scalar(select(RawObject).where(RawObject.sha256 == stored.sha256))
        now = datetime.now(UTC)
        if raw_object is None:
            try:
                with session.begin_nested():
                    raw_object = RawObject(
                        sha256=stored.sha256,
                        storage_key=stored.storage_key,
                        mime_type=mime_type,
                        size_bytes=stored.size_bytes,
                        source_url=response.url,
                        http_status=response.status_code,
                        response_headers=response.headers,
                        fetched_at=now,
                    )
                    session.add(raw_object)
                    session.flush()
            except IntegrityError:
                raw_object = session.scalar(
                    select(RawObject).where(RawObject.sha256 == stored.sha256)
                )
                if raw_object is None:
                    raise

        canonical_url = canonicalize_url(response.url)
        document = session.scalar(
            select(Document).where(
                Document.source_id == source.id,
                Document.canonical_url == canonical_url,
            )
        )
        if document is None:
            document = Document(
                source_id=source.id,
                canonical_url=canonical_url,
                external_id=item.external_id,
                title=item.title,
            )
            session.add(document)
            session.flush()
        elif item.title and not document.title:
            document.title = item.title

        existing = session.scalar(
            select(DocumentVersion.id).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.content_sha256 == stored.sha256,
            )
        )
        if existing is not None:
            return False
        latest_version = session.scalar(
            select(func.max(DocumentVersion.version_no)).where(
                DocumentVersion.document_id == document.id
            )
        )
        version = DocumentVersion(
            document_id=document.id,
            version_no=(latest_version or 0) + 1,
            raw_object_id=raw_object.id,
            title=item.title,
            publisher=source.publisher,
            published_at=item.published_at,
            discovered_at=now,
            fetched_at=now,
            region_codes=source.regions,
            content_type=content_kind,
            content_sha256=stored.sha256,
            parse_status=ParseStatus.PENDING,
            metadata_json=item.metadata,
        )
        session.add(version)
        session.flush()
        enqueue_job(
            session,
            job_type="document_parse",
            object_type="document_version",
            object_id=str(version.id),
            input_fingerprint=stored.sha256,
            processor_version=self.parser_version,
        )
        return True


def canonicalize_url(url: str) -> str:
    _validate_http_url(url)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{host}{port}"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    path = quote(unquote(parts.path or "/"), safe="/%:@!$&'()*+,;=-._~")
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def _validate_http_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise CollectionError(f"only absolute HTTP(S) URLs are allowed: {url}")


def _decode_html(content: bytes, headers: dict[str, str]) -> str:
    message = Message()
    message["content-type"] = headers.get("content-type", "text/html")
    charset = message.get_content_charset() or "utf-8"
    return content.decode(charset, errors="replace")


def published_at_from_url(url: str) -> datetime | None:
    patterns = (
        (r"/t(20\d{6})_", "%Y%m%d"),
        (r"/(20\d{6})/", "%Y%m%d"),
        (r"/(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?:/|_|\.)", None),
        (r"/(20\d{2})/(\d{1,2})/(\d{1,2})/", None),
    )
    for pattern, date_format in patterns:
        match = re.search(pattern, url)
        if not match:
            continue
        try:
            if date_format:
                return datetime.strptime(match.group(1), date_format).replace(tzinfo=UTC)
            return datetime(*(int(value) for value in match.groups()), tzinfo=UTC)
        except ValueError:
            continue
    return None


def _extract_pdf_attachments(
    response: FetchResponse,
    *,
    parent_url: str,
    published_at: datetime | None,
    limit: int,
) -> list[DiscoveredItem]:
    attachments: list[DiscoveredItem] = []
    seen: set[str] = set()
    for href, title in extract_html_links(response):
        try:
            url = canonicalize_url(urljoin(response.url, href))
        except CollectionError:
            continue
        if url in seen or not urlsplit(url).path.lower().endswith(".pdf"):
            continue
        seen.add(url)
        attachments.append(
            DiscoveredItem(
                url=url,
                title=title or "PDF附件",
                published_at=published_at,
                metadata={"attachment_of": canonicalize_url(parent_url)},
            )
        )
        if len(attachments) >= limit:
            break
    return attachments


def extract_html_links(response: FetchResponse) -> list[tuple[str, str]]:
    parser = _AnchorParser()
    parser.feed(_decode_html(response.content, response.headers))
    return parser.links


def extract_html_link_records(response: FetchResponse) -> list[HtmlLink]:
    soup = BeautifulSoup(_decode_html(response.content, response.headers), "html.parser")
    records: list[HtmlLink] = []
    for anchor in soup.find_all("a"):
        href = anchor.get("href") or _scripted_anchor_href(anchor)
        if not href:
            continue
        title = " ".join(anchor.get_text(" ", strip=True).split())
        if not title:
            image = anchor.find("img", alt=True)
            title = " ".join(str(image.get("alt", "")).split()) if image else ""
        container = anchor.find_parent(["li", "tr", "dd", "dt"]) or anchor.parent
        context = " ".join(container.get_text(" ", strip=True).split()) if container else title
        records.append(HtmlLink(str(href), title, context[:1000]))
    return records


def _scripted_anchor_href(anchor: Any) -> str | None:
    """Extract navigation URLs used by common government CMS paging controls."""
    tagname = str(anchor.get("tagname", "")).strip()
    if tagname.startswith(("/", "http://", "https://")):
        return tagname

    onclick = str(anchor.get("onclick", ""))
    for candidate in re.findall(r"['\"]([^'\"]+)['\"]", onclick):
        if candidate.startswith(("/", "http://", "https://")):
            return candidate
    return None
