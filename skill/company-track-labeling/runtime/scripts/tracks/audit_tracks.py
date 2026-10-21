#!/usr/bin/env python3
"""Create a reproducible first-pass quality audit for extracted tracks."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any


OVERLAP_GROUPS = {
    "6G与卫星通信": {"6G卫星载荷制造", "6G通信技术研发", "卫星制造", "商业卫星电推进系统制造"},
    "自动驾驶": {"L3级自动驾驶汽车制造", "L4级自动驾驶量产车辆制造", "自动驾驶技术", "智能网联新能源汽车制造"},
    "具身智能与机器人": {"人形机器人", "具身智能"},
    "显示技术": {
        "AMOLED显示面板制造", "MLED显示产品与解决方案", "OLED显示技术", "新型显示面板制造",
        "显示制造大模型", "车载显示面板制造", "LCD电视显示技术", "Micro LED显示产品制造", "裸眼3D显示产品",
    },
    "芯片设计": {
        "AI芯片设计", "RISC-V处理器核研发", "BMS芯片研发", "应用处理器芯片设计", "系统级芯片（SoC）设计",
        "芯片定制设计服务", "视频处理器芯片设计", "车规级NOR Flash存储芯片", "车规级模拟芯片", "边缘计算芯片",
    },
    "先进封装": {"先进封装测试", "玻璃基先进封装"},
    "固态电池": {"固态电池技术", "半固态电池技术", "动力电池与储能", "电池材料制造"},
    "船舶": {"大型船舶制造", "大型邮轮制造"},
    "工业软件": {"工业软件", "工业母机设计软件", "基础软件"},
    "商业航天": {"卫星制造", "商业航天器制造", "可重复使用运载火箭", "商业卫星电推进系统制造"},
}

COARSE_TRACKS = {
    "工业软件", "基础软件", "智慧终端制造", "工程机械", "钢铁制造", "卫星制造", "商业航天器制造",
    "航空器整机制造", "生物药品制造", "精准医学", "自动驾驶技术", "动力电池与储能", "电池材料制造",
    "新型显示面板制造", "创新医疗器械研发制造", "行业垂直大模型", "信创解决方案", "智慧能源服务",
}

FINE_TRACKS = {
    "BMS芯片研发", "应用处理器芯片设计", "视频处理器芯片设计", "车规级NOR Flash存储芯片", "车规级模拟芯片",
    "边缘计算芯片", "系统级芯片（SoC）设计", "番茄育种固相基因芯片", "干式真空泵制造", "轴向磁通电机",
    "X射线平板探测器制造", "商业卫星电推进系统制造", "氢燃料电池汽车质量检验检测服务", "电动载人飞艇",
}

DIRECT_OFFERING_PATTERNS = {
    "终端/工作站": r"工作站|商用电脑|商用PC|PC终端|笔记本|台式机|显示器|本地AI|本地推理",
    "服务器/算力": r"服务器|高性能计算|HPC|算力资源|计算集群|训练集群|推理集群|GPU",
    "存储/数据保护": r"存储阵列|企业存储|数据备份|备份系统|数据恢复|容灾|数据迁移|数据销毁",
    "超融合/云基础设施": r"超融合|虚拟化|私有云|混合云|云基础设施|云计算资源|云平台",
    "网络/安全": r"办公网|无线网络|Wi-?Fi|网络建设|网络安全|防火墙",
    "设备与IT服务": r"DaaS|设备租赁|设备采购|设备部署|设备运维|IT运维|IT外包|IT管家|资产管理|设备巡检",
    "企业SaaS": r"Microsoft 365|OA|CRM|eHR|企业邮箱|在线文档|智慧客服|协同办公",
    "会议/智慧办公": r"智能会议|视频会议|远程会议|智慧办公|智慧空间",
    "AI/智能体": r"企业智能体|AI智能体|智能IT服务|AI IT|AI运维|AaaS",
}

NON_CATALOG_PATTERNS = {
    "制造运营软件": r"\bMES\b|制造执行|生产执行|\bSCADA\b|设备监控系统|质量追溯",
    "企业经营软件": r"\bERP\b|供应链管理|\bSCM\b|订单管理|仓储管理|物流管理|财务软件",
    "研发设计软件": r"\bPLM\b|\bPDM\b|\bCAD\b|\bCAE\b|\bEDA\b|仿真软件|设计软件|版本控制|DevOps|CI/CD",
    "行业专用软件": r"\bLIMS\b|实验室信息|\bQMS\b|质量管理系统|临床试验|生物信息学|医学影像|医院信息|能源管理系统",
}

FIELDS = (
    "track_id", "name", "status", "heat", "confidence", "evidence_grade", "event_count", "source_count",
    "granularity", "overlap_groups", "definition_quality", "interpretation_quality", "lenovo_fit",
    "direct_product_domains", "non_catalog_claims", "main_issues", "revision_priority",
)


def evidence_grade(track: dict[str, Any]) -> str:
    sources, events = track["source_count"], track["event_count"]
    if sources >= 5 and events >= 5:
        return "A"
    if sources >= 3 and events >= 3:
        return "B"
    if sources >= 2 and events >= 2:
        return "C"
    return "D"


def audit(track: dict[str, Any]) -> dict[str, Any]:
    name = track["name"]
    analysis_text = json.dumps(track.get("smb_value_analysis", []), ensure_ascii=False)
    direct = [label for label, pattern in DIRECT_OFFERING_PATTERNS.items() if re.search(pattern, analysis_text, re.I)]
    unsupported = [label for label, pattern in NON_CATALOG_PATTERNS.items() if re.search(pattern, analysis_text, re.I)]
    groups = [label for label, members in OVERLAP_GROUPS.items() if name in members]
    if name in COARSE_TRACKS:
        granularity = "偏粗"
    elif name in FINE_TRACKS:
        granularity = "偏细"
    else:
        granularity = "基本可用"

    issues: list[str] = []
    grade = evidence_grade(track)
    if grade == "D":
        issues.append("热点证据弱")
    if groups:
        issues.append("需确认相邻赛道可独立判断")
    if unsupported:
        issues.append("将非目录软件写成SMB可提供能力")
    if not direct:
        issues.append("未映射到明确的联想SMB产品域")
    if not track.get("aliases"):
        issues.append("缺少检索别名")

    required = ("definition", "included_activities", "excluded_activities", "observable_company_features")
    if any(not track.get(field) for field in required):
        definition_quality = "缺少分类字段"
    elif groups:
        definition_quality = "需相邻边界复核"
    else:
        definition_quality = "可用"
    if unsupported and not direct:
        interpretation_quality = "差"
        lenovo_fit = "低"
    elif unsupported:
        interpretation_quality = "需重写产品映射"
        lenovo_fit = "中"
    elif direct:
        interpretation_quality = "基本可用"
        lenovo_fit = "高"
    else:
        interpretation_quality = "需补充"
        lenovo_fit = "低"

    high = definition_quality == "缺少分类字段" or interpretation_quality in {"差", "需重写产品映射"}
    return {
        "track_id": track["track_id"],
        "name": name,
        "status": track["status"],
        "heat": track["heat"],
        "confidence": track["confidence"],
        "evidence_grade": grade,
        "event_count": track["event_count"],
        "source_count": track["source_count"],
        "granularity": granularity,
        "overlap_groups": groups,
        "definition_quality": definition_quality,
        "interpretation_quality": interpretation_quality,
        "lenovo_fit": lenovo_fit,
        "direct_product_domains": direct,
        "non_catalog_claims": unsupported,
        "main_issues": issues,
        "revision_priority": "高" if high else ("中" if grade == "D" else "低"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="tracks.json created by extract_tracks.py")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/track_quality"))
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    results = [audit(track) for track in payload["tracks"]]
    args.output_dir.mkdir(parents=True, exist_ok=True)

    (args.output_dir / "track_quality_audit.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (args.output_dir / "track_quality_audit.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for result in results:
            row = dict(result)
            for field in FIELDS:
                if isinstance(row.get(field), list):
                    row[field] = "；".join(row[field])
            writer.writerow(row)
    print(f"Audited {len(results)} tracks to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
