import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO

from bs4 import BeautifulSoup
from pypdf import PdfReader
from sqlalchemy.orm import Session

from track_insight.blobstore import LocalBlobStore
from track_insight.enums import ParseStatus
from track_insight.models import DocumentVersion

PARSER_VERSION = "document-parser-v6"
MIN_USEFUL_TEXT_LENGTH = 80
CONTENT_SELECTORS = (
    "article",
    "main",
    "#UCAP-CONTENT",
    ".TRS_Editor",
    ".txt-content",
    "#mainText",
    ".mainTextBox",
    ".article-content",
    ".article_content",
    "#zoom",
)
BOILERPLATE_SELECTORS = (
    ".breadcrumb",
    ".breadcrumbs",
    "[class*='breadcrumb']",
    "[class*='crumb']",
    "[class*='article-share']",
    "[class*='share-box']",
    "[class*='sharebar']",
    "[class*='toolbar']",
    "[class*='article-tool']",
    "[class*='recommend']",
    "[class*='related-news']",
    "[class*='qrcode']",
    "[class*='qr-code']",
    "[class*='copyright']",
    "[class*='footer']",
    "[id*='footer']",
    "[role='navigation']",
)
UI_LABEL_PATTERN = re.compile(
    r"^(?:首页|返回首页|上一页|下一页|打印|关闭|收藏|分享|返回顶部|"
    r"字体\s*[:：]?\s*(?:大\s*中\s*小)?|字号\s*[:：]?\s*(?:大\s*中\s*小)?)$",
    re.I,
)
TRAILING_BOILERPLATE_MARKERS = (
    "更多热点速报、权威资讯、深度分析",
    "如遇作品内容、版权等问题",
    "扫描二维码",
    "下载手机客户端",
    "扫一扫在手机打开当前页",
    "相关推荐",
    "版权声明",
    "免责声明",
    "版权所有",
)
INLINE_CONTROL_PATTERNS = (
    re.compile(
        r"(?:打印\s*)?【\s*字体\s*[:：]\s*大\s*中\s*小\s*】"
        r"\s*分享\s*[:：]?\s*X?",
        re.I,
    ),
    re.compile(r"分享\s*[:：]\s*X(?:\s*\[\s*关闭本页\s*\])?", re.I),
    re.compile(r"字体\s*[:：]\s*\[\s*放大\s*缩小\s*\]", re.I),
)


class DocumentParseError(RuntimeError):
    def __init__(self, message: str, *, code: str = "parse_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    text: str
    title: str | None = None
    published_at: datetime | None = None
    document_number: str | None = None
    quality: float = 0.0
    warnings: list[str] = field(default_factory=list)
    page_count: int | None = None


class DocumentParser:
    def __init__(self, blob_store: LocalBlobStore, *, parser_version: str = PARSER_VERSION):
        self.blob_store = blob_store
        self.parser_version = parser_version

    def parse_version(self, session: Session, version: DocumentVersion) -> ParsedDocument:
        content = self.blob_store.read_bytes(version.raw_object.storage_key)
        if version.content_type == "html":
            parsed = parse_html(content)
        elif version.content_type == "pdf":
            parsed = parse_pdf(content)
        else:
            version.parse_status = ParseStatus.UNSUPPORTED
            version.parser_version = self.parser_version
            raise DocumentParseError(
                f"unsupported document type: {version.content_type}", code="unsupported_type"
            )

        if len(parsed.text) < MIN_USEFUL_TEXT_LENGTH:
            version.parse_status = ParseStatus.PARTIAL
            version.parser_version = self.parser_version
            version.parse_quality = parsed.quality
            version.metadata_json = _merge_parse_metadata(version.metadata_json, parsed)
            raise DocumentParseError(
                "document contains too little extractable text; "
                "OCR or manual review may be required",
                code="insufficient_text",
            )

        version.normalized_text = parsed.text
        version.body_sha256 = hashlib.sha256(parsed.text.encode("utf-8")).hexdigest()
        version.simhash = _simhash(parsed.text)
        version.parse_quality = parsed.quality
        version.parser_version = self.parser_version
        version.parse_status = ParseStatus.SUCCEEDED
        version.metadata_json = _merge_parse_metadata(version.metadata_json, parsed)
        if parsed.title and not version.title:
            version.title = parsed.title
        if parsed.title and not version.document.title:
            version.document.title = parsed.title
        if parsed.published_at and not version.published_at:
            version.published_at = parsed.published_at
        if parsed.document_number and not version.document_number:
            version.document_number = parsed.document_number
        session.flush()
        return parsed

    def parse_version_id(self, session: Session, object_id: str) -> ParsedDocument:
        try:
            version_id = uuid.UUID(object_id)
        except ValueError as error:
            raise DocumentParseError(
                "invalid document version id", code="invalid_object_id"
            ) from error
        version = session.get(DocumentVersion, version_id)
        if version is None:
            raise DocumentParseError("document version does not exist", code="object_not_found")
        return self.parse_version(session, version)


def parse_html(content: bytes) -> ParsedDocument:
    soup = BeautifulSoup(content, "html.parser")
    for node in soup.select("script, style, nav, footer, header, aside, form, iframe, noscript"):
        node.decompose()
    _remove_boilerplate_nodes(soup)
    candidates = [node for selector in CONTENT_SELECTORS for node in soup.select(selector)]
    root = max(candidates, key=lambda node: len(node.get_text(" ", strip=True)), default=soup.body)
    if root is None:
        raise DocumentParseError("HTML has no body", code="invalid_html")
    text = normalize_inline(root.get_text(" ", strip=True))
    title_node = soup.find("h1") or soup.find("title")
    title = normalize_inline(title_node.get_text(" ", strip=True)) if title_node else None
    text = _remove_leading_navigation(text, title)
    text = _remove_inline_controls(text)
    text = _trim_trailing_boilerplate(text)
    full_text = normalize_text(soup.get_text("\n", strip=True))
    published_at = _extract_published_at(full_text)
    document_number = _extract_document_number(full_text)
    quality = _quality_score(text, has_title=bool(title))
    return ParsedDocument(
        text=text,
        title=title,
        published_at=published_at,
        document_number=document_number,
        quality=quality,
    )


def parse_pdf(content: bytes) -> ParsedDocument:
    try:
        reader = PdfReader(BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as error:
        raise DocumentParseError(f"cannot read PDF: {error}", code="invalid_pdf") from error
    text = normalize_text("\n\n".join(pages))
    warnings = [] if text else ["no_extractable_text"]
    return ParsedDocument(
        text=text,
        published_at=_extract_published_at(text),
        document_number=_extract_document_number(text),
        quality=_quality_score(text, has_title=False),
        warnings=warnings,
        page_count=len(reader.pages),
    )


def normalize_text(value: str) -> str:
    value = _remove_unsupported_control_characters(value)
    lines = [normalize_inline(line) for line in value.replace("\xa0", " ").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def normalize_inline(value: str) -> str:
    value = _remove_unsupported_control_characters(value)
    return re.sub(r"\s+", " ", value).strip()


def _remove_boilerplate_nodes(soup: BeautifulSoup) -> None:
    for node in soup.select(",".join(BOILERPLATE_SELECTORS)):
        node.decompose()
    for node in soup.find_all(["a", "button"]):
        if UI_LABEL_PATTERN.fullmatch(normalize_inline(node.get_text(" ", strip=True))):
            node.decompose()


def _remove_leading_navigation(text: str, title: str | None) -> str:
    if not title:
        return text
    position = text.find(title)
    if 0 < position < 500:
        return text[position:]
    return text


def _trim_trailing_boilerplate(text: str) -> str:
    cutoff = len(text)
    searchable_start = max(20, int(len(text) * 0.6))
    for marker in TRAILING_BOILERPLATE_MARKERS:
        position = text.find(marker, searchable_start)
        if position >= 0:
            cutoff = min(cutoff, position)
    return text[:cutoff].strip()


def _remove_inline_controls(text: str) -> str:
    for pattern in INLINE_CONTROL_PATTERNS:
        text = pattern.sub(" ", text)
    return normalize_inline(text)


def _remove_unsupported_control_characters(value: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)


def _extract_published_at(text: str) -> datetime | None:
    match = re.search(
        r"(?:发布时间|发布日期|成文日期|印发日期)?\s*[:：]?\s*"
        r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?",
        text[:5000],
    )
    if not match:
        return None
    try:
        return datetime(*(int(value) for value in match.groups()), tzinfo=UTC)
    except ValueError:
        return None


def _extract_document_number(text: str) -> str | None:
    match = re.search(r"([\u4e00-\u9fff]{1,12}[〔\[]20\d{2}[〕\]]\d{1,5}号)", text[:5000])
    return normalize_inline(match.group(1)) if match else None


def _quality_score(text: str, *, has_title: bool) -> float:
    length_score = min(len(text) / 1200, 1.0)
    structure_score = min(text.count("\n") / 12, 1.0)
    score = 0.75 * length_score + 0.15 * structure_score + (0.1 if has_title else 0.0)
    return round(min(score, 1.0), 3)


def _simhash(text: str) -> str:
    tokens = re.findall(r"[\u4e00-\u9fff]{2}|[a-zA-Z0-9]+", text.lower())
    vector = [0] * 64
    for token in tokens:
        value = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest())
        for bit in range(64):
            vector[bit] += 1 if value & (1 << bit) else -1
    fingerprint = sum(1 << bit for bit, weight in enumerate(vector) if weight >= 0)
    return f"{fingerprint:016x}"


def _merge_parse_metadata(existing: dict, parsed: ParsedDocument) -> dict:
    metadata = dict(existing)
    metadata["parse"] = {
        "warnings": parsed.warnings,
        "page_count": parsed.page_count,
        "text_length": len(parsed.text),
    }
    return metadata
