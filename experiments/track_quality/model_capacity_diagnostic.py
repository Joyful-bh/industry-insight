"""Bounded model comparison on identical source-reviewed candidate cards."""
# ruff: noqa: E501

import json
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from scripts.event_stage_supervisor import client, save
from track_insight.settings import load_poc_config
from track_insight.tracks.contracts import SupplierRole

ROOT = Path("data/track_quality/model_capacity_diagnostic/run_1")
MODELS = ["deepseek-v4.1-flash", "qwen3.7-max-2026-06-08"]
# Source-reviewed scopes, not expected market assignments. Mixed facts remain mixed.
SCOPES = {
    1: "激光气体传感器；另有柔性触觉传感器和电子鼻研发产品",
    7: "具身智能机器人产品与核心部件技术攻关、规模化验证和应用扶持",
    8: "对外提供非标零部件加工、样机试制的共享制造服务",
    10: "高多层PCB、HDI线路板及AI服务器背板生产",
    11: "ADC/DAC与硅基射频等信号链芯片及解决方案供给",
    14: "铝合金扁锭、铝板带箔；另有铝合金电缆项目签约",
    16: "电解槽铝冶炼生产及绿电使用",
    25: "石材荒料智能柔性切片加工中心设备制造",
    32: "超薄铝箔生产；另有绿色铝产业集群政策",
    35: "藏药材加工、中成药工业产量、藏药产品开发与生产模式",
    37: "打包采棉机生产",
    41: "中药浓缩丸生产基地与中药、民族药制造扶持",
    48: "PCB产线收购与Micro LED芯片封装集成载板生产基地项目",
    50: "新型显示芯片项目开工，具体芯片功能未说明",
    51: "传感器产业从芯片设计制造封装至整机应用的产业链能力",
    60: "IP、EDA工具采购补助政策，事件没有明确具体芯片产品",
    61: "超声波燃气表生产项目；另有传感器仪器仪表园区建设",
    66: "高档铝合金型材及铝制零部件生产项目",
    67: "风力发电机组采购预中标与风电装备展示",
    70: "自研自产人形机器人整机下线",
    71: "高精密减速机及机器人关节模组产业化、关节模组出货",
    73: "大型收获机械生产测试基地建设项目",
    76: "企业为整车厂供应汽车底盘结构件",
    81: "风电整机生产线、制造产能；基地也有叶片和塔筒产能",
    94: "新能源及新能源装备基地政策，具体供给产品未说明",
}

PROMPT = """为联想SMB制造业客户覆盖整理本轮候选的略粗粒度市场。输入为来源复核的产品范围及事实摘要，不是人工市场答案；没有已证明供给产品的材料允许pending。
只做市场划分，不重新采集、不输出解释评分。技术路线、型号、等级、应用变体不单独切市场。同类主要交付产品尽量使用共同类别；不同交付物、整机和部件、冶炼和深加工、产品供给和代工服务不能仅因同产业链合并。
宽泛候选不能被硬塞进只覆盖部分产品的窄市场；混合候选可保留明确覆盖其事实的宽边界，不强求所有方向完美互斥。不要凭常识补来源没有提及的产品或能力。研发产业化扶持、生产线规划可以支持产品方向；内部使用或泛基地政策不能自动证明供给产品。
每个候选恰好归入一个市场或pending，独立方向可单成员保留。不因不确定如何归并而删除有效方向。
输出JSON {"markets":[{"name":"共同产品类别及供给角色","supplier_role":"MANUFACTURE_PRODUCT","members":["C1"],"boundary":"一句说明包括的产品及必要边界"}],"pending":["C94"]}。成员只引用输入编号；供给角色限DESIGN_PRODUCT、MANUFACTURE_PRODUCT、MANUFACTURING_PROCESS_SERVICE、INDUSTRIAL_SOFTWARE。名称和边界必须与成员事实一致。"""


class Market(BaseModel):
    name: str = Field(min_length=2)
    supplier_role: SupplierRole
    members: list[str] = Field(min_length=1)
    boundary: str = Field(min_length=2)


class Output(BaseModel):
    markets: list[Market]
    pending: list[str]

    @model_validator(mode="after")
    def coverage(self):
        refs = [r for m in self.markets for r in m.members] + self.pending
        expected = {f"C{i}" for i in SCOPES}
        if len(refs) != len(set(refs)) or set(refs) != expected:
            raise ValueError(f"Exactly one destination required; missing={expected - set(refs)}")
        return self


def pairs(output):
    return {tuple(sorted(p)) for m in output.markets for p in combinations(m.members, 2)}


def main():
    config = load_poc_config(Path("config/national.yaml"))
    config.bailian.max_retries = 0
    inputs = json.loads(
        Path("data/track_quality/set_boundary_pilot/run_1/inputs.json").read_text("utf-8")
    )
    rows = inputs["candidates"]
    cards = [
        dict(
            candidate_ref=f"C{i}",
            product_scope=scope,
            supplier_role_hint=rows[i]["supplier_role"],
            source_facts=[e["summary"] for e in rows[i]["events"]],
        )
        for i, scope in SCOPES.items()
    ]
    save(ROOT / "inputs.json", dict(cards=cards, prompt=PROMPT, models=MODELS))
    state = dict(status="running", started_at=datetime.now(UTC).isoformat(), calls={})
    save(ROOT / "status.json", state)
    shuffled = list(cards)
    random.Random(20261013).shuffle(shuffled)

    def call(model, arm, candidates):
        path = ROOT / "calls" / f"{model}_{arm}.json"
        if path.exists():
            return model, arm, Output.model_validate(json.loads(path.read_text("utf-8"))["output"])
        active = client(config)
        try:
            result = active.complete_structured(
                messages=[
                    dict(role="system", content=PROMPT),
                    dict(role="user", content=json.dumps(candidates, ensure_ascii=False)),
                ],
                output_model=Output,
                model=model,
                max_tokens=4096,
                enable_thinking=False,
            )
        finally:
            active.close()
        save(
            path,
            dict(
                output=result.output.model_dump(mode="json"),
                usage=result.usage,
                call_count=result.call_count,
                model=model,
                arm=arm,
            ),
        )
        return model, arm, result.output

    outputs = {}
    failures = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(call, model, arm, data): (model, arm)
            for model in MODELS
            for arm, data in [("normal", cards), ("shuffled", shuffled)]
        }
        for future in as_completed(futures):
            key = "_".join(futures[future])
            try:
                model, arm, result = future.result()
                outputs[(model, arm)] = result
                state["calls"][key] = "completed"
                print(f"{key}: completed", flush=True)
            except Exception as error:
                failures[key] = str(error)
                state["calls"][key] = "failed"
                print(f"{key}: failed: {error}", flush=True)
            save(ROOT / "status.json", state)
    metrics = {}
    for model in MODELS:
        if all((model, arm) in outputs for arm in ["normal", "shuffled"]):
            normal, shuffled_result = (outputs[(model, arm)] for arm in ["normal", "shuffled"])
            left, right = pairs(normal), pairs(shuffled_result)
            metrics[model] = dict(
                tracks=[len(normal.markets), len(shuffled_result.markets)],
                pending=[normal.pending, shuffled_result.pending],
                merge_pairs=[sorted(left), sorted(right)],
                merge_pair_jaccard=len(left & right) / len(left | right) if left | right else 1,
            )
    save(
        ROOT / "metrics.json",
        dict(models=metrics, failures=failures, database_modified=False, production_modified=False),
    )
    state.update(status="completed" if not failures else "partial_failure", failures=failures)
    save(ROOT / "status.json", state)


if __name__ == "__main__":
    main()
