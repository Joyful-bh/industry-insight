"""Source-backed set partition replay; no production or database writes."""
# ruff: noqa: E501

import argparse
import json
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from experiments.track_quality.object_first_pilot import Definitions, checked_schema
from scripts.event_stage_supervisor import client, save
from track_insight.core.fingerprints import fingerprint
from track_insight.settings import load_poc_config
from track_insight.tracks.contracts import SupplierRole

PARTITION = """整理本轮制造业候选集合的最终市场边界，服务联想SMB的略粗粒度客户覆盖。遵守scope。
候选的名称、offering_family、definition是待核实的模型概括；身份及产品以events原文事实为准。
先统一这整个集合的划分层级，再形成完整划分。不要逐对作互不一致的判断。
同一产品市场的型号、等级、材料体系、技术路线、代际和应用变体，尽量归为共同市场。
不同主要交付物、不同直接生产工序、上下游或整机与零部件不能只因同产业链合并；也不能仅因为都用PC服务器合并。
对于宽窄重叠，检查宽候选来源：若只是已有具体产品的笼统名称，吸收并消除父子重复；若确有额外对象，按原文明示的共同产品边界缩窄、独立保留或重分成员。不要把宽候选整个吞进不涵盖其实际产品的窄市场。
原文明示产品的研发、生产线规划或产业化扶持也可定义候选，不要求已量产。泛产业基地、平台建设、内部使用改造没有明确供给产品时放pending，而不是凭常识补目录。
使用软件设备不证明供应软件设备。可依据来源修正名称、产品边界和supplier_role，但不得把修正建立在生成定义上。
supplier_role仅取DESIGN_PRODUCT、MANUFACTURE_PRODUCT、MANUFACTURING_PROCESS_SERVICE、INDUSTRIAL_SOFTWARE；同市场角色一致。
每个候选编号必须出现在某个市场members或pending。市场members非空；独立有效方向也作为单成员市场输出，不强求少量赛道。
core_event_refs选择直接支持市场产品及供给角色的输入event_ref。通常引用1–3个，但覆盖成员的确切范围比控制数量更重要。
同一候选若来源明确不同供给对象，可跨市场拆分，但各市场引用该候选的身份事件必须互不重叠。不能仅靠拆定义创造产品。
只输出紧凑JSON {"markets":[{"name":"共同产品市场及角色","supplier_role":"MANUFACTURE_PRODUCT","members":["C0"],"core_event_refs":["E..."]}],"pending":["C1"]}。
不输出定义、评分、解释或额外字段。任何成员和事件只能引用输入短编号，不能一边归属一边pending。"""

AUDIT = """审核制造业最终市场划分与来源事实。候选生成的定义不是事实。
逐个市场判断KEEP或REJECT。KEEP要求名称和角色有直接依据、完整覆盖已选事件支持的产品，且没有拼接不同交付物、上下游或不同工序。
同产品市场的技术、等级、代际和应用变体可以合并；原文明示产品的产业化扶持和生产线规划也可支持候选。
使用/部署不能证明供应；泛基地政策不能补出产品目录。
还需整体检查父子重叠；只有各市场来源真的指向不同对象时才同时保留宽窄方向。不为减少数量而丢弃独立有效方向。
每个track_ref恰好输出一次，只输出 {"decisions":[{"track_ref":"R0","decision":"KEEP"}]}。"""

DEFINE = """为已通过来源审核的制造业客户市场写约60–120字定义。名称与成员边界已确定，不重新归并。
输入legacy_definition不是事实，只根据已选events说明产品、供给角色和必要的相邻边界。
不要补未证明的技术能力、自有产线、具体用途或产品目录；规划项目不能写成已量产事实。
每个track_ref恰好输出一次，只输出 {"tracks":[{"track_ref":"R0","name":"保持输入名称","definition":"..."}]}。"""  # noqa: E501


class Market(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    supplier_role: SupplierRole
    members: list[str] = Field(min_length=1)
    core_event_refs: list[str] = Field(min_length=1)


class Partition(BaseModel):
    markets: list[Market]
    pending: list[str] = Field(default_factory=list)


class AuditDecision(BaseModel):
    track_ref: str
    decision: str = Field(pattern="^(KEEP|REJECT)$")


class AuditOutput(BaseModel):
    decisions: list[AuditDecision]


def partition_schema(candidates):
    allowed = {c["candidate_ref"]: {e["event_ref"] for e in c["events"]} for c in candidates}

    @model_validator(mode="before")
    @classmethod
    def normalize_pending(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            assigned = {ref for row in value.get("markets", []) for ref in row.get("members", [])}
            value["pending"] = sorted(set(value.get("pending", [])) - assigned)
        return value

    @model_validator(mode="after")
    def verify(self):
        seen = {}
        for market in self.markets:
            if len(market.members) != len(set(market.members)) or not set(market.members) <= set(
                allowed
            ):
                raise ValueError("Members must be distinct existing candidates")
            events = set(market.core_event_refs)
            if not events <= set().union(*(allowed[ref] for ref in market.members)):
                raise ValueError("Identity events must come from market members")
            for ref in market.members:
                identity = events & allowed[ref]
                if not identity:
                    raise ValueError("Every core member needs an identity event")
                if seen.get(ref, set()) & identity:
                    raise ValueError("Splitting a candidate requires disjoint identity events")
                seen[ref] = seen.get(ref, set()) | identity
        returned = set(seen) | set(self.pending)
        if returned != set(allowed):
            raise ValueError(
                f"Account for all input candidates; missing={sorted(set(allowed) - returned)}, "
                f"unknown={sorted(returned - set(allowed))}"
            )
        return self

    return type(
        "Partition", (Partition,), {"normalize_pending": normalize_pending, "verify": verify}
    )


def merge_pairs(partition):
    return {tuple(sorted(pair)) for market in partition for pair in combinations(market.members, 2)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-first", action="store_true")
    args = parser.parse_args()
    root = args.output
    config = load_poc_config(Path("config/national.yaml"))
    state = dict(status="preparing", started_at=datetime.now(UTC).isoformat(), calls={})
    save(root / "status.json", state)
    try:
        local = json.loads((args.baseline / "local_tracks.json").read_text(encoding="utf-8"))
        observations = json.loads(
            (args.baseline / "compact_inputs.json").read_text(encoding="utf-8")
        )
        inputs = json.loads((args.baseline / "inputs.json").read_text(encoding="utf-8"))
        scope = json.loads(
            (args.baseline / "calls/generation_0.input.json").read_text(encoding="utf-8")
        )["payload"]["scope"]
        facts = {e["event_ref"]: e for o in observations for e in o["representative_events"]}
        reverse = {t["id"]: f"T{i}" for i, t in enumerate(inputs["topics"], 1)}
        topic_events = {
            reverse[tid]: o["representative_events"]
            for o in observations
            for tid in o["original_topic_ids"]
        }
        candidates = []
        for i, track in enumerate(local):
            available = {e["event_ref"]: e for ref in track["refs"] for e in topic_events[ref]}
            identity_ids = set(track["identity_event_ids"])
            ordered = sorted(
                available.values(),
                key=lambda e: (e["event_id"] not in identity_ids, e["event_ref"]),
            )
            chosen = ordered[:4]
            candidates.append(
                dict(
                    candidate_ref=f"C{i}",
                    name=track["name"],
                    supplier_role=track["supplier_role"],
                    offering_family=track["offering_family"],
                    legacy_definition=track["definition"],
                    events=[
                        dict(
                            event_ref=e["event_ref"],
                            title=e["title"],
                            summary=e["summary"],
                            quote=e["quote"][:300],
                        )
                        for e in chosen
                    ],
                )
            )
        save(
            root / "inputs.json",
            dict(
                model=config.stage4.model,
                scope=scope,
                baseline=str(args.baseline),
                candidates=candidates,
                input_topics=len(inputs["topics"]),
                database_modified=False,
            ),
        )
        if len(candidates) > 120:
            raise ValueError("Bounded pilot only accepts at most 120 candidates")
        if args.source_first:
            candidates = [
                dict(
                    candidate_ref=c["candidate_ref"],
                    offering_family_hint=c["offering_family"],
                    supplier_role_hint=c["supplier_role"],
                    events=c["events"],
                )
                for c in candidates
            ]
            save(root / "source_first_inputs.json", candidates)

        def call(task, payload, prompt, schema, max_tokens=8192):
            signature = fingerprint(
                config.stage4.model, prompt, payload, schema.model_json_schema()
            )
            path = root / "calls" / f"{task}.json"
            if path.exists():
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached["signature"] == signature:
                    state["calls"][task] = "cached"
                    return schema.model_validate(cached["output"])
            save(root / "calls" / f"{task}.input.json", dict(prompt=prompt, payload=payload))
            active = client(config)
            try:
                result = active.complete_structured(
                    messages=[
                        dict(role="system", content=prompt),
                        dict(role="user", content=json.dumps(payload, ensure_ascii=False)),
                    ],
                    output_model=schema,
                    model=config.stage4.model,
                    max_tokens=max_tokens,
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
            state["updated_at"] = datetime.now(UTC).isoformat()
            print(f"{task}: completed", flush=True)
            return result.output

        arms = {"normal": candidates, "shuffled": list(candidates)}
        random.Random(20261012).shuffle(arms["shuffled"])
        state["status"] = "partitioning"
        save(root / "status.json", state)
        schema = partition_schema(candidates)
        partitions = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = {
                pool.submit(
                    call, f"partition_{arm}", dict(scope=scope, candidates=rows), PARTITION, schema
                ): arm
                for arm, rows in arms.items()
            }
            for future in as_completed(pending):
                arm = pending[future]
                partitions[arm] = future.result()
                save(root / f"{arm}_partition.json", partitions[arm].model_dump())
                save(root / "status.json", state)

        def market_payload(market, ref):
            return dict(
                track_ref=ref,
                name=market.name,
                supplier_role=market.supplier_role,
                members=market.members,
                events=[
                    dict(
                        event_ref=e,
                        title=facts[e]["title"],
                        summary=facts[e]["summary"],
                        quote=facts[e]["quote"][:300],
                    )
                    for e in market.core_event_refs
                ],
            )

        state["status"] = "auditing"
        save(root / "status.json", state)
        kept = {}
        for arm, partition in partitions.items():
            payloads = [market_payload(m, f"R{i}") for i, m in enumerate(partition.markets)]
            audit = call(
                f"audit_{arm}",
                dict(scope=scope, markets=payloads),
                AUDIT,
                checked_schema(
                    AuditOutput, "decisions", "track_ref", {p["track_ref"] for p in payloads}
                ),
                4096,
            )
            accepted = {d.track_ref for d in audit.decisions if d.decision == "KEEP"}
            kept[arm] = [m for i, m in enumerate(partition.markets) if f"R{i}" in accepted]
            save(
                root / f"{arm}_rejected.json",
                [
                    m.model_dump()
                    for i, m in enumerate(partition.markets)
                    if f"R{i}" not in accepted
                ],
            )
        state["status"] = "defining"
        save(root / "status.json", state)
        final = kept["normal"]
        definitions = {}

        def define(start):
            payload = [
                market_payload(m, f"R{i}") for i, m in enumerate(final[start : start + 8], start)
            ]
            return call(
                f"definition_{start}",
                dict(scope=scope, markets=payload),
                DEFINE,
                checked_schema(
                    Definitions, "tracks", "track_ref", {p["track_ref"] for p in payload}
                ),
                4096,
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            for result in pool.map(define, range(0, len(final), 8)):
                definitions.update({t.track_ref: t for t in result.tracks})
        tracks = []
        for i, market in enumerate(final):
            sources = [local[int(ref[1:])] for ref in market.members]
            tracks.append(
                dict(
                    name=market.name,
                    definition=definitions[f"R{i}"].definition,
                    supplier_role=market.supplier_role,
                    members=market.members,
                    refs=sorted({r for t in sources for r in t["refs"]}),
                    supporting_refs=sorted(
                        {r for t in sources for r in t["supporting_refs"]}
                        - {r for t in sources for r in t["refs"]}
                    ),
                    identity_event_ids=[facts[e]["event_id"] for e in market.core_event_refs],
                    evidence=market_payload(market, f"R{i}")["events"],
                )
            )
        save(root / "tracks.json", tracks)
        left, right = merge_pairs(kept["normal"]), merge_pairs(kept["shuffled"])
        responses = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in (root / "calls").glob("*.json")
            if not p.name.endswith(".input.json")
        ]
        metrics = dict(
            input_variant="source_first" if args.source_first else "legacy_context",
            input_topics=len(inputs["topics"]),
            input_candidates=len(local),
            proposed_tracks={arm: len(p.markets) for arm, p in partitions.items()},
            pending_candidates={arm: p.pending for arm, p in partitions.items()},
            audited_tracks={arm: len(rows) for arm, rows in kept.items()},
            represented_candidates={
                arm: len({ref for m in rows for ref in m.members}) for arm, rows in kept.items()
            },
            unrepresented_candidates={
                arm: sorted(
                    {c["candidate_ref"] for c in candidates}
                    - {ref for m in rows for ref in m.members}
                )
                for arm, rows in kept.items()
            },
            merge_pair_jaccard=len(left & right) / len(left | right) if left | right else 1,
            normal_merge_pairs=len(left),
            shuffled_merge_pairs=len(right),
            input_tokens=sum(r["usage"].get("prompt_tokens", 0) for r in responses),
            output_tokens=sum(r["usage"].get("completion_tokens", 0) for r in responses),
            api_calls=sum(r["call_count"] for r in responses),
            usage_scope="saved_successful_responses_only; failed_requests_not_included",
            database_modified=False,
            production_configuration_modified=False,
        )
        save(root / "metrics.json", metrics)
        state.update(status="completed", metrics=metrics)
        save(root / "status.json", state)
        print(json.dumps(metrics, ensure_ascii=False), flush=True)
    except Exception as error:
        state.update(status="failed", error=f"{type(error).__name__}: {error}")
        save(root / "status.json", state)
        raise


if __name__ == "__main__":
    main()
