#!/usr/bin/env python3
"""Remap curated tracks to Lenovo SMB offerings with DashScope-compatible API."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx


SYSTEM_PROMPT = """你是企业赛道研究和中小企业IT需求分析专家。

任务：分析一个制造业赛道中的典型中小企业，判断联想SMB当前产品目录能解决哪些真实IT需求。

必须遵守：
1. 分析对象是“属于该赛道的中小企业自身”，不是它们的客户、消费者或整个产业链。
2. 只能通过 catalog_item_ids 引用给定目录中的产品。不得虚构产品、功能、行业软件或合作伙伴能力。
3. 严格区分：
   - DIRECT_SOLUTION：目录产品直接解决需求，例如办公终端、工作站、服务器、存储、网络、运维、备份、会议等。
   - INFRASTRUCTURE_ENABLER：目录产品只是承载第三方系统或专业工作负载，例如服务器承载MES、工作站运行CAD/EDA；不得宣称联想提供MES、ERP、PLM、LIMS、QMS、SCM、SCADA、CAD、CAE或EDA软件本身。
4. 目录外需求写入 ecosystem_needs，不得塞进 product_opportunities。
5. 不为凑数量推荐产品。没有可靠映射时允许返回空数组。每项推荐必须对应具体工作负载和采购触发因素。
6. 区分研发型、试制型、量产型企业；避免默认所有中小企业都建设大型数据中心或购买高端AI服务器。
7. 不使用赛道热度事件推断未提供的企业规模、预算、合规资质或现有IT系统。
8. 输出必须完全符合JSON Schema，不要输出Markdown或额外说明。
9. product_opportunities通常保留4—7项，ecosystem_needs最多5项；按同一业务问题合并，不要穷举通用IT需求。
10. 同类产品存在多个系列时只选最匹配的少数目录项；除非适用阶段或性能层级确有区别，不得罗列替代机型。
11. 优先输出由赛道特征产生的差异化机会。通用办公、会议、运维、租赁仅在该赛道材料能支持明确触发因素时纳入。
"""


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


def parse_markdown_catalog(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [re.sub(r"\*\*", "", cell.strip()) for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5 or cells[0] in {"一级产品域", "------------", ""} or set(cells[0]) <= {"-", ":", " "}:
            continue
        rows.append({
            "catalog_item_id": f"P{len(rows) + 1:03d}",
            "product_domain": cells[0],
            "category": cells[1],
            "product_service": cells[2],
            "examples": cells[3],
            "intended_scenarios": cells[4],
        })
    if not rows:
        raise ValueError(f"No catalog rows parsed from {path}")
    return rows


def load_tracks(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["tracks"] if isinstance(payload, dict) else payload


def output_schema(catalog_ids: list[str]) -> dict[str, Any]:
    text = {"type": "string"}
    string_array = {"type": "array", "items": text}
    opportunity = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "opportunity_name", "relationship", "catalog_item_ids", "target_company_stage",
            "workloads", "buying_triggers", "buyer_roles", "fit_reason", "priority", "confidence",
        ],
        "properties": {
            "opportunity_name": text,
            "relationship": {"type": "string", "enum": ["DIRECT_SOLUTION", "INFRASTRUCTURE_ENABLER"]},
            "catalog_item_ids": {"type": "array", "minItems": 1, "items": {"type": "string", "enum": catalog_ids}},
            "target_company_stage": {"type": "array", "items": {"type": "string", "enum": ["研发", "中试", "量产", "成长扩张", "通用"]}},
            "workloads": string_array,
            "buying_triggers": string_array,
            "buyer_roles": string_array,
            "fit_reason": text,
            "priority": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
    }
    ecosystem_need = {
        "type": "object",
        "additionalProperties": False,
        "required": ["need", "reason", "possible_lenovo_role"],
        "properties": {
            "need": text,
            "reason": text,
            "possible_lenovo_role": {"type": "string", "enum": ["基础设施承载", "生态合作", "当前无明确角色"]},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "track_id", "track_name", "enterprise_profile", "digital_workload_profile", "smb_constraints",
            "product_opportunities", "ecosystem_needs", "non_recommended_catalog_item_ids", "analysis_summary",
        ],
        "properties": {
            "track_id": text,
            "track_name": text,
            "enterprise_profile": {
                "type": "object", "additionalProperties": False,
                "required": ["typical_business", "typical_company_stages", "it_characteristics"],
                "properties": {
                    "typical_business": text,
                    "typical_company_stages": string_array,
                    "it_characteristics": string_array,
                },
            },
            "digital_workload_profile": string_array,
            "smb_constraints": string_array,
            "product_opportunities": {"type": "array", "items": opportunity},
            "ecosystem_needs": {"type": "array", "items": ecosystem_need},
            "non_recommended_catalog_item_ids": {"type": "array", "items": {"type": "string", "enum": catalog_ids}},
            "analysis_summary": text,
        },
    }


def build_user_prompt(track: dict[str, Any], catalog: list[dict[str, str]]) -> str:
    track_input = {
        key: track.get(key)
        for key in (
            "track_id", "name", "definition", "enterprise_archetype", "core_products_services",
            "included_activities", "excluded_activities", "observable_company_features",
            "positive_terms", "negative_terms", "classification_rules",
        )
    }
    return (
        "请根据以下赛道定义和封闭产品目录，重做该赛道的联想SMB产品机会分析。\n\n"
        "评估顺序：先归纳典型中小企业的研发、试制、量产和办公工作负载；再识别采购触发；"
        "最后才选择目录项。推荐应少而准确，并列出重要但目录外的生态需求。\n\n"
        f"赛道：\n{json.dumps(track_input, ensure_ascii=False, indent=2)}\n\n"
        f"联想SMB封闭产品目录：\n{json.dumps(catalog, ensure_ascii=False, separators=(',', ':'))}\n\n"
        "只返回一个JSON对象，字段契约如下：\n"
        "track_id、track_name：必须原样复制输入；\n"
        "enterprise_profile：含typical_business字符串、typical_company_stages字符串数组、it_characteristics字符串数组；\n"
        "digital_workload_profile、smb_constraints：字符串数组；\n"
        "product_opportunities：数组，每项含opportunity_name、relationship（仅DIRECT_SOLUTION或INFRASTRUCTURE_ENABLER）、"
        "catalog_item_ids（非空且仅使用目录ID）、target_company_stage（仅研发/中试/量产/成长扩张/通用）、"
        "workloads、buying_triggers、buyer_roles、fit_reason、priority（仅HIGH/MEDIUM/LOW）、confidence（0到1）；\n"
        "ecosystem_needs：数组，每项含need、reason、possible_lenovo_role（仅基础设施承载/生态合作/当前无明确角色）；\n"
        "non_recommended_catalog_item_ids：不适合该赛道且容易被误荐的目录ID数组；analysis_summary：简洁结论。"
    )


def validate_result(result: dict[str, Any], track: dict[str, Any], catalog_by_id: dict[str, dict[str, str]]) -> None:
    if result.get("track_id") != track["track_id"] or result.get("track_name") != track["name"]:
        raise ValueError("track identity mismatch")
    all_ids: list[str] = []
    for opportunity in result.get("product_opportunities", []):
        ids = opportunity.get("catalog_item_ids", [])
        if not ids:
            raise ValueError("opportunity has no catalog items")
        all_ids.extend(ids)
    all_ids.extend(result.get("non_recommended_catalog_item_ids", []))
    unknown = sorted(set(all_ids) - set(catalog_by_id))
    if unknown:
        raise ValueError(f"unknown catalog IDs: {unknown}")


def enrich_result(result: dict[str, Any], catalog_by_id: dict[str, dict[str, str]]) -> dict[str, Any]:
    for opportunity in result["product_opportunities"]:
        opportunity["catalog_items"] = [catalog_by_id[item_id] for item_id in opportunity["catalog_item_ids"]]
    return result


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


async def map_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    track: dict[str, Any],
    catalog: list[dict[str, str]],
    catalog_by_id: dict[str, dict[str, str]],
    schema: dict[str, Any],
    model: str,
    raw_dir: Path,
    force: bool,
    retries: int,
) -> tuple[str, bool, str]:
    output_path = raw_dir / f"{track['track_id']}.json"
    if output_path.exists() and not force:
        try:
            cached = json.loads(output_path.read_text(encoding="utf-8"))
            validate_result(cached["result"], track, catalog_by_id)
            return track["track_id"], True, "cached"
        except (ValueError, KeyError, json.JSONDecodeError):
            pass

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(track, catalog)},
        ],
        "response_format": {"type": "json_object"},
        "enable_thinking": False,
        "max_completion_tokens": 12000,
    }
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            async with semaphore:
                response = await client.post("/chat/completions", json=body)
            if response.status_code in {408, 409, 429, 500, 502, 503, 504}:
                raise httpx.HTTPStatusError(
                    f"retryable HTTP {response.status_code}: {response.text[:1000]}",
                    request=response.request,
                    response=response,
                )
            if response.is_error:
                raise ValueError(f"HTTP {response.status_code}: {response.text[:2000]}")
            api_payload = response.json()
            result = extract_json_content(api_payload)
            validate_result(result, track, catalog_by_id)
            result = enrich_result(result, catalog_by_id)
            record = {
                "track_id": track["track_id"], "track_name": track["name"], "model": model,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "usage": api_payload.get("usage", {}), "result": result,
            }
            output_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return track["track_id"], True, f"generated attempt={attempt}"
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                await asyncio.sleep(min(30.0, 1.5 * (2 ** (attempt - 1)) + random.random()))
    return track["track_id"], False, last_error


def aggregate(
    tracks: list[dict[str, Any]], raw_dir: Path, output_dir: Path, catalog: list[dict[str, str]],
    model: str, concurrency: int,
) -> None:
    records = []
    for track in tracks:
        path = raw_dir / f"{track['track_id']}.json"
        if not path.exists():
            continue
        records.append(json.loads(path.read_text(encoding="utf-8")))
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "model": model, "track_count": len(tracks), "completed_count": len(records),
        "product_catalog_item_count": len(catalog), "concurrency": concurrency,
    }
    (output_dir / "product_catalog_snapshot.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "smb_product_mappings.json").write_text(
        json.dumps({"metadata": metadata, "mappings": records}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (output_dir / "smb_product_mappings.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    fields = ("track_id", "track_name", "opportunity_name", "relationship", "priority", "confidence", "catalog_items", "fit_reason")
    with (output_dir / "smb_product_opportunities.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            result = record["result"]
            for item in result["product_opportunities"]:
                writer.writerow({
                    "track_id": result["track_id"], "track_name": result["track_name"],
                    "opportunity_name": item["opportunity_name"], "relationship": item["relationship"],
                    "priority": item["priority"], "confidence": item["confidence"],
                    "catalog_items": "；".join(x["product_service"] for x in item["catalog_items"]),
                    "fit_reason": item["fit_reason"],
                })


async def async_main(args: argparse.Namespace) -> int:
    load_env(args.env)
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    model = os.getenv("DASHSCOPE_MODEL", "deepseek-v4.1-flash").strip()
    tracks = load_tracks(args.tracks)
    catalog = parse_markdown_catalog(args.catalog)
    catalog_by_id = {item["catalog_item_id"]: item for item in catalog}
    schema = output_schema(list(catalog_by_id))
    if args.limit:
        tracks = tracks[: args.limit]
    args.raw_dir.mkdir(parents=True, exist_ok=True)

    if args.print_prompt:
        print(SYSTEM_PROMPT)
        print(build_user_prompt(tracks[0], catalog))
        return 0
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is missing from environment or .env")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(args.timeout, connect=30.0)
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    semaphore = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout, limits=limits) as client:
        tasks = [
            asyncio.create_task(map_one(
                client, semaphore, track, catalog, catalog_by_id, schema, model,
                args.raw_dir, args.force, args.retries,
            ))
            for track in tracks
        ]
        failures = []
        completed = 0
        for future in asyncio.as_completed(tasks):
            track_id, ok, detail = await future
            completed += 1
            print(f"[{completed}/{len(tasks)}] {'OK' if ok else 'FAIL'} {track_id} {detail}", flush=True)
            if not ok:
                failures.append((track_id, detail))

    aggregate(tracks, args.raw_dir, args.output_dir, catalog, model, args.concurrency)
    if failures:
        (args.output_dir / "failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{len(failures)} track(s) failed; rerun to resume", file=sys.stderr)
        return 1
    failure_path = args.output_dir / "failures.json"
    if failure_path.exists():
        failure_path.unlink()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracks", type=Path, default=Path("data/processed/tracks_curated/tracks_classification_ready.jsonl"))
    parser.add_argument("--catalog", type=Path, default=Path("data/raw/联想SMB产品服务列表.md"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--raw-dir", type=Path, default=Path("outputs/smb_product_remapping/raw"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/smb_product_remapping"))
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--print-prompt", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 32:
        parser.error("--concurrency must be between 1 and 32")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main(parse_args())))
