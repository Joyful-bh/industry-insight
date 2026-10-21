from io import BytesIO

from pypdf import PdfReader


def extract_pdf_text(raw: bytes) -> dict:
    reader = PdfReader(BytesIO(raw), strict=False)
    if reader.is_encrypted:
        return {
            "title": None,
            "published_at": None,
            "content_text": "",
            "page_count": 0,
            "is_encrypted": True,
        }
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text(extraction_mode="layout") or "")
        except TypeError:
            pages.append(page.extract_text() or "")
    text = "\n\n".join(part.strip() for part in pages if part.strip())
    return {
        "title": None,
        "published_at": None,
        "content_text": text,
        "page_count": len(reader.pages),
        "is_encrypted": reader.is_encrypted,
    }
