import re

from track_insight.content.contracts import QualityStatus

_BLOCK_PAGE_MARKERS = (
    "验证码",
    "访问过于频繁",
    "系统检测到异常访问",
    "请求被拒绝",
    "请登录后查看",
    "javascript is required",
    "enable javascript",
    "access denied",
    "robot check",
)


def assess_text_quality(
    text: str,
    *,
    min_chars: int,
    pdf: bool = False,
) -> tuple[QualityStatus, list[str]]:
    normalized = re.sub(r"\s+", " ", text).strip()
    reasons: list[str] = []
    if not normalized:
        reasons.append("empty_body")
        return (
            QualityStatus.OCR_REQUIRED if pdf else QualityStatus.REMOTE_READER_REQUIRED,
            reasons,
        )
    lowered = normalized.lower()
    if any(marker in lowered for marker in _BLOCK_PAGE_MARKERS):
        reasons.append("access_or_javascript_placeholder")
        return QualityStatus.REMOTE_READER_REQUIRED, reasons
    if len(normalized) < min_chars:
        reasons.append("body_below_minimum_length")
        return (
            QualityStatus.OCR_REQUIRED if pdf else QualityStatus.REMOTE_READER_REQUIRED,
            reasons,
        )
    paragraph_count = sum(1 for part in text.splitlines() if len(part.strip()) >= 30)
    if len(normalized) > 1200 and paragraph_count == 0:
        reasons.append("no_substantive_paragraphs")
        return QualityStatus.REMOTE_READER_REQUIRED, reasons
    return QualityStatus.USABLE, reasons
