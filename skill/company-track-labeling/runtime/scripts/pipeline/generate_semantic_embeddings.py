#!/usr/bin/env python3
"""Generate resumable company and track embeddings with DashScope OpenAI API."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import sqlite3
import struct
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.parquet as pq


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def clean(value: Any) -> str:
    return str(value or "").strip()


def joined(values: Any) -> str:
    return "；".join(clean(x) for x in (values or []) if clean(x))


def company_texts(row: dict[str, Any], max_chars: int) -> tuple[str, str]:
    profile = clean(row.get("company_profile_for_classification"))
    core_parts = [
        f"企业名称：{clean(row.get('company_name'))}",
        f"主行业：{clean(row.get('primary_industry'))}",
        f"国标行业：{clean(row.get('national_industry_name'))}",
    ]
    if profile:
        core_parts.append(f"企业简介：{profile}")
    core = "\n".join(x for x in core_parts if not x.endswith("："))[:max_chars]
    scope = (
        f"企业名称：{clean(row.get('company_name'))}\n"
        f"经营范围：{clean(row.get('business_scope'))}"
    )[:max_chars]
    return core, scope


def track_texts(row: dict[str, Any], max_chars: int) -> tuple[str, str]:
    core = (
        f"制造业赛道：{clean(row.get('track_name'))}\n"
        f"定义：{clean(row.get('definition'))}\n"
        f"核心产品：{joined(row.get('product_terms'))}\n"
        f"强特征术语：{joined(row.get('strong_terms'))}"
    )[:max_chars]
    activity = (
        f"制造业赛道：{clean(row.get('track_name'))}\n"
        f"企业实际活动：{joined(row.get('activity_terms'))}\n"
        f"可观察企业特征：{joined(row.get('feature_terms'))}"
    )[:max_chars]
    return core, activity


class Cache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS embeddings (cache_key TEXT PRIMARY KEY, vector BLOB NOT NULL, dimension INTEGER NOT NULL)"
        )

    def get(self, key: str, dimension: int) -> list[float] | None:
        row = self.db.execute("SELECT vector, dimension FROM embeddings WHERE cache_key=?", (key,)).fetchone()
        if not row or row[1] != dimension:
            return None
        return list(struct.unpack(f"<{dimension}f", row[0]))

    def put_many(self, rows: list[tuple[str, list[float]]], dimension: int) -> None:
        payload = [(key, struct.pack(f"<{dimension}f", *vector), dimension) for key, vector in rows]
        self.db.executemany("INSERT OR REPLACE INTO embeddings VALUES (?,?,?)", payload)
        self.db.commit()

    def close(self) -> None:
        self.db.close()


def cache_key(model: str, dimension: int, kind: str, text: str) -> str:
    material = json.dumps([model, dimension, kind, text], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def embed_batch(
    client: httpx.AsyncClient, semaphore: asyncio.Semaphore, url: str, model: str,
    dimension: int, texts: list[str], retries: int,
) -> tuple[list[list[float]], dict[str, int]]:
    for attempt in range(retries + 1):
        try:
            async with semaphore:
                response = await client.post(url, json={"model": model, "input": texts, "dimensions": dimension, "encoding_format": "float"})
            if response.status_code >= 400:
                if response.status_code == 429 and "insufficient_quota" in response.text:
                    raise SystemExit("DashScope embedding quota is exhausted; cached progress is preserved. Top up quota and rerun the same command.")
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:1000]}")
            data = response.json()
            ordered = sorted(data.get("data", []), key=lambda x: x["index"])
            vectors = [x["embedding"] for x in ordered]
            if len(vectors) != len(texts) or any(len(x) != dimension for x in vectors):
                raise RuntimeError("API returned unexpected embedding count or dimension")
            usage = data.get("usage") or {}
            return vectors, {k: int(v) for k, v in usage.items() if isinstance(v, (int, float))}
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            if attempt >= retries:
                raise RuntimeError(f"Embedding request failed after {retries + 1} attempts: {exc}") from exc
            await asyncio.sleep(min(12, (2 ** attempt) + random.random()))
    raise AssertionError("unreachable")


async def fill_embeddings(
    items: list[dict[str, str]], cache: Cache, client: httpx.AsyncClient, semaphore: asyncio.Semaphore,
    url: str, model: str, dimension: int, batch_size: int, retries: int,
) -> tuple[dict[str, list[float]], dict[str, int], int]:
    result: dict[str, list[float]] = {}
    missing: list[dict[str, str]] = []
    for item in items:
        key = cache_key(model, dimension, item["kind"], item["text"])
        item["cache_key"] = key
        vector = cache.get(key, dimension)
        if vector is None:
            missing.append(item)
        else:
            result[item["item_key"]] = vector
    batches = [missing[i:i + batch_size] for i in range(0, len(missing), batch_size)]
    usage: dict[str, int] = {}

    async def run(batch: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[list[float]], dict[str, int]]:
        vectors, batch_usage = await embed_batch(client, semaphore, url, model, dimension, [x["text"] for x in batch], retries)
        return batch, vectors, batch_usage

    completed = 0
    tasks = [asyncio.create_task(run(batch)) for batch in batches]
    try:
        for future in asyncio.as_completed(tasks):
            batch, vectors, batch_usage = await future
            cache.put_many([(item["cache_key"], vector) for item, vector in zip(batch, vectors)], dimension)
            for item, vector in zip(batch, vectors):
                result[item["item_key"]] = vector
            for key, value in batch_usage.items():
                usage[key] = usage.get(key, 0) + value
            completed += len(batch)
            if completed % 200 == 0 or completed == len(missing):
                print(f"embedded={completed:,}/{len(missing):,} cached={len(items)-len(missing):,}", flush=True)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return result, usage, len(items) - len(missing)


async def async_main(args: argparse.Namespace) -> None:
    load_env(args.env)
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        raise SystemExit("DASHSCOPE_API_KEY is missing")
    base_url = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    model = args.model or os.environ.get("DASHSCOPE_EMBEDDING_MODEL", "qwen3.7-text-embedding-flash")
    companies = [x for x in pq.read_table(args.companies).to_pylist() if x["classification_eligible"]]
    tracks = pq.read_table(args.tracks).to_pylist()
    if args.limit_companies:
        companies = companies[:args.limit_companies]
    items: list[dict[str, str]] = []
    for row in companies:
        core, scope = company_texts(row, args.max_chars)
        items.extend([
            {"item_key": f"company:{row['company_id']}:core", "kind": "company_core", "text": core},
            {"item_key": f"company:{row['company_id']}:scope", "kind": "company_scope", "text": scope},
        ])
    for row in tracks:
        core, activity = track_texts(row, args.max_chars)
        items.extend([
            {"item_key": f"track:{row['track_id']}:core", "kind": "track_core", "text": core},
            {"item_key": f"track:{row['track_id']}:activity", "kind": "track_activity", "text": activity},
        ])
    cache = Cache(args.cache)
    try:
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {api_key}"}, timeout=args.timeout) as client:
            vectors, usage, cached = await fill_embeddings(
                items, cache, client, asyncio.Semaphore(args.concurrency), f"{base_url}/embeddings",
                model, args.dimension, args.batch_size, args.retries,
            )
    finally:
        cache.close()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    company_rows = [{
        "company_id": row["company_id"], "company_name": row["company_name"],
        "core_embedding": vectors[f"company:{row['company_id']}:core"],
        "scope_embedding": vectors[f"company:{row['company_id']}:scope"],
    } for row in companies]
    track_rows = [{
        "track_id": row["track_id"], "track_name": row["track_name"],
        "core_embedding": vectors[f"track:{row['track_id']}:core"],
        "activity_embedding": vectors[f"track:{row['track_id']}:activity"],
    } for row in tracks]
    pq.write_table(pa.Table.from_pylist(company_rows), args.output_dir / "company_embeddings.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(track_rows), args.output_dir / "track_embeddings.parquet", compression="zstd")
    manifest = {
        "model": model, "dimension": args.dimension, "company_count": len(company_rows), "track_count": len(track_rows),
        "vector_count": len(items), "cache_hits": cached, "api_usage": usage,
        "design": {"company_vectors": ["core", "scope"], "track_vectors": ["core", "activity"]},
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--companies", type=Path, default=Path("data/processed/company_track_pipeline/companies/companies_classification_ready.parquet"))
    parser.add_argument("--tracks", type=Path, default=Path("data/processed/company_track_pipeline/tracks/tracks_retrieval_ready.parquet"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed/company_track_pipeline/embeddings"))
    parser.add_argument("--cache", type=Path, default=Path("data/processed/company_track_pipeline/cache/embedding_cache.sqlite"))
    parser.add_argument("--model", default="")
    parser.add_argument("--dimension", type=int, default=768)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--max-chars", type=int, default=12000)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--limit-companies", type=int, default=0)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 20:
        parser.error("--batch-size must be between 1 and 20 for qwen3.7 embedding models")
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
