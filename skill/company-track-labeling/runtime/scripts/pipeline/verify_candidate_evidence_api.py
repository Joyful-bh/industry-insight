#!/usr/bin/env python3
"""Verify retrieved company-track candidates using only local evidence and a chat API."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.parquet as pq


LABELS = {"MATCH", "NOT_MATCH", "EVIDENCE_INSUFFICIENT"}
CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}
REASON_CODES = {
    "CORE_PRODUCT_ACTIVITY",
    "CLEAR_ADJACENT_OR_EXCLUDED",
    "SALES_SERVICE_OR_APPLICATION_ONLY",
    "UNRELATED_BUSINESS",
    "GENERIC_OR_TEMPLATE_TEXT",
    "MISSING_COMPANY_INFORMATION",
    "CONFLICTING_EVIDENCE",
    "OTHER",
}

SYSTEM_PROMPT = """你是企业—制造业赛道关系核验器。每次请求只提供一个赛道和一批候选企业。所有企业都只能与请求顶部唯一的track比较，不得引入、猜测或沿用其他赛道作为判断目标。

规则优先级：
1. track中的included_activities、excluded_activities、core_products和definition是本次判断的权威边界；赛道名称只是简称，不得用名称中的“制造”“研发”等词自行收窄边界。
2. 如果included_activities明确纳入设计、研发、CRO、CDMO、测试、系统集成、技术服务或核心部件，企业无需制造最终产品也可以MATCH。
3. 企业可以同时属于多个赛道。即使它更像其他赛道，只要明确满足当前track，也应判为MATCH；不得因为“更适合其他赛道”而判NOT_MATCH。

逐家企业独立判断：
- MATCH：企业资料明确表明其实际产品、研发、生产、产业化交付或服务活动满足当前track的定义、核心产品或纳入活动。
- NOT_MATCH：企业资料充分，且实际主营明确只属于当前track的排除项、销售服务、下游应用或无关业务，同时没有满足纳入范围的明确证据。
- EVIDENCE_INSUFFICIENT：只有泛化工商词、企业资料缺失、角色不清或证据互相矛盾，无法确认是否满足当前track。

只能使用当前企业对象内的资料判断该企业，不得把同批其他企业的业务、证据或结论用于当前企业。企业名称、主行业以及宽泛的“技术开发/设备制造/软件开发”不能单独构成证据。evidence_sufficient只有在本地资料足以区分MATCH与NOT_MATCH时才为true。

只输出一个JSON对象，顶层只能有decisions。每个decision只能包含：candidate_id、label、evidence_sufficient、confidence、reason_code、brief_reason。brief_reason不超过50个汉字，只说明企业与当前唯一赛道的关系，不得提及其他赛道，不复制长段原文，不输出分析过程、Markdown或额外字段。"""


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def clip(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    head = int(limit * 0.72)
    tail = limit - head
    return text[:head] + "……" + text[-tail:]


def compact_list(values: Any, maximum: int = 8, item_limit: int = 180) -> list[str]:
    if not isinstance(values, list):
        return []
    return [clip(value, item_limit) for value in values[:maximum] if str(value or "").strip()]


def company_payload(candidate: dict[str, Any], company: dict[str, Any]) -> dict[str, Any]:
    profile = company.get("company_profile_for_classification") or company.get("company_profile") or ""
    return {
        "candidate_id": candidate["candidate_id"],
        "name": company.get("company_name", ""),
        "primary_industry": company.get("primary_industry", ""),
        "national_industry": company.get("national_industry_name", ""),
        "profile": clip(profile, 1800),
        "business_scope": clip(company.get("business_scope"), 1600),
        "data_quality_flags": company.get("data_quality_flags", []),
    }


def track_payload(track: dict[str, Any]) -> dict[str, Any]:
    return {
        "track_id": str(track.get("track_id", "")),
        "name": track.get("track_name", ""),
        "definition": clip(track.get("definition"), 500),
        "core_products": compact_list(track.get("product_terms")),
        "included_activities": compact_list(track.get("activity_terms")),
        "observable_features": compact_list(track.get("feature_terms"), maximum=6),
        "excluded_activities": compact_list(track.get("negative_terms")),
    }


def pack_track_companies(
    track: dict[str, Any],
    companies: list[dict[str, Any]],
    min_size: int,
    max_size: int,
    max_chars: int,
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    track_chars = len(json.dumps(track, ensure_ascii=False, separators=(",", ":")))
    current_chars = track_chars
    for payload in companies:
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        if current and len(current) >= min_size and (len(current) >= max_size or current_chars + size > max_chars):
            batches.append(current)
            current, current_chars = [], track_chars
        current.append(payload)
        current_chars += size
        if len(current) >= max_size:
            batches.append(current)
            current, current_chars = [], track_chars
    if current:
        batches.append(current)

    if len(batches) >= 2 and len(batches[-1]) < min_size:
        previous, last = batches[-2], batches[-1]
        if len(previous) + len(last) <= max_size:
            batches[-2:] = [previous + last]
        else:
            while len(last) < min_size and len(previous) > min_size:
                last.insert(0, previous.pop())
    return batches


def make_batch_record(
    index: int,
    track: dict[str, Any],
    companies: list[dict[str, Any]],
) -> dict[str, Any]:
    content = json.dumps({"track": track, "companies": companies}, ensure_ascii=False, separators=(",", ":"))
    input_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "batch_id": f"B{index:04d}_{input_hash[:10]}",
        "input_hash": input_hash,
        "track": track,
        "candidate_count": len(companies),
        "input_char_count": len(content),
        "companies": companies,
    }


def build_user_prompt(batch: dict[str, Any]) -> str:
    contract = {
        "decisions": [{
            "candidate_id": "必须原样复制",
            "label": "MATCH|NOT_MATCH|EVIDENCE_INSUFFICIENT",
            "evidence_sufficient": True,
            "confidence": "HIGH|MEDIUM|LOW",
            "reason_code": "限定枚举",
            "brief_reason": "不超过50个汉字",
        }]
    }
    return (
        "请只针对下面唯一的track，逐家核验companies中的企业。每个candidate_id必须且只能返回一次，顺序与输入一致。\n"
        "每家企业都只与这个track比较；不得引用其他企业的信息，不得判断其是否更适合其他赛道。\n"
        "included_activities和excluded_activities是权威边界；不得仅凭赛道名称自行增加限制。\n"
        "一致性要求：EVIDENCE_INSUFFICIENT必须对应evidence_sufficient=false；"
        "MATCH或NOT_MATCH若证据不足也必须改为EVIDENCE_INSUFFICIENT。\n"
        f"reason_code只能使用：{sorted(REASON_CODES)}\n"
        f"输出契约示意：{json.dumps(contract, ensure_ascii=False, separators=(',', ':'))}\n"
        f"唯一赛道track：{json.dumps(batch['track'], ensure_ascii=False, separators=(',', ':'))}\n"
        f"候选企业companies：{json.dumps(batch['companies'], ensure_ascii=False, separators=(',', ':'))}"
    )


def extract_json_content(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices") or []
    if not choices:
        raise ValueError("API response has no choices")
    content = choices[0].get("message", {}).get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise ValueError("API response has empty content")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def validate_result(result: dict[str, Any], batch: dict[str, Any]) -> list[dict[str, Any]]:
    if set(result) != {"decisions"} or not isinstance(result["decisions"], list):
        raise ValueError("top-level output must contain only decisions array")
    expected = [row["candidate_id"] for row in batch["companies"]]
    decisions = result["decisions"]
    if len(decisions) != len(expected):
        raise ValueError("decision count mismatch")
    if [row.get("candidate_id") for row in decisions] != expected:
        raise ValueError("candidate IDs or ordering mismatch")
    required = {"candidate_id", "label", "evidence_sufficient", "confidence", "reason_code", "brief_reason"}
    for row in decisions:
        if set(row) != required:
            raise ValueError(f"invalid decision fields for {row.get('candidate_id')}")
        if row["label"] not in LABELS:
            raise ValueError("invalid label")
        if type(row["evidence_sufficient"]) is not bool:
            raise ValueError("evidence_sufficient must be boolean")
        if row["confidence"] not in CONFIDENCE:
            raise ValueError("invalid confidence")
        if row["reason_code"] not in REASON_CODES:
            raise ValueError("invalid reason_code")
        if not isinstance(row["brief_reason"], str) or len(row["brief_reason"]) > 80:
            raise ValueError("brief_reason must be a short string")
        if row["label"] == "EVIDENCE_INSUFFICIENT" and row["evidence_sufficient"]:
            raise ValueError("insufficient label cannot have sufficient evidence")
        if row["label"] != "EVIDENCE_INSUFFICIENT" and not row["evidence_sufficient"]:
            raise ValueError("insufficient evidence must use EVIDENCE_INSUFFICIENT")
    return decisions


async def verify_batch(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    batch: dict[str, Any],
    model: str,
    raw_dir: Path,
    retries: int,
    force: bool,
) -> tuple[str, bool, str, dict[str, Any] | None]:
    output_path = raw_dir / f"{batch['batch_id']}.json"
    if output_path.exists() and not force:
        try:
            cached = json.loads(output_path.read_text(encoding="utf-8"))
            if cached.get("input_hash") != batch["input_hash"]:
                raise ValueError("cached input hash changed")
            validate_result(cached["result"], batch)
            return batch["batch_id"], True, "cached", cached
        except (ValueError, KeyError, json.JSONDecodeError):
            pass

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(batch)},
        ],
        "response_format": {"type": "json_object"},
        "enable_thinking": False,
        "max_completion_tokens": 1800,
        "temperature": 0,
    }
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            async with semaphore:
                response = await client.post("/chat/completions", json=body)
            if response.status_code in {408, 409, 429, 500, 502, 503, 504}:
                raise httpx.HTTPStatusError(
                    f"retryable HTTP {response.status_code}: {response.text[:800]}",
                    request=response.request, response=response,
                )
            if response.is_error:
                raise ValueError(f"HTTP {response.status_code}: {response.text[:1600]}")
            api_payload = response.json()
            result = extract_json_content(api_payload)
            validate_result(result, batch)
            record = {
                "batch_id": batch["batch_id"], "input_hash": batch["input_hash"],
                "model": model, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "usage": api_payload.get("usage", {}), "result": result,
            }
            output_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return batch["batch_id"], True, f"generated attempt={attempt}", record
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                await asyncio.sleep(min(30.0, 1.5 * 2 ** (attempt - 1) + random.random()))
    return batch["batch_id"], False, last_error, None


def final_status(decision: dict[str, Any]) -> str:
    label = decision["label"]
    sufficient = decision["evidence_sufficient"]
    confidence = decision["confidence"]
    reason_code = decision["reason_code"]
    if label == "MATCH" and sufficient:
        if confidence == "HIGH":
            return "CONFIRMED_MATCH"
        if confidence == "MEDIUM":
            return "PROBABLE_MATCH"
        return "NEEDS_VERIFICATION"
    if label == "NOT_MATCH" and sufficient:
        if confidence == "HIGH":
            return "CONFIRMED_NOT_MATCH"
        if confidence == "MEDIUM" and reason_code in {"UNRELATED_BUSINESS", "SALES_SERVICE_OR_APPLICATION_ONLY"}:
            return "PROBABLE_NOT_MATCH"
        return "NEEDS_VERIFICATION"
    if label == "EVIDENCE_INSUFFICIENT" or not sufficient:
        return "NEEDS_ENRICHMENT"
    return "NEEDS_VERIFICATION"


def aggregate(
    selected_candidates: list[dict[str, Any]],
    batches: list[dict[str, Any]],
    raw_dir: Path,
    output_dir: Path,
    model: str,
    concurrency: int,
) -> dict[str, Any]:
    decisions: dict[str, dict[str, Any]] = {}
    usage = Counter()
    completed_batches = 0
    for batch in batches:
        path = raw_dir / f"{batch['batch_id']}.json"
        if not path.exists():
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("input_hash") != batch["input_hash"]:
            continue
        for decision in validate_result(record["result"], batch):
            decisions[decision["candidate_id"]] = decision
        usage.update({key: int(value) for key, value in (record.get("usage") or {}).items() if isinstance(value, (int, float))})
        completed_batches += 1

    rows: list[dict[str, Any]] = []
    for candidate in selected_candidates:
        decision = decisions.get(candidate["candidate_id"])
        row = dict(candidate)
        if decision:
            status = final_status(decision)
            row.update({
                "local_evidence_label": decision["label"],
                "evidence_sufficient": decision["evidence_sufficient"],
                "verification_confidence": decision["confidence"],
                "verification_reason_code": decision["reason_code"],
                "verification_brief_reason": decision["brief_reason"],
                "decision_status": status,
                "previous_decision_status": candidate.get("decision_status", ""),
                "automatic_disposition": {
                    "CONFIRMED_MATCH": "ACCEPT",
                    "PROBABLE_MATCH": "ACCEPT",
                    "CONFIRMED_NOT_MATCH": "EXCLUDE",
                    "PROBABLE_NOT_MATCH": "EXCLUDE",
                    "NEEDS_ENRICHMENT": "ENRICH",
                    "NEEDS_VERIFICATION": "REVIEW",
                }[status],
            })
        else:
            row.update({
                "local_evidence_label": "",
                "evidence_sufficient": None,
                "verification_confidence": "",
                "verification_reason_code": "",
                "verification_brief_reason": "",
                "decision_status": "VERIFICATION_PENDING",
                "previous_decision_status": candidate.get("decision_status", ""),
                "automatic_disposition": "PENDING",
            })
        rows.append(row)

    output_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), output_dir / "candidate_decisions.parquet", compression="zstd")
    with (output_dir / "candidate_decisions.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        if rows:
            writer.writeheader(); writer.writerows(rows)
    with (output_dir / "candidate_decisions.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    status_counts = Counter(row["decision_status"] for row in rows)
    label_counts = Counter(row["local_evidence_label"] for row in rows if row["local_evidence_label"])
    report = {
        "schema_version": "local-evidence-verification-3.0",
        "request_structure": "one-track-with-company-batch",
        "model": model,
        "thinking_enabled": False,
        "candidate_count": len(selected_candidates),
        "track_count": len({str(candidate["track_id"]) for candidate in selected_candidates}),
        "batch_count": len(batches),
        "single_track_batch_count": sum(1 for batch in batches if batch.get("track")),
        "cross_track_batch_count": 0,
        "completed_batch_count": completed_batches,
        "completed_candidate_count": len(decisions),
        "batch_size_counts": dict(Counter(batch["candidate_count"] for batch in batches)),
        "input_char_quantiles": {},
        "label_counts": dict(label_counts),
        "decision_status_counts": dict(status_counts),
        "api_usage": dict(usage),
        "concurrency": concurrency,
        "external_evidence_or_agent_used": False,
    }
    if batches:
        chars = sorted(batch["input_char_count"] for batch in batches)
        for name, fraction in (("min", 0), ("p50", .5), ("p90", .9), ("max", 1)):
            index = min(round((len(chars) - 1) * fraction), len(chars) - 1)
            report["input_char_quantiles"][name] = chars[index]
    (output_dir / "verification_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def prepare(args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates = pq.read_table(args.candidates).to_pylist()
    if args.candidate_limit:
        candidates = candidates[:args.candidate_limit]
    companies = {str(row["company_id"]): row for row in pq.read_table(args.companies).to_pylist()}
    tracks = {str(row["track_id"]): row for row in pq.read_table(args.tracks).to_pylist()}
    candidates_by_track: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        candidates_by_track.setdefault(str(candidate["track_id"]), []).append(candidate)

    batch_parts: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for track_id, track_candidates in candidates_by_track.items():
        shared_track = track_payload(tracks[track_id])
        company_payloads = [
            company_payload(candidate, companies[str(candidate["company_id"])])
            for candidate in track_candidates
        ]
        for packed_companies in pack_track_companies(
            shared_track,
            company_payloads,
            args.min_batch_size,
            args.max_batch_size,
            args.max_batch_chars,
        ):
            batch_parts.append((shared_track, packed_companies))

    batches = [
        make_batch_record(index, shared_track, packed_companies)
        for index, (shared_track, packed_companies) in enumerate(batch_parts, start=1)
    ]
    if args.limit_batches:
        batches = batches[:args.limit_batches]
        selected_ids = {row["candidate_id"] for batch in batches for row in batch["companies"]}
        candidates = [row for row in candidates if row["candidate_id"] in selected_ids]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "input_batches.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for batch in batches:
            handle.write(json.dumps(batch, ensure_ascii=False, separators=(",", ":")) + "\n")
    manifest = {
        "schema_version": "local-evidence-verification-input-2.0",
        "request_structure": "one-track-with-company-batch",
        "candidate_count": len(candidates), "track_count": len({batch["track"]["track_id"] for batch in batches}),
        "batch_count": len(batches), "cross_track_batch_count": 0,
        "batch_size_counts": dict(Counter(batch["candidate_count"] for batch in batches)),
        "min_batch_size": args.min_batch_size, "max_batch_size": args.max_batch_size,
        "max_batch_chars": args.max_batch_chars,
    }
    (args.output_dir / "input_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return candidates, batches


async def async_main(args: argparse.Namespace) -> int:
    candidates, batches = prepare(args)
    if args.print_prompt:
        print(SYSTEM_PROMPT)
        print(build_user_prompt(batches[0]))
        return 0
    if args.prepare_only:
        report = aggregate(candidates, batches, args.raw_dir, args.output_dir, "NOT_RUN", args.concurrency)
        print(json.dumps(report, ensure_ascii=False))
        return 0

    load_env(args.env)
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    model = os.getenv("DASHSCOPE_VERIFICATION_MODEL", os.getenv("DASHSCOPE_MODEL", "deepseek-v4.1-flash")).strip()
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is missing from environment or .env")

    args.raw_dir.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(args.timeout, connect=30.0)
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    semaphore = asyncio.Semaphore(args.concurrency)
    failures: list[tuple[str, str]] = []
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout, limits=limits) as client:
        tasks = [asyncio.create_task(verify_batch(client, semaphore, batch, model, args.raw_dir, args.retries, args.force)) for batch in batches]
        completed = 0
        for future in asyncio.as_completed(tasks):
            batch_id, ok, detail, _ = await future
            completed += 1
            print(f"[{completed}/{len(tasks)}] {'OK' if ok else 'FAIL'} {batch_id} {detail}", flush=True)
            if not ok:
                failures.append((batch_id, detail))

    report = aggregate(candidates, batches, args.raw_dir, args.output_dir, model, args.concurrency)
    print(json.dumps(report, ensure_ascii=False))
    if failures:
        (args.output_dir / "failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{len(failures)} batch(es) failed; rerun to resume", file=sys.stderr)
        return 1
    failure_path = args.output_dir / "failures.json"
    if failure_path.exists():
        failure_path.unlink()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=Path("data/processed/company_track_pipeline/candidates/candidates_hybrid.parquet"))
    parser.add_argument("--companies", type=Path, default=Path("data/processed/company_track_pipeline/companies/companies_classification_ready.parquet"))
    parser.add_argument("--tracks", type=Path, default=Path("data/processed/company_track_pipeline/tracks/tracks_retrieval_ready.parquet"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--raw-dir", type=Path, default=Path("data/processed/company_track_pipeline/cache/local_verification_raw"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed/company_track_pipeline/local_verification"))
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--min-batch-size", type=int, default=4)
    parser.add_argument("--max-batch-size", type=int, default=8)
    parser.add_argument("--max-batch-chars", type=int, default=26000)
    parser.add_argument("--candidate-limit", type=int, default=2000)
    parser.add_argument("--limit-batches", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--print-prompt", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 16:
        parser.error("--concurrency must be between 1 and 16")
    if not 1 <= args.min_batch_size <= args.max_batch_size <= 12:
        parser.error("batch sizes must satisfy 1 <= min <= max <= 12")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main(parse_args())))
