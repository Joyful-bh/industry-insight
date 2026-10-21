"""Frozen-input experiment; never writes production configuration or database snapshots."""

import argparse
import json
import random
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field, model_validator

from scripts.event_stage_supervisor import client, save
from track_insight.core.fingerprints import fingerprint
from track_insight.settings import load_poc_config
from track_insight.tracks.contracts import SupplierRole
from track_insight.tracks.preparation import Observation, cached_embeddings

EXTRACT = """识别来源明确支持的制造业供给对象，不生成赛道名称或定义。遵守scope。
只判断原文中的主体实际生产/供应什么，或者政策明确扶持什么产品的研发和产业化。
object仅写原文支持的产品或直接生产工序，不写技术应用、项目建设等动作。
使用/采购设备或软件、内部工厂改造不证明对外供应这些设备或软件；如果原文明示其制造产品，输出该产品。
仅有产业、基地、平台、数字化等泛称而无明确产品时，不补出具体产品。
不要求已经量产，明确产品的生产线规划也可以。种养殖、采矿、设施运营不属于制造。
role只能取DESIGN_PRODUCT(设计并供应芯片/IP等)、MANUFACTURE_PRODUCT(制造实体产品材料设备)、MANUFACTURING_PROCESS_SERVICE(代工/封测/CDMO等直接生产工序)、INDUSTRIAL_SOFTWARE(对外供应工业研发生产控制软件)。
同一来源并列不同产品时，可分别提取明确对象，但不凭常识拆出技术路线、上下游产品。相同对象角色可合并引用。
每项仅object、role、event_refs；event_refs引用直接支持产品和角色的输入短编号。无法确定的事件不输出对象，程序保留这些材料。仅输出JSON {"objects":[]}。"""  # noqa: E501

RELATE = """比较制造业供给对象，判断联想SMB略粗粒度客户市场关系。依据对象和原始事件，不补外部事实。
每个pair_ref恰好输出一次，relation仅取SAME_MARKET、LEFT_CONTAINS、RIGHT_CONTAINS、DISTINCT、UNCERTAIN。
SAME_MARKET：同一主要交付物或相同直接生产工序中的技术路线、等级、代际、材料体系或应用变体，可共同形成清晰客户市场。
LEFT_CONTAINS/RIGHT_CONTAINS：一方明显覆盖另一方，但还覆盖其他交付物；不能把包含关系当同义。
DISTINCT：不同交付物、不同直接生产工序、整机与部件、上下游或仅配套使用，或供给角色不同。
仅因都使用PC服务器、同属大行业或同供应链不能判SAME_MARKET。不能确定时UNCERTAIN。
只输出JSON {"decisions":[{"pair_ref":"P0","relation":"SAME_MARKET"}]}，不写定义或解释。"""

AUDIT = """审核同一制造业客户市场的对象组，依据给定原始事件。每个group_ref恰好输出一次。
只在所有成员都是同一主要交付物或同一生产工序的横向变体时ACCEPT。
存在宽窄包含、不同交付物、不同生产工序、上下游、整机与部件或角色冲突时REJECT。
不要因为相邻两对可合并就默认全组可合并。拒绝组的成员由程序原样保留。
只输出JSON {"decisions":[{"group_ref":"G0","decision":"ACCEPT"}]}。"""

DEFINE = """根据已确定成员的供给对象和原始事件，为制造业客户市场命名并写约60–120字定义。
成员已经确定，不得增删或重新归并。名称概括全体成员的共同产品或生产工序，不用地区、项目、技术配置命名。
定义只说明实际支持的产品、供给角色及必要排除边界，不补原文没有的产能、技术能力、用途或自有产线。
输入项目可能仍是规划，不能写成已经量产。不要沿着宽名称扩写产品目录。
每个track_ref恰好输出一次，只输出JSON {"tracks":[{"track_ref":"R0","name":"...","definition":"..."}]}。"""  # noqa: E501


class SupplyObject(BaseModel):
    object: str = Field(min_length=1, max_length=100)
    role: SupplierRole
    event_refs: list[str] = Field(min_length=1)


class ObjectOutput(BaseModel):
    objects: list[SupplyObject] = Field(default_factory=list)


class Relation(BaseModel):
    pair_ref: str
    relation: Literal["SAME_MARKET", "LEFT_CONTAINS", "RIGHT_CONTAINS", "DISTINCT", "UNCERTAIN"]


class Relations(BaseModel):
    decisions: list[Relation]


class AuditItem(BaseModel):
    group_ref: str
    decision: Literal["ACCEPT", "REJECT"]


class Audits(BaseModel):
    decisions: list[AuditItem]


class DefinedMarket(BaseModel):
    track_ref: str
    name: str = Field(min_length=2, max_length=100)
    definition: str = Field(min_length=20, max_length=600)


class Definitions(BaseModel):
    tracks: list[DefinedMarket]


def checked_schema(base, field, key, expected):
    @model_validator(mode="after")
    def check(self):
        refs = [getattr(row, key) for row in getattr(self, field)]
        if len(refs) != len(set(refs)) or set(refs) != set(expected):
            raise ValueError("Output must cover exactly the supplied references")
        return self

    return type(base.__name__, (base,), {"check": check})


def extraction_schema(expected):
    @model_validator(mode="after")
    def check(self):
        for item in self.objects:
            if not set(item.event_refs) <= set(expected):
                raise ValueError("Identity evidence must come from supplied events")
        return self

    return type("ObjectOutput", (ObjectOutput,), {"check": check})


def combine_objects(rows):
    groups = {}
    for row in rows:
        key = ("".join(row.object.split()).casefold(), row.role)
        if key not in groups:
            groups[key] = row.model_copy(deep=True)
        else:
            groups[key].event_refs = sorted(set(groups[key].event_refs + row.event_refs))
    return sorted(groups.values(), key=lambda r: (r.role, r.object))


def neighbor_pairs(rows, vectors, neighbors=5, threshold=0.55):
    matrix = np.asarray(vectors, dtype=float)
    matrix /= np.maximum(np.linalg.norm(matrix, axis=1)[:, None], 1e-12)
    similarity = matrix @ matrix.T
    pairs = set()
    for i, row in enumerate(rows):
        nearest = sorted(
            (j for j, other in enumerate(rows) if i != j and row.role == other.role),
            key=lambda j: (-similarity[i, j], j),
        )[:neighbors]
        for j in nearest:
            if similarity[i, j] >= threshold:
                pairs.add(tuple(sorted((i, j))))
    return sorted(pairs)


def components(count, pairs, decisions):
    parent = list(range(count))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for ref, (a, b) in pairs.items():
        if decisions[ref] == "SAME_MARKET":
            parent[find(b)] = find(a)
    result = defaultdict(list)
    for i in range(count):
        result[find(i)].append(i)
    return sorted(result.values(), key=lambda members: members[0])


def merged_pairs(partition):
    return {tuple(sorted(pair)) for group in partition for pair in combinations(group, 2)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = load_poc_config(Path("config/national.yaml"))
    root = args.output
    state = dict(status="starting", started_at=datetime.now(UTC).isoformat(), calls={})
    save(root / "status.json", state)
    try:
        inputs = json.loads((args.baseline / "inputs.json").read_text(encoding="utf-8"))
        observations = json.loads(
            (args.baseline / "compact_inputs.json").read_text(encoding="utf-8")
        )
        groups = json.loads((args.baseline / "groups.json").read_text(encoding="utf-8"))
        scope = json.loads(
            (args.baseline / "calls/generation_0.input.json").read_text(encoding="utf-8")
        )["payload"]["scope"]
        lookup = {o["topic_ref"]: o for o in observations}
        facts = {e["event_ref"]: e for o in observations for e in o["representative_events"]}
        event_topics = defaultdict(set)
        reverse = {t["id"]: f"T{i}" for i, t in enumerate(inputs["topics"], 1)}
        for observation in observations:
            for event in observation["representative_events"]:
                event_topics[event["event_ref"]].update(
                    reverse[t] for t in observation["original_topic_ids"]
                )

        def evidence(refs):
            return [
                dict(
                    event_ref=r,
                    title=facts[r]["title"],
                    summary=facts[r]["summary"],
                    quote=facts[r]["quote"],
                )
                for r in refs
            ]

        def call(task, payload, prompt, schema):
            signature = fingerprint(config.stage4.model, prompt, payload)
            path = root / "calls" / f"{task}.json"
            if path.exists():
                old = json.loads(path.read_text(encoding="utf-8"))
                if old["signature"] == signature or old["signature"] == fingerprint(
                    config.stage4.model, prompt, payload, schema.model_json_schema()
                ):
                    return schema.model_validate(old["output"])
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
                    max_tokens=4096,
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
            return result.output

        def parallel(tasks, worker):
            outputs = {}
            with ThreadPoolExecutor(max_workers=4) as pool:
                pending = {pool.submit(worker, task): task for task in tasks}
                for future in as_completed(pending):
                    task = pending[future]
                    outputs[task] = future.result()
                    state["calls"][f"{state['status']}_{task}"] = "completed"
                    state["updated_at"] = datetime.now(UTC).isoformat()
                    save(root / "status.json", state)
                    print(f"{state['status']} {task}: completed", flush=True)
            return outputs

        state["status"] = "extracting_objects"

        def extract(index):
            refs = sorted(
                {
                    e["event_ref"]
                    for ref in groups[index]
                    for e in lookup[ref]["representative_events"]
                }
            )
            return call(
                f"objects_{index}",
                dict(scope=scope, events=evidence(refs)),
                EXTRACT,
                extraction_schema(refs),
            )

        outputs = parallel(range(len(groups)), extract)
        rows = combine_objects([row for i in sorted(outputs) for row in outputs[i].objects])
        save(
            root / "objects.json",
            [
                dict(
                    object_ref=f"C{i}",
                    **r.model_dump(),
                    topic_refs=sorted(set().union(*(event_topics[e] for e in r.event_refs))),
                )
                for i, r in enumerate(rows)
            ],
        )
        if not rows:
            raise ValueError("No supply objects extracted")
        active = client(config)
        try:
            vectors, embedding_usage = cached_embeddings(
                [
                    Observation(
                        str(i), [], set(), set(), set(), dict(label=r.object, definition=r.role)
                    )
                    for i, r in enumerate(rows)
                ],
                active,
                config.stage4.grouping_embedding_model,
            )
        finally:
            active.close()
        selected = neighbor_pairs(rows, vectors, neighbors=3)
        if len(selected) > 1200:
            raise ValueError("Bounded pilot pair limit exceeded (1200)")
        pairs = {f"P{i}": pair for i, pair in enumerate(selected)}

        def object_payload(index):
            row = rows[index]
            return dict(
                object_ref=f"C{index}",
                object=row.object,
                role=row.role,
                events=evidence(row.event_refs[:2]),
            )

        partitions, relation_maps = {}, {}
        for arm in ("normal", "shuffled"):
            state["status"] = f"relating_{arm}"
            order = list(pairs)
            if arm == "shuffled":
                random.Random(20261011).shuffle(order)
            batches = [order[start : start + 20] for start in range(0, len(order), 20)]

            def relate(index, batches=batches, arm=arm):
                refs = batches[index]
                return call(
                    f"{arm}_relations_{index}",
                    dict(
                        scope=scope,
                        pairs=[
                            dict(
                                pair_ref=r,
                                left=object_payload(pairs[r][0]),
                                right=object_payload(pairs[r][1]),
                            )
                            for r in refs
                        ],
                    ),
                    RELATE,
                    checked_schema(Relations, "decisions", "pair_ref", refs),
                )

            answers = parallel(range(len(batches)), relate)
            decisions = {
                d.pair_ref: d.relation for answer in answers.values() for d in answer.decisions
            }
            relation_maps[arm] = decisions
            proposed = components(len(rows), pairs, decisions)
            merging = {f"G{i}": group for i, group in enumerate(proposed) if len(group) > 1}
            accepted = set()
            if merging:
                state["status"] = f"auditing_{arm}"
                audit = call(
                    f"{arm}_audit",
                    dict(
                        scope=scope,
                        groups=[
                            dict(group_ref=ref, objects=[object_payload(i) for i in members])
                            for ref, members in merging.items()
                        ],
                    ),
                    AUDIT,
                    checked_schema(Audits, "decisions", "group_ref", merging),
                )
                accepted = {d.group_ref for d in audit.decisions if d.decision == "ACCEPT"}
            partitions[arm] = [
                members
                for i, group in enumerate(proposed)
                for members in (
                    [group] if len(group) == 1 or f"G{i}" in accepted else [[j] for j in group]
                )
            ]
            save(root / f"{arm}_partition.json", partitions[arm])
        state["status"] = "defining_final_markets"
        final = partitions["normal"]
        batches = [
            list(range(start, min(start + 8, len(final)))) for start in range(0, len(final), 8)
        ]

        def define(index):
            refs = [f"R{i}" for i in batches[index]]
            return call(
                f"definition_{index}",
                dict(
                    scope=scope,
                    markets=[
                        dict(track_ref=f"R{i}", objects=[object_payload(j) for j in final[i]])
                        for i in batches[index]
                    ],
                ),
                DEFINE,
                checked_schema(Definitions, "tracks", "track_ref", refs),
            )

        answers = parallel(range(len(batches)), define)
        definitions = {t.track_ref: t for answer in answers.values() for t in answer.tracks}
        tracks = []
        for i, members in enumerate(final):
            refs = sorted({e for j in members for e in rows[j].event_refs})
            tracks.append(
                dict(
                    **definitions[f"R{i}"].model_dump(),
                    supplier_role=rows[members[0]].role,
                    objects=[rows[j].object for j in members],
                    refs=sorted(set().union(*(event_topics[e] for e in refs))),
                    identity_event_ids=[facts[e]["event_id"] for e in refs],
                    evidence=evidence(refs),
                )
            )
        save(root / "tracks.json", tracks)
        represented = {e for row in rows for e in row.event_refs}
        save(root / "pending_events.json", evidence(sorted(set(facts) - represented)))
        responses = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in (root / "calls").glob("*.json")
            if not p.name.endswith(".input.json")
        ]
        normal_pairs = merged_pairs(partitions["normal"])
        shuffled_pairs = merged_pairs(partitions["shuffled"])
        metrics = dict(
            input_topics=len(inputs["topics"]),
            input_events=len(facts),
            objects=len(rows),
            neighbor_pairs=len(pairs),
            final_tracks=len(tracks),
            shuffled_tracks=len(partitions["shuffled"]),
            relation_agreement=sum(
                relation_maps["normal"][r] == relation_maps["shuffled"][r] for r in pairs
            )
            / len(pairs)
            if pairs
            else 1,
            identical_partition={tuple(x) for x in partitions["normal"]}
            == {tuple(x) for x in partitions["shuffled"]},
            merge_pair_jaccard=len(normal_pairs & shuffled_pairs)
            / len(normal_pairs | shuffled_pairs)
            if normal_pairs | shuffled_pairs
            else 1,
            normal_merged_pairs=len(normal_pairs),
            shuffled_merged_pairs=len(shuffled_pairs),
            represented_events=len(represented),
            pending_events=len(set(facts) - represented),
            input_tokens=sum(r["usage"].get("prompt_tokens", 0) for r in responses),
            output_tokens=sum(r["usage"].get("completion_tokens", 0) for r in responses),
            api_calls=sum(r["call_count"] for r in responses),
            embedding_usage=embedding_usage,
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
