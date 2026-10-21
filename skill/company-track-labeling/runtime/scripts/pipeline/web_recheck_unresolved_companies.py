#!/usr/bin/env python3
"""Enrich unresolved companies with web search, then recheck their candidate tracks."""

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
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import httpx
import pyarrow as pa
import pyarrow.parquet as pq


UNRESOLVED_INPUT_STATUSES = {"NEEDS_VERIFICATION", "NEEDS_ENRICHMENT"}
PROFILE_STATUSES = {"SUFFICIENT", "PARTIAL", "INSUFFICIENT"}
RECHECK_LABELS = {"MATCH", "NOT_MATCH", "UNRESOLVED"}
ENRICHMENT_PROMPT_VERSIONS = {
    "basic": "company-web-enrichment-1.3",
    "advanced": "company-web-enrichment-responses-2.2",
}
ADJUDICATION_PROMPT_VERSION = "company-track-adjudication-2.2"
RESPONSES_REASONING_EFFORT = "low"
PROFILE_FIELDS = {
    "core_business": (3, 100),
    "products_services": (5, 100),
    "actual_activities": (5, 100),
    "manufacturing_rd_status": (4, 100),
    "application_markets": (4, 100),
}
ADJUDICATION_MIN_COMPANIES = 4
ADJUDICATION_MAX_COMPANIES = 8

BASIC_ENRICHMENT_SYSTEM_PROMPT = """你负责联网核实单家企业，并将已有资料与检索到的有效信息合并为简明的公开业务档案。本次请求不会提供任何候选赛道；档案必须保持行业和赛道中立。

先使用企业完整名称进行精确检索，并结合统一社会信用代码、所在地、曾用名和已有工商资料确认身份；继续检索能够说明实际业务的企业介绍、产品、服务和项目资料。不能确认身份时，不得混用同名企业的信息，information_status返回INSUFFICIENT。

已有资料是档案输入的一部分。即使没有查到新的有效内容，只要已有资料包含具体且可信的实际业务事实，也应将其整理进档案；不要因为“没有新增信息”而清空已有的有效事实。

重点整理企业现实经营中的主营业务、具体产品或服务、实际研发生产或交付活动、制造与研发方式、主要应用市场。严格遵守以下边界：
1. 工商经营范围只表示依法可以经营，不能单独证明企业实际开展某项业务。
2. 企业拥有某种资质、许可证或项目备案，不能单独证明其制造相应设备或产品。
3. 设备安装、工程施工、销售、充电站建设运营和运维服务不能表述成设备制造。
4. 技术开发、研发和自主设计不能表述成自有生产；委外流片、代工或封装不能表述成自建制造产线。
5. core_business只写已经能够确认的实际主业；products_services只写已经能够确认的具体产品或服务。
6. manufacturing_rd_status只写能够确认的实际研发、生产方式或产线事实；安装施工能力和运营资质应写入actual_activities，不得写入该字段。
7. 只有检索或已有企业介绍明确说明时，才能写“委托生产”“自主制造”“多个项目”“自有App”“自有产线”等具体事实；不得根据常见商业模式补全。
8. “未发现自有产线”“不从事制造”等否定结论也必须有充分的实际业务信息支持；不能仅因没有查到而断言。

不得仅凭企业名称推断业务，不得复述冗长宣传文字，不得为了填满字段而重复同一事实，也不得用分号把多个事实塞入一个数组项来规避数量限制。

档案务必简短：每个数组遵守给定数量限制，每项只写一个具体事实且不超过100个汉字。

information_status必须按以下规则选择：
- SUFFICIENT：已经确认企业的主要业务，并找到具体产品、服务或现实经营活动。
- PARTIAL：只找到部分可信的现实业务事实，尚不足以完整说明主要业务；必须保留已经确认的事实。
- INSUFFICIENT：没有找到任何可确认的现实业务事实，或者无法确认检索结果属于目标企业；所有档案数组返回空数组。

只输出符合契约的JSON对象，不输出分析过程、Markdown或额外字段。"""

ADVANCED_ENRICHMENT_SYSTEM_PROMPT = BASIC_ENRICHMENT_SYSTEM_PROMPT.replace(
    "先使用企业完整名称进行精确检索，并结合统一社会信用代码、所在地、曾用名和已有工商资料确认身份；继续检索能够说明实际业务的企业介绍、产品、服务和项目资料。",
    "必须实际使用联网工具，而不是只整理输入材料。先使用企业完整名称进行精确检索，并结合统一社会信用代码、所在地、曾用名和已有工商资料确认身份；再以“企业完整名称+官网/产品/业务”等不同组合继续检索。找到可能属于该企业的页面后，使用网页提取工具读取正文，再整理能够说明实际业务的企业介绍、产品、服务和项目资料。",
)

ADJUDICATION_SYSTEM_PROMPT = """你负责针对一个固定赛道，分别判断一小批候选企业是否匹配。不得联网，不得补写企业档案中不存在的事实，不得在不同企业之间混用信息。输入企业的综合档案状态只会是SUFFICIENT或PARTIAL；PARTIAL表示信息不完整，但其中记录的事实仍然可信，可以用于判断。

逐家企业独立判断：
- MATCH：企业实际产品、研发、生产、产业化交付或服务活动明确满足赛道定义或纳入活动。
- NOT_MATCH：企业的实际主营产品或活动已经能够确认，且属于赛道排除项、相邻方向、销售代理、下游应用或无关业务，同时没有满足纳入范围的明确事实。无需证明企业绝对不存在任何目标活动；只要现有事实足以确定其实际主营不属于本赛道即可排除。
- UNRESOLVED：资料仍不足、身份不清、只出现工商模板词、事实相互矛盾，或者只是没有查到相关活动。

如果企业实际主营业务本身仍不清楚，仅仅没有发现目标活动不能判为NOT_MATCH。企业可以同时匹配多个赛道。企业名称、行业名称和宽泛工商词不能单独构成MATCH证据。

MATCH必须同时遵守以下限制：
1. 工商经营范围、许可证、生产资质、注册地址和企业名称只能作为线索，不能单独证明实际开展相应业务。
2. information_status为PARTIAL时，只有档案明确记录了已经发生的具体产品、研发、生产或交付活动，才能判MATCH；“经营范围包含”“可生产”“拟开展”“实际情况待核实”等表述不能支持MATCH。
3. 仅有语义相近、共同应用场景或上下游关系不能判MATCH。例如普通益生菌、传统发酵和微生物研究不自动等于合成生物制造；普通光学仪器不自动等于量子测量；汽车电子、智能驾驶服务不自动等于新能源汽车整车制造。
4. 赛道定义要求特定技术路线、制造环节、产品形态或产业化状态时，必须有对应的明确事实；不得用更宽泛的上位概念替代。
5. 仅提供零部件、软件、算法或技术服务时，只有赛道定义明确纳入该类关键系统或核心部件，并且企业已有具体产品化或交付事实，才能判MATCH。
6. 对MATCH从严判断；证据接近但尚不能确认是否满足核心定义时返回UNRESOLVED，不得根据可能性补全事实。

所有企业共用本次输入中的同一份赛道定义。每个candidate_id必须且只能返回一次，顺序与输入一致；company_id必须原样复制。brief_reason不超过60个汉字。只输出符合契约的JSON对象，不输出分析过程、Markdown或额外字段。"""


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
    head = int(limit * 0.75)
    return text[:head] + "…" + text[-(limit - head - 1):]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def write_rows(rows: list[dict[str, Any]], stem: Path) -> None:
    stem.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    with stem.with_suffix(".csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with stem.with_suffix(".jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    pq.write_table(pa.Table.from_pylist(rows), stem.with_suffix(".parquet"), compression="zstd")


def stable_job_id(prefix: str, company_id: str) -> str:
    digest = hashlib.sha256(company_id.encode("utf-8")).hexdigest()[:14]
    return f"{prefix}_{digest}"


def json_hash(value: Any) -> str:
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def company_identity(company: dict[str, Any]) -> dict[str, Any]:
    return {
        "company_id": str(company.get("company_id", "")),
        "company_name": company.get("company_name", ""),
        "unified_social_credit_code": company.get("unified_social_credit_code", ""),
        "province": company.get("province", ""),
        "city": company.get("city", ""),
        "district": company.get("district", ""),
        "former_name": clip(company.get("former_name"), 300),
        "primary_industry": company.get("primary_industry", ""),
        "national_industry_name": company.get("national_industry_name", ""),
        "business_scope": clip(company.get("business_scope"), 2200),
        "existing_profile": clip(
            company.get("company_profile_for_classification") or company.get("company_profile"), 1800
        ),
    }


def track_payload(track: dict[str, Any]) -> dict[str, Any]:
    def values(name: str, maximum: int = 10) -> list[str]:
        raw = track.get(name) or []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = [raw]
        return [clip(item, 160) for item in raw[:maximum] if str(item or "").strip()]

    return {
        "track_id": str(track.get("track_id", "")),
        "track_name": track.get("track_name") or track.get("name", ""),
        "definition": clip(track.get("definition"), 700),
        "core_products": values("product_terms"),
        "included_activities": values("activity_terms"),
        "observable_features": values("feature_terms", 8),
        "excluded_activities": values("negative_terms", 12),
    }


def prepare_jobs(args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    if args.escalate_from:
        base_profiles_path = args.escalate_from / "company_supplementary_profiles.jsonl"
        base_decisions_path = args.escalate_from / "candidate_decisions_final.csv"
        if not base_profiles_path.exists() or not base_decisions_path.exists():
            raise FileNotFoundError(
                "--escalate-from must contain company_supplementary_profiles.jsonl "
                "and candidate_decisions_final.csv"
            )
        base_profiles = {
            str(row["company_id"]): row for row in read_jsonl(base_profiles_path)
        }
        insufficient_company_ids = {
            company_id
            for company_id, row in base_profiles.items()
            if row.get("information_status") == "INSUFFICIENT"
        }
        all_decisions = read_csv(base_decisions_path)
        unresolved = [
            row
            for row in all_decisions
            if row["company_id"] in insufficient_company_ids
            and row.get("decision_status") == "UNRESOLVED"
            and row.get("decision_basis") == "WEB_INFORMATION_INSUFFICIENT"
        ]
        args.target_candidate_ids = {row["candidate_id"] for row in unresolved}
    else:
        all_decisions = read_csv(args.candidates)
        unresolved = [row for row in all_decisions if row["decision_status"] in UNRESOLVED_INPUT_STATUSES]
        args.target_candidate_ids = {row["candidate_id"] for row in unresolved}
    companies = {str(row["company_id"]): row for row in pq.read_table(args.companies).to_pylist()}
    tracks = {str(row["track_id"]): row for row in pq.read_table(args.tracks).to_pylist()}

    grouped: dict[str, list[dict[str, str]]] = {}
    for row in unresolved:
        grouped.setdefault(row["company_id"], []).append(row)

    enrichment_jobs: list[dict[str, Any]] = []
    company_payloads: dict[str, dict[str, Any]] = {}
    for company_id, candidates in sorted(grouped.items(), key=lambda item: min(int(x["global_rank"]) for x in item[1])):
        if company_id not in companies:
            raise KeyError(f"company not found: {company_id}")
        identity = company_identity(companies[company_id])
        company_payloads[company_id] = identity
        enrichment_input = {"company": identity}
        enrichment_jobs.append({
            "job_id": stable_job_id("E", company_id),
            "company_id": company_id,
            "input_hash": json_hash(enrichment_input),
            "input": enrichment_input,
        })

    grouped_by_track: dict[str, list[dict[str, Any]]] = {}
    for candidate in sorted(unresolved, key=lambda row: int(row["global_rank"])):
        track_id = candidate["track_id"]
        if track_id not in tracks:
            raise KeyError(f"track not found: {track_id}")
        grouped_by_track.setdefault(track_id, []).append({
            "candidate_id": candidate["candidate_id"],
            "company_id": candidate["company_id"],
            "previous_status": candidate["decision_status"],
            "company": company_payloads[candidate["company_id"]],
        })

    adjudication_bases = [
        {
            "track_id": track_id,
            "track": track_payload(tracks[track_id]),
            "companies": candidates,
        }
        for track_id, candidates in grouped_by_track.items()
    ]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "enrichment_jobs.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for job in enrichment_jobs:
            handle.write(json.dumps(job, ensure_ascii=False, separators=(",", ":")) + "\n")
    manifest = {
        "schema_version": "company-web-recheck-input-2.0",
        "enrichment_prompt_version": args.enrichment_prompt_version,
        "adjudication_prompt_version": ADJUDICATION_PROMPT_VERSION,
        "search_tier": args.search_tier,
        "escalation_source": str(args.escalate_from) if args.escalate_from else None,
        "source_candidate_count": len(all_decisions),
        "unresolved_candidate_count": len(unresolved),
        "unresolved_company_count": len(grouped),
        "source_status_counts": dict(Counter(row["decision_status"] for row in unresolved)),
        "candidate_count_per_company": dict(sorted(Counter(len(v) for v in grouped.values()).items())),
        "max_candidate_count_per_company": max(map(len, grouped.values()), default=0),
        "unresolved_track_count": len(grouped_by_track),
        "candidate_count_per_track": dict(sorted(Counter(len(v) for v in grouped_by_track.values()).items())),
        "max_candidate_count_per_track": max(map(len, grouped_by_track.values()), default=0),
        "request_design": {
            "enrichment": (
                "one company, Responses API with web_search and web_extractor, no track data"
                if args.search_tier == "advanced"
                else "one company, Chat Completions with low-thinking single-pass web search, no track data"
            ),
            "adjudication": (
                "one track with a dynamic batch of 4-8 unresolved companies, "
                "sized by serialized input length, web search disabled"
            ),
        },
    }
    (args.output_dir / "input_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return enrichment_jobs, adjudication_bases, all_decisions


def enrichment_prompt(job: dict[str, Any]) -> str:
    contract = {
        "company_id": job["company_id"],
        "information_status": "SUFFICIENT|PARTIAL|INSUFFICIENT",
        "supplementary_profile": {key: [] for key in PROFILE_FIELDS},
    }
    return (
        "请联网检索并补充下面这一家企业的现实业务档案。不要判断或猜测它属于哪个赛道。\n"
        "字段上限：core_business最多3项；products_services最多5项；actual_activities最多5项；"
        "manufacturing_rd_status最多4项；application_markets最多4项；每项最多100个汉字。\n"
        f"输出契约：{json.dumps(contract, ensure_ascii=False, separators=(',', ':'))}\n"
        f"企业身份与已有资料：{json.dumps(job['input']['company'], ensure_ascii=False, separators=(',', ':'))}"
    )


def validate_profile(result: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    if set(result) != {"company_id", "information_status", "supplementary_profile"}:
        raise ValueError("invalid enrichment top-level fields")
    if str(result["company_id"]) != job["company_id"]:
        raise ValueError("company_id mismatch")
    if result["information_status"] not in PROFILE_STATUSES:
        raise ValueError("invalid information_status")
    profile = result["supplementary_profile"]
    if not isinstance(profile, dict) or set(profile) != set(PROFILE_FIELDS):
        raise ValueError("invalid supplementary_profile fields")
    for key, (maximum, char_limit) in PROFILE_FIELDS.items():
        values = profile[key]
        if not isinstance(values, list):
            raise ValueError(f"invalid {key} value")
        normalized: list[str] = []
        for item in values:
            if not isinstance(item, str):
                raise ValueError(f"invalid {key} item")
            item = re.sub(r"\s+", " ", item).strip()
            if item:
                normalized.append(item[:char_limit])
        profile[key] = normalized[:maximum]
    has_facts = any(profile.values())
    if result["information_status"] == "INSUFFICIENT" and has_facts:
        result["information_status"] = "PARTIAL"
    elif result["information_status"] in {"SUFFICIENT", "PARTIAL"} and not has_facts:
        result["information_status"] = "INSUFFICIENT"
    return result


def split_company_batch(companies: list[dict[str, Any]], target_chars: int) -> list[list[dict[str, Any]]]:
    """Pack 4-8 companies per request, using serialized length as a token-cost proxy."""
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for company in companies:
        company_chars = len(json.dumps(company, ensure_ascii=False, separators=(",", ":")))
        should_close = (
            len(current) >= ADJUDICATION_MIN_COMPANIES
            and current_chars + company_chars > target_chars
        )
        if len(current) >= ADJUDICATION_MAX_COMPANIES or should_close:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(company)
        current_chars += company_chars
    if current:
        batches.append(current)

    if len(batches) >= 2 and len(batches[-1]) < ADJUDICATION_MIN_COMPANIES:
        needed = ADJUDICATION_MIN_COMPANIES - len(batches[-1])
        if len(batches[-2]) - needed >= ADJUDICATION_MIN_COMPANIES:
            batches[-1][:0] = batches[-2][-needed:]
            del batches[-2][-needed:]
        elif len(batches[-2]) + len(batches[-1]) <= ADJUDICATION_MAX_COMPANIES:
            batches[-2].extend(batches.pop())
    return batches


def build_adjudication_jobs(
    bases: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
    target_chars: int,
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for base in bases:
        companies = []
        for candidate in base["companies"]:
            company_id = candidate["company_id"]
            if company_id not in profiles:
                continue
            profile = profiles[company_id]
            if profile["information_status"] == "INSUFFICIENT":
                continue
            companies.append({
                **candidate,
                "information_status": profile["information_status"],
                "supplementary_profile": profile["supplementary_profile"],
            })
        for batch_index, batch in enumerate(split_company_batch(companies, target_chars), start=1):
            payload = {"track": base["track"], "companies": batch}
            batch_key = base["track_id"] + "|" + "|".join(row["candidate_id"] for row in batch)
            jobs.append({
                "job_id": stable_job_id("A", batch_key),
                "track_id": base["track_id"],
                "batch_index": batch_index,
                "companies": batch,
                "input_hash": json_hash(payload),
                "input": payload,
            })
    return jobs


def adjudication_prompt(job: dict[str, Any]) -> str:
    contract = {
        "track_id": job["track_id"],
        "decisions": [{
            "candidate_id": "必须原样复制",
            "company_id": "必须原样复制",
            "label": "MATCH|NOT_MATCH|UNRESOLVED",
            "brief_reason": "不超过60个汉字",
        }],
    }
    return (
        "请依据同一个赛道定义，分别判断下面每家企业与该赛道的关系。\n"
        f"输出契约：{json.dumps(contract, ensure_ascii=False, separators=(',', ':'))}\n"
        f"输入：{json.dumps(job['input'], ensure_ascii=False, separators=(',', ':'))}"
    )


def validate_adjudication(result: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    if set(result) != {"track_id", "decisions"}:
        raise ValueError("invalid adjudication top-level fields")
    if str(result["track_id"]) != job["track_id"]:
        raise ValueError("track_id mismatch")
    decisions = result["decisions"]
    expected = [row["candidate_id"] for row in job["companies"]]
    if not isinstance(decisions, list) or [row.get("candidate_id") for row in decisions] != expected:
        raise ValueError("candidate IDs, count, or ordering mismatch")
    for row, company in zip(decisions, job["companies"]):
        if set(row) != {"candidate_id", "company_id", "label", "brief_reason"}:
            raise ValueError("invalid decision fields")
        if str(row["company_id"]) != company["company_id"]:
            raise ValueError("company_id mismatch")
        if row["label"] not in RECHECK_LABELS:
            raise ValueError("invalid recheck label")
        if not isinstance(row["brief_reason"], str) or len(row["brief_reason"]) > 80:
            raise ValueError("brief_reason is too long")
    return result


def extract_json(response: dict[str, Any]) -> dict[str, Any]:
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


def extract_responses_json(response: dict[str, Any]) -> dict[str, Any]:
    texts: list[str] = []
    for item in response.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                texts.append(content["text"])
    text = "\n".join(texts).strip()
    if not text:
        raise ValueError("Responses API returned no output_text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def responses_tool_counts(response: dict[str, Any]) -> dict[str, int]:
    counts = Counter(item.get("type") for item in response.get("output") or [])
    usage_tools = (response.get("usage") or {}).get("x_tools") or {}
    search_count = int((usage_tools.get("web_search") or {}).get("count") or counts["web_search_call"])
    extractor_count = int((usage_tools.get("web_extractor") or {}).get("count") or counts["web_extractor_call"])
    return {"web_search_calls": search_count, "web_extractor_calls": extractor_count}


async def call_job(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    job: dict[str, Any],
    raw_dir: Path,
    model: str,
    system_prompt: str,
    prompt_builder: Callable[[dict[str, Any]], str],
    validator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    retries: int,
    force: bool,
    web_search: bool,
    search_strategy: str,
    web_api: str,
    prompt_version: str,
) -> tuple[str, bool, str]:
    output_path = raw_dir / f"{job['job_id']}.json"
    if output_path.exists() and not force:
        try:
            cached = json.loads(output_path.read_text(encoding="utf-8"))
            if cached.get("input_hash") != job["input_hash"]:
                raise ValueError("cached input changed")
            if cached.get("prompt_version") != prompt_version:
                raise ValueError("cached prompt version changed")
            if cached.get("model") != model or bool(cached.get("web_search_enabled")) != web_search:
                raise ValueError("cached model or search mode changed")
            if cached.get("api_mode") != (web_api if web_search else "chat_completions"):
                raise ValueError("cached API mode changed")
            if web_search and web_api == "chat_completions" and cached.get("search_strategy") != search_strategy:
                raise ValueError("cached search strategy changed")
            validator(cached["result"], job)
            return job["job_id"], True, "cached"
        except (ValueError, KeyError, json.JSONDecodeError):
            pass

    if web_search and web_api == "responses":
        body: dict[str, Any] = {
            "model": model,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt_builder(job)},
            ],
            "tools": [{"type": "web_search"}, {"type": "web_extractor"}],
            "reasoning": {"effort": RESPONSES_REASONING_EFFORT},
            "max_output_tokens": 6000,
        }
        endpoint = "/responses"
    else:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt_builder(job)},
            ],
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
            "max_tokens": 1500,
            "temperature": 0,
        }
        if web_search:
            body["enable_search"] = True
            body["search_options"] = {"forced_search": True, "search_strategy": search_strategy}
        endpoint = "/chat/completions"

    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            async with semaphore:
                response = await client.post(endpoint, json=body)
            if response.status_code in {408, 409, 429, 500, 502, 503, 504}:
                raise httpx.HTTPStatusError(
                    f"retryable HTTP {response.status_code}: {response.text[:600]}",
                    request=response.request,
                    response=response,
                )
            if response.is_error:
                raise ValueError(f"HTTP {response.status_code}: {response.text[:1200]}")
            payload = response.json()
            if web_search and web_api == "responses":
                tool_counts = responses_tool_counts(payload)
                if tool_counts["web_search_calls"] < 1:
                    raise ValueError("Responses API completed without using web_search")
                result = validator(extract_responses_json(payload), job)
            else:
                tool_counts = {"web_search_calls": 0, "web_extractor_calls": 0}
                result = validator(extract_json(payload), job)
            usage = {
                key: int(value)
                for key, value in (payload.get("usage") or {}).items()
                if isinstance(value, (int, float))
            }
            usage.update(tool_counts)
            record = {
                "job_id": job["job_id"],
                "company_id": job.get("company_id"),
                "track_id": job.get("track_id"),
                "input_hash": job["input_hash"],
                "prompt_version": prompt_version,
                "model": model,
                "api_mode": web_api if web_search else "chat_completions",
                "reasoning_effort": (
                    RESPONSES_REASONING_EFFORT
                    if web_search and web_api == "responses"
                    else None
                ),
                "web_search_enabled": web_search,
                "search_strategy": search_strategy if web_search and web_api == "chat_completions" else None,
                "usage": usage,
                "result": result,
            }
            output_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return job["job_id"], True, f"generated attempt={attempt}"
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                await asyncio.sleep(min(30.0, 1.5 * 2 ** (attempt - 1) + random.random()))
    return job["job_id"], False, last_error


def load_cached_results(
    jobs: list[dict[str, Any]],
    raw_dir: Path,
    validator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    prompt_version: str,
) -> tuple[dict[str, dict[str, Any]], Counter]:
    results: dict[str, dict[str, Any]] = {}
    usage: Counter = Counter()
    for job in jobs:
        path = raw_dir / f"{job['job_id']}.json"
        if not path.exists():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("input_hash") != job["input_hash"]:
                continue
            if record.get("prompt_version") != prompt_version:
                continue
            result = validator(record["result"], job)
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
        results[job["job_id"]] = result
        usage.update({k: int(v) for k, v in (record.get("usage") or {}).items() if isinstance(v, (int, float))})
    return results, usage


def aggregate_profiles(jobs: list[dict[str, Any]], args: argparse.Namespace, model: str) -> dict[str, dict[str, Any]]:
    cached, usage = load_cached_results(
        jobs, args.enrichment_raw_dir, validate_profile, args.enrichment_prompt_version
    )
    profiles = {
        job["company_id"]: cached[job["job_id"]]
        for job in jobs
        if job["job_id"] in cached
    }
    rows = [profiles[job["company_id"]] for job in jobs if job["company_id"] in profiles]
    if rows:
        write_rows(rows, args.output_dir / "company_supplementary_profiles")
    report = {
        "schema_version": "company-web-enrichment-3.0",
        "prompt_version": args.enrichment_prompt_version,
        "search_tier": args.search_tier,
        "model": model,
        "api_mode": getattr(args, "web_api", "responses"),
        "web_search_enabled": True,
        "thinking_enabled": getattr(args, "web_api", "responses") == "responses",
        "reasoning_effort": (
            RESPONSES_REASONING_EFFORT
            if getattr(args, "web_api", "responses") == "responses"
            else None
        ),
        "company_count": len(jobs),
        "completed_company_count": len(rows),
        "pending_company_count": len(jobs) - len(rows),
        "information_status_counts": dict(Counter(row["information_status"] for row in rows)),
        "api_usage": dict(usage),
    }
    (args.output_dir / "enrichment_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return profiles


def aggregate_adjudications(
    jobs: list[dict[str, Any]], args: argparse.Namespace, model: str
) -> dict[str, dict[str, Any]]:
    results, usage = load_cached_results(
        jobs, args.adjudication_raw_dir, validate_adjudication, ADJUDICATION_PROMPT_VERSION
    )
    rows: list[dict[str, Any]] = []
    for job in jobs:
        result = results.get(job["job_id"])
        if not result:
            continue
        for decision in result["decisions"]:
            rows.append({"track_id": job["track_id"], **decision})
    if rows:
        write_rows(rows, args.output_dir / "candidate_recheck_decisions")
    input_lengths = [
        len(json.dumps(job["input"], ensure_ascii=False, separators=(",", ":")))
        for job in jobs
    ]
    report = {
        "schema_version": "company-track-web-recheck-2.0",
        "prompt_version": ADJUDICATION_PROMPT_VERSION,
        "model": model,
        "web_search_enabled": False,
        "track_count": len({job["track_id"] for job in jobs}),
        "batch_count": len(jobs),
        "batch_size_counts": dict(sorted(Counter(len(job["companies"]) for job in jobs).items())),
        "input_char_length": {
            "min": min(input_lengths, default=0),
            "average": round(sum(input_lengths) / len(input_lengths), 2) if input_lengths else 0,
            "max": max(input_lengths, default=0),
            "target": args.adjudication_target_chars,
        },
        "completed_batch_count": len(results),
        "pending_batch_count": len(jobs) - len(results),
        "completed_company_count": len({row["company_id"] for row in rows}),
        "completed_candidate_count": len(rows),
        "label_counts": dict(Counter(row["label"] for row in rows)),
        "api_usage": dict(usage),
    }
    (args.output_dir / "adjudication_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return results


async def run_jobs(
    jobs: list[dict[str, Any]],
    args: argparse.Namespace,
    raw_dir: Path,
    model: str,
    system_prompt: str,
    prompt_builder: Callable[[dict[str, Any]], str],
    validator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    web_search: bool,
    search_strategy: str,
    web_api: str,
    prompt_version: str,
) -> list[tuple[str, str]]:
    selected = jobs[: args.limit_companies] if args.limit_companies else jobs
    raw_dir.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {args.api_key}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(args.timeout, connect=30.0)
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    semaphore = asyncio.Semaphore(args.concurrency)
    failures: list[tuple[str, str]] = []
    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, timeout=timeout, limits=limits) as client:
        tasks = [asyncio.create_task(call_job(
            client, semaphore, job, raw_dir, model, system_prompt, prompt_builder, validator,
            args.retries, args.force, web_search, search_strategy, web_api, prompt_version,
        )) for job in selected]
        for index, future in enumerate(asyncio.as_completed(tasks), start=1):
            job_id, ok, detail = await future
            print(f"[{index}/{len(tasks)}] {'OK' if ok else 'FAIL'} {job_id} {detail}", flush=True)
            if not ok:
                failures.append((job_id, detail))
    return failures


def merge_results(
    all_decisions: list[dict[str, str]],
    adjudication_jobs: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    results, _ = load_cached_results(
        adjudication_jobs,
        args.adjudication_raw_dir,
        validate_adjudication,
        ADJUDICATION_PROMPT_VERSION,
    )
    rechecks = {
        decision["candidate_id"]: decision
        for result in results.values()
        for decision in result["decisions"]
    }
    unresolved_ids = set(args.target_candidate_ids)
    insufficient_ids = {
        row["candidate_id"]
        for row in all_decisions
        if row["candidate_id"] in unresolved_ids
        and profiles.get(row["company_id"], {}).get("information_status") == "INSUFFICIENT"
    }
    missing = unresolved_ids - insufficient_ids - set(rechecks)
    if missing and not args.allow_partial:
        raise RuntimeError(
            f"cannot merge: {len(missing)} unresolved candidates have no completed recheck; "
            "rerun adjudicate or use --allow-partial"
        )

    merged: list[dict[str, Any]] = []
    mapping = {
        "MATCH": ("CONFIRMED_MATCH", "ACCEPT"),
        "NOT_MATCH": ("CONFIRMED_NOT_MATCH", "EXCLUDE"),
        "UNRESOLVED": ("UNRESOLVED", "UNRESOLVED"),
    }
    for original in all_decisions:
        row: dict[str, Any] = dict(original)
        decision = rechecks.get(original["candidate_id"])
        if args.escalate_from:
            row["pre_advanced_decision_status"] = original["decision_status"]
            row["advanced_web_information_status"] = ""
            row["advanced_web_recheck_label"] = ""
            row["advanced_web_recheck_brief_reason"] = ""
            if original["candidate_id"] not in unresolved_ids:
                merged.append(row)
                continue
            if decision:
                status, disposition = mapping[decision["label"]]
                row["advanced_web_information_status"] = profiles[original["company_id"]]["information_status"]
                row["advanced_web_recheck_label"] = decision["label"]
                row["advanced_web_recheck_brief_reason"] = decision["brief_reason"]
                row["decision_status"] = status
                row["automatic_disposition"] = disposition
                row["decision_basis"] = "ADVANCED_WEB_ENRICHED_RECHECK"
            elif original["candidate_id"] in insufficient_ids:
                row["advanced_web_information_status"] = "INSUFFICIENT"
                row["advanced_web_recheck_label"] = "UNRESOLVED"
                row["advanced_web_recheck_brief_reason"] = "高级搜索后企业公开业务信息仍不足"
                row["decision_status"] = "UNRESOLVED"
                row["automatic_disposition"] = "UNRESOLVED"
                row["decision_basis"] = "ADVANCED_WEB_INFORMATION_INSUFFICIENT"
            else:
                row["advanced_web_information_status"] = profiles.get(
                    original["company_id"], {}
                ).get("information_status", "")
                row["decision_status"] = "ADVANCED_WEB_RECHECK_PENDING"
                row["automatic_disposition"] = "PENDING"
                row["decision_basis"] = "ADVANCED_WEB_RECHECK_PENDING"
            merged.append(row)
            continue

        row["pre_web_decision_status"] = original["decision_status"]
        if decision:
            status, disposition = mapping[decision["label"]]
            row["web_information_status"] = profiles[original["company_id"]]["information_status"]
            row["web_recheck_label"] = decision["label"]
            row["web_recheck_brief_reason"] = decision["brief_reason"]
            row["decision_status"] = status
            row["automatic_disposition"] = disposition
            row["decision_basis"] = "WEB_ENRICHED_RECHECK"
        elif original["candidate_id"] in insufficient_ids:
            row["web_information_status"] = "INSUFFICIENT"
            row["web_recheck_label"] = "UNRESOLVED"
            row["web_recheck_brief_reason"] = "企业公开业务信息不足，无法进行有效赛道判定"
            row["decision_status"] = "UNRESOLVED"
            row["automatic_disposition"] = "UNRESOLVED"
            row["decision_basis"] = "WEB_INFORMATION_INSUFFICIENT"
        elif original["candidate_id"] in unresolved_ids:
            row["web_information_status"] = profiles.get(original["company_id"], {}).get("information_status", "")
            row["web_recheck_label"] = ""
            row["web_recheck_brief_reason"] = ""
            row["decision_status"] = "WEB_RECHECK_PENDING"
            row["automatic_disposition"] = "PENDING"
            row["decision_basis"] = "WEB_RECHECK_PENDING"
        else:
            row["web_information_status"] = ""
            row["web_recheck_label"] = ""
            row["web_recheck_brief_reason"] = ""
            row["decision_basis"] = "LOCAL_EVIDENCE"
        merged.append(row)

    write_rows(merged, args.output_dir / "candidate_decisions_final")
    report = {
        "schema_version": (
            "company-track-final-with-advanced-escalation-1.0"
            if args.escalate_from
            else "company-track-final-with-web-recheck-1.0"
        ),
        "candidate_count": len(merged),
        "web_recheck_target_count": len(unresolved_ids),
        "web_recheck_completed_count": len(unresolved_ids & (set(rechecks) | insufficient_ids)),
        "web_information_insufficient_count": len(insufficient_ids),
        "web_recheck_pending_count": len(missing),
        "decision_status_counts": dict(Counter(row["decision_status"] for row in merged)),
        "automatic_disposition_counts": dict(Counter(row["automatic_disposition"] for row in merged)),
    }
    (args.output_dir / "final_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


async def async_main(args: argparse.Namespace) -> int:
    load_env(args.env)
    args.search_tier = args.search_tier or os.getenv("DASHSCOPE_WEB_SEARCH_TIER", "basic").strip().lower()
    if args.search_tier not in ENRICHMENT_PROMPT_VERSIONS:
        raise RuntimeError("DASHSCOPE_WEB_SEARCH_TIER must be basic or advanced")
    if args.escalate_from and args.search_tier != "advanced":
        raise RuntimeError("--escalate-from requires --search-tier advanced")
    args.enrichment_prompt_version = ENRICHMENT_PROMPT_VERSIONS[args.search_tier]
    if args.output_dir is None:
        args.output_dir = Path("data/processed/company_track_pipeline") / (
            "web_advanced" if args.escalate_from else "web_basic"
        )
    if args.enrichment_raw_dir is None:
        args.enrichment_raw_dir = Path(
            "data/processed/company_track_pipeline/cache/web_advanced_enrichment_raw"
            if args.search_tier == "advanced"
            else "data/processed/company_track_pipeline/cache/web_basic_enrichment_raw"
        )
    if args.adjudication_raw_dir is None:
        args.adjudication_raw_dir = Path(
            "data/processed/company_track_pipeline/cache/web_advanced_adjudication_raw"
            if args.escalate_from
            else "data/processed/company_track_pipeline/cache/web_basic_adjudication_raw"
        )
    enrichment_jobs, adjudication_bases, all_decisions = prepare_jobs(args)
    if args.stage == "prepare":
        print((args.output_dir / "input_manifest.json").read_text(encoding="utf-8"))
        return 0

    args.api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    args.base_url = os.getenv(
        "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ).rstrip("/")
    search_model = os.getenv("DASHSCOPE_WEB_SEARCH_MODEL", "deepseek-v4.1-flash").strip()
    judge_model = os.getenv(
        "DASHSCOPE_VERIFICATION_MODEL", os.getenv("DASHSCOPE_MODEL", "deepseek-v4.1-flash")
    ).strip()
    search_strategy = os.getenv("DASHSCOPE_SEARCH_STRATEGY", "max").strip()
    web_api = "responses" if args.search_tier == "advanced" else "chat_completions"
    args.web_api = web_api
    if args.stage != "merge" and not args.api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is missing from environment or .env")

    if args.stage in {"enrich", "all"}:
        failures = await run_jobs(
            enrichment_jobs, args, args.enrichment_raw_dir, search_model,
            (
                ADVANCED_ENRICHMENT_SYSTEM_PROMPT
                if args.search_tier == "advanced"
                else BASIC_ENRICHMENT_SYSTEM_PROMPT
            ),
            enrichment_prompt, validate_profile, True, search_strategy, web_api,
            args.enrichment_prompt_version,
        )
        aggregate_profiles(enrichment_jobs, args, search_model)
        if failures:
            (args.output_dir / "enrichment_failures.json").write_text(
                json.dumps(failures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"{len(failures)} enrichment job(s) failed; rerun to resume", file=sys.stderr)
            return 1
        failure_path = args.output_dir / "enrichment_failures.json"
        if failure_path.exists():
            failure_path.unlink()

    profiles = aggregate_profiles(enrichment_jobs, args, search_model)
    adjudication_jobs = build_adjudication_jobs(
        adjudication_bases, profiles, args.adjudication_target_chars
    )
    with (args.output_dir / "adjudication_jobs.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for job in adjudication_jobs:
            handle.write(json.dumps(job, ensure_ascii=False, separators=(",", ":")) + "\n")

    if args.stage in {"adjudicate", "all"}:
        missing_profiles = {
            company["company_id"]
            for base in adjudication_bases
            for company in base["companies"]
            if company["company_id"] not in profiles
        }
        if missing_profiles:
            raise RuntimeError(
                f"enrichment is incomplete: {len(missing_profiles)} company profiles missing"
            )
        failures = await run_jobs(
            adjudication_jobs, args, args.adjudication_raw_dir, judge_model,
            ADJUDICATION_SYSTEM_PROMPT, adjudication_prompt, validate_adjudication, False, search_strategy, web_api,
            ADJUDICATION_PROMPT_VERSION,
        )
        aggregate_adjudications(adjudication_jobs, args, judge_model)
        if failures:
            (args.output_dir / "adjudication_failures.json").write_text(
                json.dumps(failures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"{len(failures)} adjudication job(s) failed; rerun to resume", file=sys.stderr)
            return 1
        failure_path = args.output_dir / "adjudication_failures.json"
        if failure_path.exists():
            failure_path.unlink()

    if args.stage in {"merge", "all"}:
        expected_company_ids = {job["company_id"] for job in enrichment_jobs}
        if expected_company_ids - set(profiles) and not args.allow_partial:
            raise RuntimeError("cannot merge before enrichment is complete")
        report = merge_results(all_decisions, adjudication_jobs, profiles, args)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["prepare", "enrich", "adjudicate", "merge", "all"], default="prepare")
    parser.add_argument("--search-tier", choices=["basic", "advanced"])
    parser.add_argument(
        "--escalate-from",
        type=Path,
        help="Basic-result directory; advanced mode processes only WEB_INFORMATION_INSUFFICIENT companies",
    )
    parser.add_argument("--candidates", type=Path, default=Path(
        "data/processed/company_track_pipeline/local_verification/candidate_decisions.csv"
    ))
    parser.add_argument("--companies", type=Path, default=Path(
        "data/processed/company_track_pipeline/companies/companies_classification_ready.parquet"
    ))
    parser.add_argument("--tracks", type=Path, default=Path(
        "data/processed/company_track_pipeline/tracks/tracks_retrieval_ready.parquet"
    ))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--enrichment-raw-dir", type=Path)
    parser.add_argument("--adjudication-raw-dir", type=Path)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--adjudication-target-chars", type=int, default=12000)
    parser.add_argument("--limit-companies", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 8:
        parser.error("--concurrency must be between 1 and 8")
    if args.limit_companies is not None and args.limit_companies < 1:
        parser.error("--limit-companies must be positive")
    if args.adjudication_target_chars < 4000:
        parser.error("--adjudication-target-chars must be at least 4000")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main(parse_args())))
