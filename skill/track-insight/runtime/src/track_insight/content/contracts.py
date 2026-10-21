from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class QualityStatus(StrEnum):
    USABLE = "usable"
    REMOTE_READER_REQUIRED = "remote_reader_required"
    OCR_REQUIRED = "ocr_required"
    UNUSABLE = "unusable"


class AcquisitionMethod(StrEnum):
    HTTP_HTML = "http_html"
    HTTP_PDF = "http_pdf"
    BAILIAN_WEB_EXTRACTOR = "bailian_web_extractor"
    BAILIAN_OCR = "bailian_ocr"


class PageContent(BaseModel):
    url_candidate_id: str
    requested_url: str
    final_url: str | None = None
    acquisition_method: AcquisitionMethod | None = None
    mime_type: str | None = None
    http_status: int | None = None
    title: str | None = None
    published_at: datetime | None = None
    content_text: str = ""
    content_chars: int = 0
    quality_status: QualityStatus
    quality_reasons: list[str] = Field(default_factory=list)
    raw_metadata: dict = Field(default_factory=dict)
