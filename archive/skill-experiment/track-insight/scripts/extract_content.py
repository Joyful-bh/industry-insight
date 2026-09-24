#!/usr/bin/env python3
"""Extract readable text and basic metadata from a local HTML, PDF, or text file."""

from __future__ import annotations

import argparse
import io
import json
import re
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from pypdf import PdfReader


REMOVE_TAGS = {
    "script",
    "style",
    "noscript",
    "nav",
    "footer",
    "header",
    "form",
    "svg",
    "canvas",
    "iframe",
}
DATE_META_KEYS = {
    "article:published_time",
    "date",
    "datepublished",
    "publishdate",
    "pubdate",
}


def clean_text(value: str) -> str:
    lines = [re.sub(r"[\t\u00a0 ]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def extract_html(raw: bytes) -> dict[str, str | None]:
    soup = BeautifulSoup(raw, "html.parser")
    title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else None
    published_at = None
    for meta in soup.find_all("meta"):
        key = str(meta.get("property") or meta.get("name") or "").lower()
        value = str(meta.get("content") or "").strip()
        if key in DATE_META_KEYS and value:
            published_at = normalize_date(value)
            if published_at:
                break
    for tag in soup.find_all(REMOVE_TAGS):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    text = clean_text(root.get_text("\n", strip=True))
    return {"title": title, "published_at": published_at, "text": text, "content_type": "html"}


def extract_pdf(raw: bytes) -> dict[str, str | None]:
    reader = PdfReader(io.BytesIO(raw))
    pages = [page.extract_text() or "" for page in reader.pages]
    text = clean_text("\n\n".join(pages))
    title = None
    if reader.metadata and reader.metadata.title:
        title = clean_text(str(reader.metadata.title)) or None
    return {"title": title, "published_at": None, "text": text, "content_type": "pdf"}


def extract_bytes(raw: bytes, *, filename: str, content_type: str | None = None) -> dict[str, str | None]:
    lowered = (content_type or "").lower()
    suffix = Path(filename).suffix.lower()
    if "pdf" in lowered or suffix == ".pdf" or raw.startswith(b"%PDF"):
        return extract_pdf(raw)
    if "html" in lowered or suffix in {".html", ".htm"} or b"<html" in raw[:4096].lower():
        return extract_html(raw)
    text = raw.decode("utf-8-sig", errors="replace")
    return {"title": Path(filename).stem, "published_at": None, "text": clean_text(text), "content_type": "other"}


def normalize_date(value: str) -> str | None:
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", value)
    if not match:
        return None
    try:
        return datetime(int(match[1]), int(match[2]), int(match[3])).date().isoformat()
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--content-type")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = extract_bytes(
        args.input.read_bytes(), filename=args.input.name, content_type=args.content_type
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(str(payload["text"]), encoding="utf-8", newline="\n")
        payload = {**payload, "output": str(args.output.resolve())}
        payload.pop("text")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
