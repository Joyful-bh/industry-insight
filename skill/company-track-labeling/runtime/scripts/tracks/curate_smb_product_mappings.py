#!/usr/bin/env python3
"""Create reviewed Lenovo SMB mappings locally without calling any model API."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from remap_smb_products import load_tracks, parse_markdown_catalog


TRACKS = Path("data/processed/tracks_curated/tracks_classification_ready.jsonl")
CATALOG = Path("data/raw/联想SMB产品服务列表.md")
OUTPUT = Path("outputs/smb_product_remapping")


GROUPS: dict[str, set[str]] = {
    "chip_design": {"AI芯片设计", "DRAM存储芯片", "RISC-V处理器核研发", "智能终端与边缘SoC芯片设计", "芯片定制设计服务", "车规级芯片设计", "集成电路设计"},
    "semiconductor_mfg": {"GaN功率器件", "MEMS传感器制造", "先进封装测试", "半导体关键材料制造", "玻璃基先进封装", "集成电路制造"},
    "display_mfg": {"Mini/Micro LED显示产品制造", "OLED与AMOLED显示面板制造", "新型显示面板制造", "车载显示面板制造"},
    "digital_hardware": {"6G卫星载荷制造", "6G通信设备与产品研发", "AI智能终端制造", "AI服务器与算力设备制造", "XR与AI可穿戴终端制造"},
    "aerospace": {"低空航空器与无人机制造", "可重复使用运载火箭", "商业卫星与航天器制造", "高端船舶制造"},
    "industrial_equipment": {"具身智能机器人", "医疗影像设备与核心探测器制造", "半导体设备与核心部件制造", "工业机器人制造", "氢能装备与关键部件制造", "量子精密测量仪器制造", "高端工业母机与核心部件制造", "高端科学仪器制造"},
    "vehicle": {"新能源汽车充换电设备制造", "新能源汽车电驱系统制造", "智能底盘系统", "智能网联新能源汽车制造", "智能驾驶系统与核心部件", "汽车电子电气架构", "高级别自动驾驶汽车制造"},
    "battery_energy": {"动力电池与储能", "固态与半固态电池制造", "新型储能系统与装备制造", "电池材料制造", "退役锂电池回收与材料再生", "钙钛矿光伏技术"},
    "life_science": {"创新医疗器械研发制造", "创新药及高端仿制药制造", "合成生物制造", "替代蛋白制造", "生物药与重组蛋白制造", "细胞与基因治疗产品研发制造", "脑机接口"},
    "advanced_material": {"高性能纤维与复合材料制造"},
}


def group_for(name: str) -> str:
    matches = [group for group, names in GROUPS.items() if name in names]
    if len(matches) != 1:
        raise ValueError(f"Track {name!r} must belong to exactly one group, got {matches}")
    return matches[0]


def opportunity(
    name: str, relation: str, ids: list[str], stages: list[str], workloads: list[str],
    triggers: list[str], buyers: list[str], reason: str, priority: str, confidence: float,
) -> dict[str, Any]:
    return {
        "opportunity_name": name,
        "relationship": relation,
        "catalog_item_ids": ids,
        "target_company_stage": stages,
        "workloads": workloads,
        "buying_triggers": triggers,
        "buyer_roles": buyers,
        "fit_reason": reason,
        "priority": priority,
        "confidence": confidence,
    }


def ecosystem(need: str, reason: str, role: str) -> dict[str, str]:
    return {"need": need, "reason": reason, "possible_lenovo_role": role}


def build_analysis(track: dict[str, Any]) -> dict[str, Any]:
    name = track["name"]
    definition = track["definition"]
    group = group_for(name)
    opportunities: list[dict[str, Any]] = []
    needs: list[dict[str, str]] = []
    constraints = ["采购应随可验证的研发、试制或量产工作负载确定，不按赛道热度预设规模。"]

    if group == "chip_design":
        opportunities = [
            opportunity("芯片设计与验证计算基础设施", "INFRASTRUCTURE_ENABLER", ["P024", "P037"], ["研发", "成长扩张"], ["承载第三方EDA仿真、综合和验证任务"], ["本地工作站资源不足", "并行验证任务增加"], ["研发负责人", "IT负责人"], "工作站和机架服务器提供计算承载，联想不提供EDA工具。", "HIGH", .86),
            opportunity("芯片设计数据集中存储", "DIRECT_SOLUTION", ["P045"], ["研发", "成长扩张"], ["集中保存设计文件、代码库和验证结果"], ["设计数据量增长", "多人并行访问"], ["研发负责人", "IT负责人"], "企业存储直接提供集中容量和访问能力。", "HIGH", .84),
            opportunity("研发数据备份与介质留存", "DIRECT_SOLUTION", ["P096", "P100"], ["研发", "通用"], ["备份版本库与设计数据", "故障盘敏感数据留存"], ["建立备份制度", "敏感项目数据保护要求"], ["IT负责人", "信息安全负责人"], "目录服务直接覆盖备份和故障盘不回收。", "MEDIUM", .80),
        ]
        needs = [ecosystem("EDA工具链及许可证", "芯片设计、验证和签核依赖专业EDA软件。", "基础设施承载"), ecosystem("IP核、晶圆代工与封测协同", "属于芯片设计交付生态，不在联想SMB目录内。", "当前无明确角色")]
        constraints += ["EDA许可证和算力通常共同决定方案规模。", "不得将工作站或服务器描述为EDA软件方案。"]
    elif group in {"semiconductor_mfg", "display_mfg"}:
        engineering_workstation = "P024" if group == "semiconductor_mfg" else "P023"
        opportunities = [
            opportunity("工艺研发与工程分析工作站", "INFRASTRUCTURE_ENABLER", [engineering_workstation], ["研发", "中试"], ["承载第三方工艺仿真、版图或检测分析软件"], ["建立工艺研发团队", "分析数据复杂度提高"], ["研发负责人", "工艺负责人"], "专业工作站提供计算承载，不包含工艺软件。", "HIGH", .82),
            opportunity("产线检测数据边缘节点", "INFRASTRUCTURE_ENABLER", ["P043"], ["中试", "量产"], ["在设备侧承载第三方采集、检测和预处理程序"], ["中试线投产", "检测设备联网"], ["生产负责人", "自动化负责人"], "边缘服务器适合靠近设备部署，但不替代设备控制系统。", "HIGH", .81),
            opportunity("制造业务系统本地承载", "INFRASTRUCTURE_ENABLER", ["P035"], ["中试", "量产"], ["承载第三方MES、QMS或设备数据服务"], ["生产追溯系统上线", "业务系统需要本地部署"], ["生产负责人", "IT负责人"], "塔式服务器可承载第三方制造系统，联想不提供这些应用。", "MEDIUM", .76),
            opportunity("研发与检测数据备份", "DIRECT_SOLUTION", ["P096"], ["研发", "中试", "量产"], ["备份配方、检测结果和工程文件"], ["形成关键电子数据", "建立恢复机制"], ["IT负责人", "质量负责人"], "数据备份服务直接降低关键文件丢失风险。", "MEDIUM", .79),
        ]
        if group == "display_mfg":
            opportunities.pop()
        needs = [ecosystem("MES、QMS及设备自动化软件", "制造执行、质量追溯和设备控制依赖行业系统。", "基础设施承载"), ecosystem("专业工艺与检测设备", "生产工艺和质量检测由专用设备完成。", "当前无明确角色")]
        constraints += ["产线设备接口和工业协议需在项目中单独验证。"]
    elif group in {"digital_hardware", "industrial_equipment", "aerospace", "vehicle"}:
        workstation_ids = ["P022", "P024"] if group in {"digital_hardware", "aerospace", "vehicle"} else ["P023"]
        opportunities = [
            opportunity("工程设计与仿真工作站", "INFRASTRUCTURE_ENABLER", workstation_ids, ["研发", "中试"], ["承载第三方CAD、CAE、EDA或算法开发工具"], ["复杂产品研发立项", "现有终端无法满足计算或图形负载"], ["研发负责人", "IT负责人"], "专业工作站提供计算平台，不包含行业软件许可证。", "HIGH", .87),
            opportunity("试验与产线边缘计算节点", "INFRASTRUCTURE_ENABLER", ["P043"], ["中试", "量产"], ["承载测试数据采集、设备侧分析或第三方边缘应用"], ["建设试验台或中试线", "需要就近处理设备数据"], ["测试负责人", "生产负责人"], "边缘服务器可部署在实验室或产线侧，具体采集软件由生态系统提供。", "HIGH", .82),
            opportunity("工程与试验数据集中存储", "DIRECT_SOLUTION", ["P045"], ["研发", "中试", "量产"], ["集中保存设计文件、测试记录和产品文档"], ["项目数据增长", "多人协同访问"], ["研发负责人", "IT负责人"], "企业存储直接提供集中容量与文件访问基础。", "MEDIUM", .81),
            opportunity("关键研发数据备份", "DIRECT_SOLUTION", ["P096"], ["研发", "中试", "量产"], ["备份设计、软件版本和测试数据"], ["关键数据进入正式管理", "建立灾后恢复流程"], ["IT负责人", "研发负责人"], "备份服务直接覆盖数据副本和恢复需求。", "MEDIUM", .80),
        ]
        if group == "industrial_equipment":
            opportunities.pop()
        needs = [ecosystem("CAD、CAE、EDA及专业研发软件", "产品设计和仿真依赖目录外专业软件。", "基础设施承载"), ecosystem("PLM、MES、QMS与测试管理系统", "研发协同、制造追溯和质量管理需要行业应用。", "基础设施承载"), ecosystem("专用试验、检测与控制设备", "产品验证和生产控制依赖行业专用设备。", "当前无明确角色")]
        constraints += ["研发软件认证、设备接口和数据量决定具体配置。"]
    elif group in {"battery_energy", "advanced_material"}:
        opportunities = [
            opportunity("材料与工艺研发计算工作站", "INFRASTRUCTURE_ENABLER", ["P023"], ["研发", "中试"], ["承载第三方材料模拟、工艺分析和检测数据处理软件"], ["开展材料或工艺研发", "分析任务超过普通终端能力"], ["研发负责人", "实验室负责人"], "工作站提供专业计算承载，不包含材料模拟软件。", "HIGH", .81),
            opportunity("中试与产线数据边缘节点", "INFRASTRUCTURE_ENABLER", ["P043"], ["中试", "量产"], ["承载设备数据采集和第三方过程监控程序"], ["中试线投产", "关键设备联网"], ["工艺负责人", "生产负责人"], "边缘服务器适合设备侧部署，不替代工业控制系统。", "HIGH", .79),
            opportunity("质量与生产系统本地承载", "INFRASTRUCTURE_ENABLER", ["P035"], ["中试", "量产"], ["承载第三方LIMS、MES或QMS"], ["建立批次追溯", "行业系统选择本地部署"], ["质量负责人", "IT负责人"], "塔式服务器仅提供应用运行基础设施。", "MEDIUM", .74),
        ]
        needs = [ecosystem("LIMS、MES、QMS与批次追溯", "实验、生产和质量流程依赖行业软件。", "基础设施承载"), ecosystem("工艺设备、检测仪器及工业控制", "材料制备和质量检测依赖专用设备。", "当前无明确角色")]
        constraints += ["不同工艺路线的数据采集条件差异较大。"]
    elif group == "life_science":
        opportunities = [
            opportunity("科研计算与仪器数据分析工作站", "INFRASTRUCTURE_ENABLER", ["P024"], ["研发", "中试"], ["承载第三方生物信息、影像、统计或仪器分析软件"], ["建立数据分析能力", "仪器数据处理负载增加"], ["研发负责人", "实验室负责人"], "工作站提供计算承载，不包含科研分析软件。", "HIGH", .80),
            opportunity("实验与质量系统本地承载", "INFRASTRUCTURE_ENABLER", ["P035"], ["研发", "中试", "量产"], ["承载第三方LIMS、QMS或实验数据服务"], ["实验流程电子化", "系统需要本地部署"], ["质量负责人", "IT负责人"], "服务器仅承载第三方生命科学应用。", "MEDIUM", .73),
            opportunity("实验与研发数据集中存储", "DIRECT_SOLUTION", ["P045"], ["研发", "中试"], ["集中保存实验结果、仪器文件和研究文档"], ["数据量增长", "多人共享实验数据"], ["研发负责人", "IT负责人"], "企业存储直接提供集中容量和访问能力。", "HIGH", .82),
            opportunity("关键研究数据备份", "DIRECT_SOLUTION", ["P096"], ["研发", "中试", "量产"], ["备份原始实验数据和关键研究文档"], ["建立数据恢复制度", "关键研究数据进入受控管理"], ["研发负责人", "质量负责人"], "数据备份服务直接提供副本保护；是否满足监管要求需另行验证。", "HIGH", .83),
        ]
        needs = [ecosystem("LIMS、QMS及受监管数据管理系统", "实验室、质量和受监管记录需要专业系统。", "基础设施承载"), ecosystem("生物信息、统计或医学专业软件", "科研分析能力不在联想产品目录内。", "基础设施承载"), ecosystem("实验仪器、生产设备与验证服务", "研发生产依赖专用设备及行业验证。", "当前无明确角色")]
        constraints += ["合规适用性、审计追踪和系统验证不能由硬件目录推定。"]
    else:
        raise AssertionError(group)

    stage_map = {
        "chip_design": ["研发", "成长扩张"], "semiconductor_mfg": ["研发", "中试", "量产"],
        "display_mfg": ["研发", "中试", "量产"], "digital_hardware": ["研发", "中试", "量产"],
        "aerospace": ["研发", "中试", "量产"], "industrial_equipment": ["研发", "中试", "量产"],
        "vehicle": ["研发", "中试", "量产"], "battery_energy": ["研发", "中试", "量产"],
        "life_science": ["研发", "中试", "量产"], "advanced_material": ["研发", "中试", "量产"],
    }
    return {
        "track_id": track["track_id"],
        "track_name": name,
        "enterprise_profile": {
            "typical_business": definition,
            "typical_company_stages": stage_map[group],
            "it_characteristics": [x["workloads"][0] for x in opportunities],
        },
        "digital_workload_profile": [x["workloads"][0] for x in opportunities],
        "smb_constraints": constraints,
        "product_opportunities": opportunities,
        "ecosystem_needs": needs,
        "non_recommended_catalog_item_ids": [],
        "analysis_summary": f"{name}的可靠机会集中在专业工作负载承载与关键数据保护；仅在出现明确项目触发时配置，未将通用办公、租赁或大型数据中心作为默认需求。",
    }


def validate(records: list[dict[str, Any]], catalog_by_id: dict[str, dict[str, str]]) -> None:
    valid_rel = {"DIRECT_SOLUTION", "INFRASTRUCTURE_ENABLER"}
    for record in records:
        result = record["result"]
        recommended: list[str] = []
        for item in result["product_opportunities"]:
            if item["relationship"] not in valid_rel or not item["catalog_item_ids"]:
                raise ValueError(f"Invalid opportunity in {result['track_name']}")
            if len(item["catalog_item_ids"]) != len(set(item["catalog_item_ids"])):
                raise ValueError(f"Duplicate IDs in {result['track_name']}")
            recommended.extend(item["catalog_item_ids"])
        unknown = set(recommended + result["non_recommended_catalog_item_ids"]) - set(catalog_by_id)
        if unknown:
            raise ValueError(f"Unknown IDs in {result['track_name']}: {unknown}")
        if set(recommended) & set(result["non_recommended_catalog_item_ids"]):
            raise ValueError(f"Recommended/non-recommended conflict in {result['track_name']}")


def main() -> None:
    tracks = load_tracks(TRACKS)
    catalog = parse_markdown_catalog(CATALOG)
    catalog_by_id = {x["catalog_item_id"]: x for x in catalog}
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    records = []
    for track in tracks:
        result = build_analysis(track)
        for item in result["product_opportunities"]:
            item["catalog_items"] = [catalog_by_id[x] for x in item["catalog_item_ids"]]
        records.append({"track_id": track["track_id"], "track_name": track["name"], "method": "expert_rule_curated", "generated_at": generated, "result": result})
    validate(records, catalog_by_id)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    metadata = {"method": "expert_rule_curated", "track_count": len(tracks), "completed_count": len(records), "product_catalog_item_count": len(catalog), "external_api_calls": 0}
    (OUTPUT / "smb_product_mappings.json").write_text(json.dumps({"metadata": metadata, "mappings": records}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (OUTPUT / "smb_product_mappings.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    fields = ["track_id", "track_name", "opportunity_name", "relationship", "priority", "confidence", "catalog_item_ids", "catalog_items", "fit_reason"]
    with (OUTPUT / "smb_product_opportunities.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            for item in record["result"]["product_opportunities"]:
                writer.writerow({"track_id": record["track_id"], "track_name": record["track_name"], "opportunity_name": item["opportunity_name"], "relationship": item["relationship"], "priority": item["priority"], "confidence": item["confidence"], "catalog_item_ids": ";".join(item["catalog_item_ids"]), "catalog_items": ";".join(x["product_service"] for x in item["catalog_items"]), "fit_reason": item["fit_reason"]})
    print(f"Generated {len(records)} curated track mappings in {OUTPUT}")


if __name__ == "__main__":
    main()
