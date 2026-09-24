#!/usr/bin/env python3
"""Import browser-reader or OCR text into an existing Source/Page record."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("source_id")
    parser.add_argument("input", type=Path, nargs="?")
    parser.add_argument("--method", choices=("web_reader", "ocr"), required=True)
    parser.add_argument("--status", choices=("usable", "insufficient", "inaccessible"), default="usable")
    parser.add_argument("--reason")
    parser.add_argument("--title")
    parser.add_argument("--published-at")
    parser.add_argument("--min-chars", type=int, default=200)
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    if args.status == "usable" and args.input is None:
        raise SystemExit("a UTF-8 text input is required when status=usable")
    text = args.input.read_text(encoding="utf-8").strip() if args.input else ""
    if args.status == "usable" and len(text) < args.min_chars:
        raise SystemExit(f"imported text has {len(text)} characters; minimum is {args.min_chars}")
    sources_path = workspace / "sources.jsonl"
    pages_path = workspace / "pages" / "index.jsonl"
    sources = read_jsonl(sources_path)
    pages = read_jsonl(pages_path)
    source = next((item for item in sources if str(item.get("source_id")) == args.source_id), None)
    if source is None:
        raise SystemExit(f"unknown source_id: {args.source_id}")

    old = next((item for item in pages if str(item.get("source_id")) == args.source_id), {})
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else old.get("content_hash")
    relative = Path("pages") / f"{args.source_id}.md"
    if text:
        (workspace / relative).write_text(text + "\n", encoding="utf-8", newline="\n")
    content_path = relative.as_posix() if text else old.get("content_path")
    page = {
        **old,
        "page_id": old.get("page_id") or f"page_{hashlib.sha256((args.source_id + '|' + (digest or 'unavailable')).encode()).hexdigest()[:16]}",
        "source_id": args.source_id,
        "requested_url": source.get("url"),
        "final_url": old.get("final_url") or source.get("url"),
        "content_type": "ocr_text" if args.method == "ocr" else "reader_text",
        "acquisition_method": args.method,
        "http_status": old.get("http_status"),
        "title": args.title or old.get("title") or source.get("title"),
        "published_at": args.published_at or old.get("published_at") or source.get("possible_publication_date"),
        "content_path": content_path,
        "content_chars": len(text) if text else old.get("content_chars", 0),
        "content_hash": digest,
        "quality_status": args.status,
        "quality_reasons": [] if args.status == "usable" else [args.reason or args.status],
        "relevance": old.get("relevance"),
        "document_type": old.get("document_type"),
        "review_reason": old.get("review_reason"),
        "review_regions": old.get("review_regions") or [],
        "review_industries": old.get("review_industries") or [],
        "content_sufficient": old.get("content_sufficient"),
        "fetched_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "error": None,
    }
    pages = [item for item in pages if str(item.get("source_id")) != args.source_id] + [page]
    source.update({
        "acquisition_status": args.status,
        "page_path": content_path,
        "content_hash": digest,
        "error": None if args.status == "usable" else {"type": args.status, "message": args.reason or args.status},
    })
    write_jsonl(pages_path, pages)
    write_jsonl(sources_path, sources)
    print(json.dumps({"source_id": args.source_id, "page_id": page["page_id"], "status": args.status, "chars": len(text)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
