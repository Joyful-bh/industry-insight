#!/usr/bin/env python3
"""Retrieve production candidates with the validated BM25 + E0_WEIGHTED formula."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

CHANNEL_WEIGHTS = np.asarray((0.40, 0.30, 0.15, 0.15), dtype=np.float32)


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def char_ngrams(text: str) -> Counter[str]:
    normalized_text = normalize_text(text)
    grams: Counter[str] = Counter()
    for size in (2, 3):
        grams.update(
            normalized_text[index:index + size]
            for index in range(max(0, len(normalized_text) - size + 1))
        )
    return grams


def build_bm25(tracks: list[dict[str, Any]]):
    docs = [char_ngrams(row["retrieval_text"]) for row in tracks]
    lengths = [sum(doc.values()) for doc in docs]
    avg_len = sum(lengths) / len(lengths)
    document_frequency = Counter(gram for doc in docs for gram in doc)
    document_count = len(docs)
    idf = {
        gram: math.log(1 + (document_count - frequency + 0.5) / (frequency + 0.5))
        for gram, frequency in document_frequency.items()
    }
    doc_idf_mass = [sum(idf[gram] for gram in doc) for doc in docs]
    return docs, lengths, avg_len, idf, doc_idf_mass


def bm25_scores(query_text: str, docs, lengths, avg_len, idf, doc_idf_mass):
    query = char_ngrams(query_text)
    k1, b = 1.5, 0.75
    results = []
    for index, doc in enumerate(docs):
        score = 0.0
        matched_mass = 0.0
        matched_rare = 0
        for gram in query.keys() & doc.keys():
            gram_idf = idf[gram]
            frequency = doc[gram]
            score += gram_idf * (frequency * (k1 + 1)) / (
                frequency + k1 * (1 - b + b * lengths[index] / avg_len)
            )
            matched_mass += gram_idf
            if gram_idf >= 1.5:
                matched_rare += 1
        coverage = matched_mass / doc_idf_mass[index] if doc_idf_mass[index] else 0.0
        results.append((score, coverage, matched_rare))
    return results


def normalized(rows: list[dict[str, Any]], field: str) -> np.ndarray:
    values = np.asarray([row[field] for row in rows], dtype=np.float32)
    return values / np.maximum(np.linalg.norm(values, axis=1, keepdims=True), 1e-12)


def robust_sigmoid(values: np.ndarray) -> np.ndarray:
    flat = values.ravel().astype(np.float64)
    median = float(np.median(flat))
    q25, q75 = np.quantile(flat, (0.25, 0.75))
    robust_sigma = max(float(q75 - q25) / 1.349, 1e-8)
    z = np.clip((values.astype(np.float64) - median) / robust_sigma, -12.0, 12.0)
    return (1.0 / (1.0 + np.exp(-z))).astype(np.float32)


def e0_weighted(
    company_vectors: list[dict[str, Any]], track_vectors: list[dict[str, Any]]
) -> np.ndarray:
    company_core = normalized(company_vectors, "core_embedding")
    company_scope = normalized(company_vectors, "scope_embedding")
    track_core = normalized(track_vectors, "core_embedding")
    track_activity = normalized(track_vectors, "activity_embedding")
    channels = np.stack(
        (
            company_core @ track_core.T,
            company_core @ track_activity.T,
            company_scope @ track_core.T,
            company_scope @ track_activity.T,
        ),
        axis=2,
    )
    return np.tensordot(channels, CHANNEL_WEIGHTS, axes=([2], [0])).astype(np.float32)


def bm25_matrices(
    companies: list[dict[str, Any]], tracks: list[dict[str, Any]]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    docs, lengths, avg_len, idf, doc_idf_mass = build_bm25(tracks)
    raw = np.zeros((len(companies), len(tracks)), dtype=np.float32)
    coverage = np.zeros_like(raw)
    rare = np.zeros_like(raw, dtype=np.int16)
    for index, company in enumerate(companies):
        scores = bm25_scores(
            company["classification_text"], docs, lengths, avg_len, idf, doc_idf_mass
        )
        raw[index] = [row[0] for row in scores]
        coverage[index] = [row[1] for row in scores]
        rare[index] = [row[2] for row in scores]
        if (index + 1) % 1000 == 0:
            print(f"bm25_scored={index + 1:,}/{len(companies):,}", flush=True)
    composite = coverage * 100.0 + np.log1p(raw) * 3.0
    return raw, coverage, rare, composite.astype(np.float32)


def top_keys(scores: np.ndarray, budget: int) -> set[int]:
    return set(np.argsort(-scores.ravel(), kind="stable")[:budget].tolist())


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--companies", type=Path, required=True)
    parser.add_argument("--tracks", type=Path, required=True)
    parser.add_argument("--embedding-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--candidate-budget", type=int, default=2000)
    parser.add_argument("--core-budget", type=int, default=1600)
    parser.add_argument("--embedding-weight", type=float, default=0.75)
    args = parser.parse_args()
    if args.candidate_budget < 1:
        parser.error("--candidate-budget must be positive")
    if not 0 <= args.embedding_weight <= 1:
        parser.error("--embedding-weight must be between 0 and 1")

    all_companies = pq.read_table(args.companies).to_pylist()
    company_data = {str(row["company_id"]): row for row in all_companies}
    track_data = {
        str(row["track_id"]): row for row in pq.read_table(args.tracks).to_pylist()
    }
    company_vectors = pq.read_table(
        args.embedding_dir / "company_embeddings.parquet"
    ).to_pylist()
    track_vectors = pq.read_table(
        args.embedding_dir / "track_embeddings.parquet"
    ).to_pylist()
    companies = [company_data[str(row["company_id"])] for row in company_vectors]
    tracks = [track_data[str(row["track_id"])] for row in track_vectors]

    embedding = e0_weighted(company_vectors, track_vectors)
    bm25_raw, bm25_coverage, bm25_rare, bm25_composite = bm25_matrices(companies, tracks)
    embedding_scaled = robust_sigmoid(embedding)
    bm25_scaled = robust_sigmoid(bm25_composite)
    hybrid = (
        args.embedding_weight * embedding_scaled
        + (1.0 - args.embedding_weight) * bm25_scaled
    )

    pair_count = hybrid.size
    budget = min(args.candidate_budget, pair_count)
    core_budget = min(args.core_budget, budget)
    selected = np.argsort(-hybrid.ravel(), kind="stable")[:budget]
    bm25_top = top_keys(bm25_scaled, budget)
    embedding_top = top_keys(embedding_scaled, budget)
    rows: list[dict[str, Any]] = []
    for rank, flat_index in enumerate(selected, start=1):
        i, j = divmod(int(flat_index), len(tracks))
        sources = []
        if int(flat_index) in bm25_top:
            sources.append("BM25")
        if int(flat_index) in embedding_top:
            sources.append("E0_WEIGHTED")
        company = companies[i]
        track = tracks[j]
        rows.append({
            "candidate_id": f"{company['company_id']}::{track['track_id']}",
            "company_id": company["company_id"],
            "company_name": company["company_name"],
            "track_id": track["track_id"],
            "track_name": track["track_name"],
            "retrieval_method": "BM25_E0_HYBRID_FIXED",
            "retrieval_score": round(float(hybrid[i, j]), 7),
            "global_rank": rank,
            "candidate_tier": "CORE_RETRIEVAL" if rank <= core_budget else "BROAD_RETRIEVAL",
            "embedding_score": round(float(embedding[i, j]), 7),
            "bm25_composite": round(float(bm25_composite[i, j]), 7),
            "bm25_raw": round(float(bm25_raw[i, j]), 7),
            "bm25_track_coverage": round(float(bm25_coverage[i, j]), 7),
            "bm25_rare_gram_count": int(bm25_rare[i, j]),
            "embedding_scaled": round(float(embedding_scaled[i, j]), 7),
            "bm25_scaled": round(float(bm25_scaled[i, j]), 7),
            "decision_status": "RETRIEVED_PENDING_LOCAL_VERIFICATION",
            "agent_enrichment_status": "NOT_REQUESTED",
            "in_bm25_top_budget": int(flat_index) in bm25_top,
            "in_embedding_top_budget": int(flat_index) in embedding_top,
            "retrieval_sources": ";".join(sources) or "FUSION_ONLY",
        })

    args.output_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(rows),
        args.output_dir / "candidates_hybrid.parquet",
        compression="zstd",
    )
    write_csv(rows, args.output_dir / "candidates_hybrid.csv")
    report = {
        "schema_version": "operational-hybrid-retrieval-1.0",
        "company_count": len(companies),
        "track_count": len(tracks),
        "pair_count": pair_count,
        "candidate_count": len(rows),
        "candidate_company_count": len({row["company_id"] for row in rows}),
        "candidate_track_count": len({row["track_id"] for row in rows}),
        "candidate_budget": budget,
        "core_budget": core_budget,
        "scaling": "ROBUST_SIGMOID",
        "embedding_method": "E0_WEIGHTED",
        "embedding_channel_weights": CHANNEL_WEIGHTS.tolist(),
        "embedding_weight": args.embedding_weight,
        "bm25_weight": 1.0 - args.embedding_weight,
        "score_threshold": round(float(hybrid.ravel()[selected[-1]]), 7),
    }
    (args.output_dir / "retrieval_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
