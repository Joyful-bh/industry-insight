#!/usr/bin/env python3
"""Normalize public HTTP(S) URLs and remove common tracking parameters."""

from __future__ import annotations

import argparse
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "spm",
    "from",
    "source",
    "ref",
    "referrer",
}


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http or https and include a hostname")
    hostname = parsed.hostname.lower().rstrip(".")
    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        hostname = f"{hostname}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_PARAMETERS:
            continue
        query.append((key, value))
    query.sort()
    return urlunsplit((scheme, hostname, path, urlencode(query, doseq=True), ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    results = []
    failed = False
    for original in args.urls:
        try:
            results.append({"url": original, "canonical_url": canonicalize_url(original)})
        except ValueError as error:
            failed = True
            results.append({"url": original, "error": str(error)})
    if args.as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for item in results:
            print(item.get("canonical_url") or f"ERROR: {item['url']}: {item['error']}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

