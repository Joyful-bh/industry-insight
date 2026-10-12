"""Replay candidate-boundary repair without changing the formal pipeline."""
# ruff: noqa: E501

import json
import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from experiments.track_quality.model_capacity_diagnostic import PROMPT, SCOPES, Market, pairs
from scripts.event_stage_supervisor import client, save
from track_insight.core.fingerprints import fingerprint
from track_insight.settings import load_poc_config
from track_insight.tracks.contracts import SupplierRole

ROOT = Path("data/track_quality/candidate_interface_pilot/run_2")
REPAIR = """修正候选交给最终归并之前的产品范围。候选旧名称、旧定义不是事实，仅根据events摘要。
每个candidate_ref输出一个结果。units是边界基本一致的制造业产品供给方向；同一产品的型号、技术路线、材料等级和应用差异不拆。只在主要交付物或直接生产工序不同、且来源明确时拆分，不枚举原子型号或产业链所有产品。
例如一个材料同时涉及整机及零部件，不能用产业链总称包装成一个互斥产品市场。宽泛产业政策、基地和园区材料作为background_event_refs，不扩写产品目录；具体产品产业化规划及生产线可以建立units。使用软件不建立软件供应商身份。
product_scope只写共同产品类别，不写地区、热度、政策动作或工序流程清单。身份以该主体对外提供的产品/直接生产工序为准，不把配套自用设备当交付物。
units通常0–2个，必要时按真正不同交付物增加；不以数量上限删有效方向。每个unit只输出product_scope、supplier_role和anchors。anchors只选择输入facts中的fact_ref，程序回填对应原文，不要复制或改写原文。一个事件明确多个产品时可以支持不同unit，但各unit需引用支持其产品的片段编号。
没有清楚身份的事件放background_event_refs，表示未作身份核心，不表示无关或删除。每个输入事件至少出现在一个anchor或background_event_refs，不能静默丢弃。相同产品和角色在该候选内归为一个unit。
只输出紧凑JSON {"candidates":[{"candidate_ref":"C1","units":[{"product_scope":"...","supplier_role":"MANUFACTURE_PRODUCT","anchors":["E...S0"]}],"background_event_refs":[]}]}。
角色限DESIGN_PRODUCT、MANUFACTURE_PRODUCT、MANUFACTURING_PROCESS_SERVICE、INDUSTRIAL_SOFTWARE。不要输出解释或最终赛道定义。"""


def event_facts(event):
    if "facts" in event:
        return event["facts"]
    return [
        dict(fact_ref=f"{event['event_ref']}S{i}", text=text)
        for i, text in enumerate(re.split(r"(?<=[。；])", event["summary"]))
        if text.strip()
    ]


class Unit(BaseModel):
    product_scope: str = Field(min_length=2)
    supplier_role: SupplierRole
    anchors: list[str] = Field(min_length=1)


class Candidate(BaseModel):
    candidate_ref: str
    units: list[Unit]
    background_event_refs: list[str]


class RepairOutput(BaseModel):
    candidates: list[Candidate]


def repair_schema(rows):
    allowed = {
        r["candidate_ref"]: {e["event_ref"]: e["summary"] for e in r["events"]} for r in rows
    }

    @model_validator(mode="after")
    def validate_sources(self):
        refs = [c.candidate_ref for c in self.candidates]
        if len(refs) != len(set(refs)) or set(refs) != set(allowed):
            raise ValueError("Exactly one record per source candidate required")
        for c in self.candidates:
            sources = allowed[c.candidate_ref]
            used = set()
            for u in c.units:
                for a in u.anchors:
                    fact_events = {
                        f["fact_ref"]: e["event_ref"]
                        for row in rows
                        if row["candidate_ref"] == c.candidate_ref
                        for e in row["events"]
                        for f in event_facts(e)
                    }
                    if a not in fact_events:
                        raise ValueError("Anchor must reference its own source fact")
                    used.add(fact_events[a])
            # Unselected facts are retained, not interpreted as semantically irrelevant.
            c.background_event_refs = sorted(set(sources) - used)
        return self

    return type("RepairOutput", (RepairOutput,), {"validate_sources": validate_sources})


def market_schema(refs):
    class MarketOutput(BaseModel):
        markets: list[Market]
        pending: list[str]

        @model_validator(mode="after")
        def coverage(self):
            returned = [r for m in self.markets for r in m.members] + self.pending
            if len(returned) != len(set(returned)) or set(returned) != set(refs):
                raise ValueError(
                    f"Each repaired unit needs exactly one destination; "
                    f"missing={sorted(set(refs) - set(returned))}; "
                    f"unknown={sorted(set(returned) - set(refs))}; "
                    f"duplicates={sorted({r for r in returned if returned.count(r) > 1})}"
                )
            return self

    return MarketOutput


def main():
    config = load_poc_config(Path("config/national.yaml"))
    config.bailian.max_retries = 1
    source = json.loads(
        Path("data/track_quality/set_boundary_pilot/run_1/inputs.json").read_text("utf-8")
    )
    rows = [c for c in source["candidates"] if int(c["candidate_ref"][1:]) in SCOPES]
    rows = [
        dict(
            candidate_ref=c["candidate_ref"],
            events=[
                dict(event_ref=e["event_ref"], summary=e["summary"], facts=event_facts(e))
                for e in c["events"]
            ],
        )
        for c in rows
    ]
    facts = {
        f["fact_ref"]: dict(event_ref=e["event_ref"], text=f["text"])
        for c in rows
        for e in c["events"]
        for f in e["facts"]
    }
    rows = [
        dict(
            candidate_ref=c["candidate_ref"],
            events=[
                dict(event_ref=e["event_ref"], facts=e["facts"], summary=e["summary"])
                for e in c["events"]
            ],
        )
        for c in rows
    ]
    state = dict(status="repairing", started_at=datetime.now(UTC).isoformat(), calls={})
    save(
        ROOT / "inputs.json",
        dict(
            model=config.stage4.model,
            candidates=rows,
            baseline="data/track_quality/model_capacity_diagnostic/run_1",
            repair_prompt=REPAIR,
            market_prompt=PROMPT,
        ),
    )
    save(ROOT / "status.json", state)

    def call(task, payload, prompt, schema):
        signature = fingerprint(config.stage4.model, payload, prompt, schema.model_json_schema())
        path = ROOT / "calls" / f"{task}.json"
        if path.exists():
            cached = json.loads(path.read_text("utf-8"))
            if cached.get("signature") == signature:
                state["calls"][task] = "cached"
                return schema.model_validate(cached["output"])
        save(ROOT / "calls" / f"{task}.input.json", dict(prompt=prompt, payload=payload))
        active = client(config)
        try:
            result = active.complete_structured(
                messages=[
                    dict(role="system", content=prompt),
                    dict(role="user", content=json.dumps(payload, ensure_ascii=False)),
                ],
                output_model=schema,
                model=config.stage4.model,
                max_tokens=6144,
                enable_thinking=False,
            )
        finally:
            active.close()
        save(
            path,
            dict(
                signature=signature,
                output=result.output.model_dump(mode="json"),
                usage=result.usage,
                call_count=result.call_count,
            ),
        )
        state["calls"][task] = "completed"
        print(f"{task}: completed", flush=True)
        return result.output

    try:
        repaired = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(
                    call,
                    f"repair_{start}",
                    rows[start : start + 8],
                    REPAIR,
                    repair_schema(rows[start : start + 8]),
                )
                for start in range(0, len(rows), 8)
            ]
            for future in as_completed(futures):
                repaired.extend(future.result().candidates)
                save(ROOT / "status.json", state)
        repaired.sort(key=lambda c: int(c.candidate_ref[1:]))
        save(ROOT / "repaired_candidates.json", [c.model_dump(mode="json") for c in repaired])
        cards, mapping = [], {}
        for c in repaired:
            for i, u in enumerate(c.units):
                ref = f"{c.candidate_ref}U{i}"
                mapping[ref] = dict(
                    source_candidate=c.candidate_ref, anchors=[facts[a] for a in u.anchors]
                )
                cards.append(
                    dict(
                        candidate_ref=ref,
                        product_scope=u.product_scope,
                        supplier_role_hint=u.supplier_role,
                        source_facts=[facts[a]["text"] for a in u.anchors],
                    )
                )
        save(ROOT / "market_inputs.json", cards)
        save(ROOT / "source_mapping.json", mapping)
        state["status"] = "merging"
        save(ROOT / "status.json", state)
        shuffled = list(cards)
        random.Random(20261013).shuffle(shuffled)
        schema = market_schema(mapping)
        results = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(call, f"market_{arm}", data, PROMPT, schema): arm
                for arm, data in [("normal", cards), ("shuffled", shuffled)]
            }
            for future in as_completed(futures):
                arm = futures[future]
                results[arm] = future.result()
                save(ROOT / f"tracks_{arm}.json", results[arm].model_dump(mode="json"))
        left, right = pairs(results["normal"]), pairs(results["shuffled"])
        responses = [
            json.loads(p.read_text("utf-8"))
            for p in (ROOT / "calls").glob("*.json")
            if not p.name.endswith(".input.json")
        ]
        metrics = dict(
            input_candidates=len(rows),
            repaired_units=len(cards),
            background_only_candidates=[c.candidate_ref for c in repaired if not c.units],
            tracks={a: len(r.markets) for a, r in results.items()},
            pending={a: r.pending for a, r in results.items()},
            merge_pairs={a: sorted(pairs(r)) for a, r in results.items()},
            merge_pair_jaccard=len(left & right) / len(left | right) if left | right else 1,
            input_tokens=sum(r["usage"].get("prompt_tokens", 0) for r in responses),
            output_tokens=sum(r["usage"].get("completion_tokens", 0) for r in responses),
            calls=sum(r["call_count"] for r in responses),
            database_modified=False,
            production_modified=False,
        )
        save(ROOT / "metrics.json", metrics)
        state.update(status="completed", metrics=metrics)
        save(ROOT / "status.json", state)
    except Exception as error:
        state.update(status="failed", error=str(error))
        save(ROOT / "status.json", state)
        raise


if __name__ == "__main__":
    main()
