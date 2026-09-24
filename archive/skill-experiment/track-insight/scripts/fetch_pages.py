#!/usr/bin/env python3
"""Batch-acquire and extract retained sources in a Track Insight workspace."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx

from extract_content import extract_bytes


BLOCK_MARKERS = (
    "验证码",
    "访问过于频繁",
    "安全验证",
    "请输入密码",
    "登录后查看",
    "captcha",
    "access denied",
)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    result = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path}:{line_no}: invalid JSON: {error}") from error
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_no}: expected a JSON object")
        result.append(value)
    return result


def write_jsonl(path: Path, rows: list[dict]) -> None:
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    temporary.replace(path)


def now_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only HTTP(S) URLs with a hostname are allowed")
    hostname = parsed.hostname.strip("[]").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("localhost destinations are not allowed")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return
    else:
        if not ip.is_global:
            raise ValueError(f"non-public literal IP rejected: {ip}")


def fetch_url(
    client: httpx.Client,
    url: str,
    max_bytes: int,
    retries: int,
) -> tuple[str, int, str, bytes]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        current = url
        try:
            for _ in range(6):
                validate_public_url(current)
                with client.stream("GET", current, follow_redirects=False) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("redirect response has no location")
                        current = urljoin(current, location)
                        continue
                    if response.status_code >= 400:
                        raise ValueError(f"HTTP {response.status_code}")
                    chunks = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError(f"response exceeds {max_bytes} bytes")
                        chunks.append(chunk)
                    return current, response.status_code, response.headers.get("content-type", ""), b"".join(chunks)
            raise ValueError("too many redirects")
        except (httpx.HTTPError, ValueError) as error:
            last_error = error
            if attempt < retries:
                time.sleep(min(2**attempt, 4))
    assert last_error is not None
    raise last_error


def quality(text: str, min_chars: int) -> tuple[str, list[str]]:
    reasons = []
    lowered = text.lower()
    if len(text) < min_chars:
        reasons.append("text_too_short")
    if any(marker in lowered for marker in BLOCK_MARKERS):
        reasons.append("access_or_login_marker")
    return ("usable", []) if not reasons else ("insufficient", reasons)


def page_record(source: dict, extracted: dict, *, final_url: str | None, status: int | None, method: str, content_path: str | None, content_hash: str | None, quality_status: str, reasons: list[str], error: dict | None = None) -> dict:
    source_id = str(source["source_id"])
    page_seed = f"{source_id}|{content_hash or 'unavailable'}"
    return {
        "page_id": f"page_{hashlib.sha256(page_seed.encode()).hexdigest()[:16]}",
        "source_id": source_id,
        "requested_url": source.get("url"),
        "final_url": final_url,
        "content_type": extracted.get("content_type", "other"),
        "acquisition_method": method,
        "http_status": status,
        "title": extracted.get("title") or source.get("title"),
        "published_at": extracted.get("published_at"),
        "content_path": content_path,
        "content_chars": len(str(extracted.get("text") or "")),
        "content_hash": content_hash,
        "quality_status": quality_status,
        "quality_reasons": reasons,
        "relevance": None,
        "document_type": None,
        "review_reason": None,
        "review_regions": [],
        "review_industries": [],
        "content_sufficient": None,
        "fetched_at": now_iso(),
        "error": error,
    }


def acquire(source: dict, workspace: Path, client: httpx.Client, args: argparse.Namespace) -> dict:
    local_path = source.get("local_path")
    if local_path:
        source_path = Path(local_path).expanduser().resolve()
        raw = source_path.read_bytes()
        extracted = extract_bytes(raw, filename=source_path.name)
        final_url = None
        http_status = None
        method = "local_file"
    else:
        url = source.get("url") or source.get("canonical_url")
        if not url:
            raise ValueError("source has neither URL nor local_path")
        final_url, http_status, content_type, raw = fetch_url(
            client,
            str(url),
            args.max_bytes,
            args.retries,
        )
        extracted = extract_bytes(raw, filename=final_url, content_type=content_type)
        method = "http_pdf" if extracted["content_type"] == "pdf" else "http_html"

    text = str(extracted.get("text") or "")
    quality_status, reasons = quality(text, args.min_chars)
    raw_content_path = None
    if extracted.get("content_type") == "pdf":
        raw_relative = Path("pages") / "attachments" / f"{source['source_id']}.pdf"
        (workspace / raw_relative).write_bytes(raw)
        raw_content_path = raw_relative.as_posix()
        if quality_status != "usable":
            quality_status = "ocr_required"
            reasons = list(dict.fromkeys([*reasons, "pdf_text_extraction_insufficient"]))
    elif quality_status != "usable" and not local_path:
        quality_status = "reader_required"
        reasons = list(dict.fromkeys([*reasons, "static_text_extraction_insufficient"]))
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
    content_path = None
    if text:
        relative = Path("pages") / f"{source['source_id']}.md"
        (workspace / relative).write_text(text + "\n", encoding="utf-8", newline="\n")
        content_path = relative.as_posix()
    record = page_record(
        source,
        extracted,
        final_url=final_url,
        status=http_status,
        method=method,
        content_path=content_path,
        content_hash=content_hash,
        quality_status=quality_status,
        reasons=reasons,
    )
    record["raw_content_path"] = raw_content_path
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-bytes", type=int, default=15_728_640)
    parser.add_argument("--min-chars", type=int, default=500)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    try:
        sources = read_jsonl(workspace / "sources.jsonl")
        pages = read_jsonl(workspace / "pages" / "index.jsonl")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    completed = {
        row.get("source_id")
        for row in pages
        if row.get("quality_status") == "usable" and row.get("content_path")
    }
    candidates = [
        source
        for source in sources
        if source.get("source_id") not in completed
        and source.get("decision") in {"keep", "maybe"}
        and (source.get("url_type") == "content_page" or source.get("local_path"))
    ]
    if args.limit is not None:
        candidates = candidates[: max(args.limit, 0)]

    counters = {
        "attempted": 0,
        "usable": 0,
        "insufficient": 0,
        "reader_required": 0,
        "ocr_required": 0,
        "failed": 0,
    }
    by_source = {str(source.get("source_id")): source for source in sources}
    with httpx.Client(
        timeout=args.timeout,
        headers={"User-Agent": "TrackInsightSkill/1.0 (+public research)"},
    ) as client:
        for source in candidates:
            source_id = str(source.get("source_id") or "")
            counters["attempted"] += 1
            try:
                record = acquire(source, workspace, client, args)
                counters[record["quality_status"]] += 1
                source["acquisition_status"] = record["quality_status"]
                source["page_path"] = record["content_path"]
                source["content_hash"] = record["content_hash"]
                source["error"] = None
            except Exception as error:  # route URL failures to one-page browser reading
                fallback_status = "inaccessible" if source.get("local_path") else "reader_required"
                if fallback_status == "inaccessible":
                    counters["failed"] += 1
                else:
                    counters["reader_required"] += 1
                record = page_record(
                    source,
                    {"content_type": "other", "text": ""},
                    final_url=None,
                    status=None,
                    method="local_file" if source.get("local_path") else "http_html",
                    content_path=None,
                    content_hash=None,
                    quality_status=fallback_status,
                    reasons=["local_file_failed" if source.get("local_path") else "static_acquisition_failed"],
                    error={"type": type(error).__name__, "message": str(error)[:1000]},
                )
                source["acquisition_status"] = fallback_status
                source["page_path"] = None
                source["content_hash"] = None
                source["error"] = record["error"]
            pages = [row for row in pages if row.get("source_id") != source_id]
            pages.append(record)
            by_source[source_id] = source
            write_jsonl(workspace / "pages" / "index.jsonl", pages)
            write_jsonl(workspace / "sources.jsonl", list(by_source.values()))

    print(json.dumps(counters, ensure_ascii=False))
    return 0 if counters["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
