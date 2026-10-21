#!/usr/bin/env python3
"""Build a weighted, explainable retrieval index from classification-ready tracks."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.parquet as pq


TERM_WEIGHTS = {"strong": 6.0, "product": 5.0, "activity": 4.0, "feature": 2.0, "negative": -6.0}
GENERIC_TERMS = {
    "人工智能", "新能源", "智能制造", "芯片", "传感器", "机器人", "电子产品", "技术开发",
    "技术服务", "设备制造", "软件开发", "科技", "信息技术", "生产制造", "研发设计", "批量生产",
    "参与行业展会或技术论坛",
}
CONTEXT_REQUIRED_TERMS = {
    "测试设备", "清洗设备", "智能机器人", "集成电路", "无人机", "临床试验服务",
    "药物临床试验服务", "生物制剂", "减速器", "充电桩",
    "高性能纤维", "半导体材料",
}


def normalize_term(value: Any) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).lower()
    text = re.sub(r"[\s·•・,，。；;:：()（）\[\]【】]", "", text)
    return text.strip("-/、")


def unique_terms(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip() if value is not None else ""
        normalized = normalize_term(text)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(text)
    return result


def load_tracks(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["tracks"] if isinstance(payload, dict) else payload


def build_track(track: dict[str, Any]) -> dict[str, Any]:
    strong = unique_terms([track["name"], *track.get("aliases", []), *track.get("positive_terms", [])])
    product = unique_terms(track.get("core_products_services", []))
    activity = unique_terms(track.get("included_activities", []))
    feature = unique_terms(track.get("observable_company_features", []))
    negative = unique_terms([*track.get("negative_terms", []), *track.get("excluded_activities", [])])
    retrieval_parts = [track["name"], *strong, *product, *activity, *feature, track.get("definition", ""), track.get("enterprise_archetype", "")]
    return {
        "track_id": track["track_id"], "track_name": track["name"],
        "definition": track.get("definition", ""), "strong_terms": strong,
        "product_terms": product, "activity_terms": activity, "feature_terms": feature,
        "negative_terms": negative, "industry_codes": [],
        "retrieval_text": "\n".join(unique_terms(retrieval_parts)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data/processed/tracks_curated/tracks_classification_ready.jsonl"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed/company_track_pipeline/tracks"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    tracks = [build_track(x) for x in load_tracks(args.input)]

    term_tracks: dict[str, set[str]] = {}
    for track in tracks:
        for field in ("strong_terms", "product_terms", "activity_terms", "feature_terms"):
            for term in track[field]:
                term_tracks.setdefault(normalize_term(term), set()).add(track["track_id"])
    n = len(tracks)
    term_rows = []
    for track in tracks:
        for term_type, field in (("strong", "strong_terms"), ("product", "product_terms"), ("activity", "activity_terms"), ("feature", "feature_terms"), ("negative", "negative_terms")):
            for term in track[field]:
                normalized = normalize_term(term)
                df = len(term_tracks.get(normalized, {track["track_id"]}))
                idf = math.log((n + 1) / (df + 1)) + 1
                is_generic = normalized in {normalize_term(x) for x in GENERIC_TERMS}
                requires_context = normalized in {normalize_term(x) for x in CONTEXT_REQUIRED_TERMS}
                effective_weight = TERM_WEIGHTS[term_type] * (0.15 if is_generic and term_type != "negative" else 1.0) * idf
                term_rows.append({
                    "track_id": track["track_id"], "track_name": track["track_name"], "term": term,
                    "term_normalized": normalized, "term_type": term_type, "base_weight": TERM_WEIGHTS[term_type],
                    "track_document_frequency": df, "idf": round(idf, 6), "is_generic": is_generic,
                    "requires_context": requires_context,
                    "effective_weight": round(effective_weight, 6),
                })

    jsonl_path = args.output_dir / "tracks_retrieval_ready.jsonl"
    parquet_path = args.output_dir / "tracks_retrieval_ready.parquet"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for track in tracks:
            handle.write(json.dumps(track, ensure_ascii=False, separators=(",", ":")) + "\n")
    pq.write_table(pa.Table.from_pylist(tracks), parquet_path, compression="zstd")
    term_fields = list(term_rows[0])
    with (args.output_dir / "track_terms.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=term_fields)
        writer.writeheader(); writer.writerows(term_rows)
    ambiguous = [x for x in term_rows if x["track_document_frequency"] > 1 or x["is_generic"]]
    with (args.output_dir / "ambiguous_terms.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=term_fields)
        writer.writeheader(); writer.writerows(ambiguous)
    counts = Counter(x["term_type"] for x in term_rows)
    report = {
        "source_file": str(args.input), "track_count": len(tracks), "term_count": len(term_rows),
        "term_type_counts": dict(counts), "ambiguous_term_row_count": len(ambiguous),
        "generic_term_policy": "effective positive weight multiplied by 0.15",
        "industry_codes_status": "empty_pending_manual_mapping",
        "outputs": {"jsonl": str(jsonl_path), "parquet": str(parquet_path), "terms": str(args.output_dir / "track_terms.csv")},
    }
    (args.output_dir / "retrieval_metadata.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
