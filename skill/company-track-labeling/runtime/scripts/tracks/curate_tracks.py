#!/usr/bin/env python3
"""Curate dashboard tracks into classification-ready, flat multi-label TrackSpecs."""

from __future__ import annotations

import argparse
import csv
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any


DELETE = {
    "AI整合营销服务": "营销服务不属于制造赛道，且不能形成制造企业分类条件",
    "信创解决方案": "通用解决方案集合，边界取决于应用项目而非制造活动",
    "工业软件": "通用软件类别，不作为本轮制造企业赛道",
    "显示制造大模型": "制造工具而非制造企业赛道",
    "智慧终端制造": "定义由单一海外产能项目概括，企业集合过于泛化",
    "智慧能源服务": "能源运营与服务集合，不是设备制造赛道",
    "精准医学": "混合诊断、治疗和服务，无法形成一致的制造企业集合",
    "行业垂直大模型": "跨行业软件类别，不作为制造企业赛道",
    "集成电路测试与认证服务": "测试认证服务不属于制造企业集合",
    "LCD电视显示技术": "成熟产品的单次技术升级不足以构成独立热门赛道",
    "基础软件": "通用软件类别，不作为本轮制造企业赛道",
    "大模型Token算力服务": "算力运营服务，不属于制造企业集合",
    "工业母机设计软件": "删除软件条目，另行新增高端工业母机制造赛道",
    "氢燃料电池汽车质量检验检测服务": "检验检测服务不属于制造企业集合",
    "番茄育种固相基因芯片": "单一作物和单一产品事件，尚不足以形成稳定热门赛道",
    "裸眼3D显示产品": "单一产品事件且企业集合过窄，近期热度证据不足",
    "超高清现场服务": "内容呈现与场馆运营服务，不属于制造企业集合",
    "钢铁制造": "成熟大行业的一次利润变化不足以证明形成新的热门赛道",
    "工程机械": "成熟大行业的单一企业事件不足以证明北京形成新的热门赛道",
    "印制电路板制造": "单一扩产事件不足以证明形成独立热门赛道",
    "智能调光玻璃": "单一产品事件且近期独立产业证据不足",
    "轻量化部件制造": "定义跨汽车和航空零部件，单一事件不足以形成稳定热门赛道",
}


MERGES = [
    {
        "sources": ["AMOLED显示面板制造", "OLED显示技术"],
        "name": "OLED与AMOLED显示面板制造",
        "definition": "从事OLED及AMOLED显示面板、模组的研发、生产、验证与交付的企业。",
        "aliases": ["OLED面板", "AMOLED面板", "有机发光显示面板"],
        "included_activities": ["OLED或AMOLED面板研发制造", "OLED显示模组制造", "OLED面板量产工艺和产品验证"],
        "excluded_activities": ["OLED材料生产", "OLED设备制造", "显示终端整机组装", "面板贸易分销"],
        "observable_company_features": ["具备OLED或AMOLED面板产线", "具有OLED面板或模组产品", "开展面板量产和客户验证"],
    },
    {
        "sources": ["MLED显示产品与解决方案", "Micro LED显示产品制造"],
        "name": "Mini/Micro LED显示产品制造",
        "definition": "从事Mini LED或Micro LED芯片、背光、面板、模组及显示终端产品研发和制造的企业。",
        "aliases": ["MLED", "Mini LED", "Micro LED", "微型发光二极管显示"],
        "included_activities": ["Mini LED背光和直显产品制造", "Micro LED芯片面板或模组制造", "巨量转移与封装工艺产业化"],
        "excluded_activities": ["普通LED照明", "传统LCD面板制造", "仅提供显示内容或系统运营", "显示产品贸易"],
        "observable_company_features": ["具有Mini或Micro LED产品", "具备芯片模组或面板量产能力", "开展巨量转移等关键工艺"],
    },
    {
        "sources": ["L3级自动驾驶汽车制造", "L4级自动驾驶量产车辆制造"],
        "name": "高级别自动驾驶汽车制造",
        "definition": "研发和制造具备L3及以上自动驾驶能力并进入准入、试点或量产阶段的整车企业。",
        "aliases": ["L3自动驾驶汽车", "L4自动驾驶汽车", "高级别自动驾驶整车"],
        "included_activities": ["L3及以上自动驾驶车型设计制造", "高级别自动驾驶系统整车集成", "自动驾驶车型准入和量产交付"],
        "excluded_activities": ["仅开发自动驾驶软件或部件", "Robotaxi运营", "L2及以下辅助驾驶整车", "测试场运营"],
        "observable_company_features": ["具有L3及以上车型或准入记录", "具备整车生产资质", "有高级别自动驾驶整车量产或试点交付"],
    },
    {
        "sources": ["人形机器人", "具身智能"],
        "name": "具身智能机器人",
        "definition": "研发和制造具备环境感知、任务决策及物理执行能力的人形或其他具身智能机器人整机与关键系统的企业。",
        "aliases": ["具身智能", "人形机器人", "智能机器人", "Embodied AI"],
        "included_activities": ["具身智能机器人整机研发制造", "人形机器人整机研发制造", "具身机器人核心控制和执行系统产品化"],
        "excluded_activities": ["传统固定程序工业机器人", "无机器人实体的通用AI软件", "机器人租赁培训或维修", "学术研究未产品化"],
        "observable_company_features": ["具有可交付机器人整机或核心系统", "开展机器人中试或量产", "产品具备感知决策和物理执行能力"],
    },
    {
        "sources": ["卫星制造", "商业航天器制造", "商业卫星电推进系统制造"],
        "name": "商业卫星与航天器制造",
        "definition": "面向商业应用研发、制造和交付卫星平台、整星、航天器及电推进等关键分系统的企业。",
        "aliases": ["商业卫星", "卫星整星", "航天器制造", "低轨卫星", "卫星电推进"],
        "included_activities": ["商业卫星平台和整星制造", "商业航天器总装集成测试", "卫星关键分系统制造", "卫星电推进系统制造"],
        "excluded_activities": ["卫星发射服务", "卫星在轨运营", "卫星数据应用", "地面终端制造", "仅提供航天咨询"],
        "observable_company_features": ["具有卫星或航天器产品型号", "具备总装集成测试或分系统产线", "获得卫星批量交付订单"],
    },
    {
        "sources": ["固态电池技术", "半固态电池技术"],
        "name": "固态与半固态电池制造",
        "definition": "研发、试制或量产采用固态或半固态电解质体系的动力及储能电池、电芯的企业。",
        "aliases": ["全固态电池", "固态电池", "半固态电池", "固液混合电池"],
        "included_activities": ["固态或半固态电芯研发试制", "固态或半固态电池量产", "固态电池包和模组制造"],
        "excluded_activities": ["仅生产传统液态电池", "仅研发电解质等材料", "电池回收", "整车制造"],
        "observable_company_features": ["具有固态或半固态电芯样品和指标", "建有中试或量产线", "取得装车验证或客户订单"],
    },
    {
        "sources": ["大型船舶制造", "大型邮轮制造"],
        "name": "高端船舶制造",
        "definition": "从事大型邮轮、智能船舶、特种船舶等高技术高附加值船舶设计、建造和交付的企业。",
        "aliases": ["大型邮轮", "智能船舶", "特种船舶", "高端船舶"],
        "included_activities": ["大型邮轮设计建造", "智能或特种船舶建造", "高技术船舶总装和交付"],
        "excluded_activities": ["普通小型船舶制造", "船舶运营租赁", "独立船舶维修", "船用材料生产"],
        "observable_company_features": ["具有高端船舶建造资质和船坞产线", "拥有在建或交付船型", "承担整船设计总装"],
    },
    {
        "sources": ["新能源汽车电驱系统", "轴向磁通电机"],
        "name": "新能源汽车电驱系统制造",
        "definition": "研发和制造新能源汽车电机、电控、减速器集成系统及轴向磁通等新型驱动电机的企业。",
        "aliases": ["电驱系统", "三合一电驱", "电机电控", "轴向磁通电机"],
        "included_activities": ["新能源汽车电驱总成制造", "驱动电机和电控制造", "减速器集成制造", "轴向磁通等新型驱动电机制造"],
        "excluded_activities": ["整车总装", "动力电池制造", "传统燃油发动机制造", "仅开发控制算法"],
        "observable_company_features": ["具有电驱总成或核心部件产品", "获得整车企业定点", "具备电机电控生产线"],
    },
    {
        "sources": ["人工智能算力基础设施", "AI服务器制造"],
        "name": "AI服务器与算力设备制造",
        "definition": "研发和制造面向人工智能训练、推理及高性能计算的服务器、计算节点和算力基础设施硬件设备的企业。",
        "aliases": ["AI服务器", "智算服务器", "GPU服务器", "算力设备", "人工智能服务器"],
        "included_activities": ["AI服务器硬件设计制造", "GPU或加速卡服务器集成", "液冷算力节点制造", "高性能计算设备制造"],
        "excluded_activities": ["仅运营智算中心", "算力租赁服务", "云计算服务", "AI模型训练", "通用数据中心施工"],
        "observable_company_features": ["具有AI服务器或算力设备型号", "具备服务器组装测试产线", "向智算中心交付硬件"],
    },
    {
        "sources": ["航空器整机制造", "电动载人飞艇"],
        "name": "低空航空器与无人机制造",
        "definition": "研发和制造无人机、eVTOL、通用飞机、电动航空器及飞艇等低空航空器整机的企业。",
        "aliases": ["无人机", "eVTOL", "电动垂直起降航空器", "通用飞机", "电动航空器", "载人飞艇"],
        "included_activities": ["民用无人机整机制造", "eVTOL整机研发制造", "通用航空器制造", "电动航空器或飞艇制造"],
        "excluded_activities": ["低空运营服务", "飞行培训", "机场和起降场建设", "航空器维修租赁", "仅制造通用零部件"],
        "observable_company_features": ["具有航空器或无人机整机型号", "具备整机总装测试能力", "开展适航取证或批量交付"],
        "supplementary_source_ids": ["beijing_low_altitude", "beijing_future_industries_2026"],
    },
    {
        "sources": ["XR智能终端制造", "AI眼镜制造"],
        "name": "XR与AI可穿戴终端制造",
        "definition": "研发和制造AR/VR/MR终端、AI眼镜及其光学显示和交互硬件产品的企业。",
        "aliases": ["XR终端", "AR眼镜", "VR头显", "MR终端", "AI眼镜", "智能眼镜"],
        "included_activities": ["AR/VR/MR终端整机制造", "AI眼镜整机制造", "XR光学显示模组制造", "XR交互硬件制造"],
        "excluded_activities": ["仅开发XR内容或应用", "传统眼镜制造", "智能终端零售", "通用AI算法开发"],
        "observable_company_features": ["具有XR或AI眼镜产品型号", "具备整机或核心模组制造能力", "开展中试量产和产品认证"],
        "supplementary_source_ids": ["beijing_xr_2026"],
    },
    {
        "sources": ["BMS芯片研发", "车规级NOR Flash存储芯片", "车规级模拟芯片"],
        "name": "车规级芯片设计",
        "definition": "设计面向汽车电子并满足车规可靠性要求的BMS、存储、模拟、控制等芯片产品的企业。",
        "aliases": ["车规芯片", "汽车芯片", "BMS芯片", "车规NOR Flash", "车规模拟芯片"],
        "included_activities": ["车规BMS芯片设计", "车规存储芯片设计", "车规模拟和控制芯片设计", "车规芯片验证与产品化"],
        "excluded_activities": ["汽车电子系统集成", "消费级芯片设计", "晶圆代工", "芯片分销"],
        "observable_company_features": ["具有车规芯片产品型号", "通过AEC-Q等车规认证", "获得汽车客户定点或量产订单"],
    },
    {
        "sources": ["应用处理器芯片设计", "视频处理器芯片设计", "边缘计算芯片", "系统级芯片（SoC）设计"],
        "name": "智能终端与边缘SoC芯片设计",
        "definition": "设计面向智能终端、视觉处理和边缘计算场景的应用处理器、视频处理器及SoC芯片产品的企业。",
        "aliases": ["应用处理器", "AP芯片", "视频处理芯片", "边缘计算芯片", "边缘SoC", "终端SoC"],
        "included_activities": ["智能终端应用处理器设计", "视频处理芯片设计", "边缘计算SoC设计", "终端SoC流片和产品化"],
        "excluded_activities": ["终端整机制造", "云端训练芯片设计", "晶圆代工", "通用嵌入式软件开发"],
        "observable_company_features": ["具有处理器或SoC芯片型号", "具有流片或量产记录", "产品面向终端视觉或边缘场景"],
    },
    {
        "sources": ["医疗影像设备", "X射线平板探测器制造"],
        "name": "医疗影像设备与核心探测器制造",
        "definition": "研发和制造医学成像诊断、介入导航设备以及X射线平板探测器等核心成像部件的企业。",
        "aliases": ["医疗影像设备", "医学成像设备", "C形臂", "X射线平板探测器", "影像探测器"],
        "included_activities": ["医疗影像整机研发制造", "介入和术中成像设备制造", "X射线平板探测器制造", "医学成像核心部件制造"],
        "excluded_activities": ["医院影像诊断服务", "医疗影像软件单独开发", "影像设备租赁维修", "通用光电器件制造"],
        "observable_company_features": ["具有医疗影像设备或探测器注册产品", "具备医疗器械生产资质", "开展成像设备或核心部件量产"],
    },
    {
        "sources": ["半导体设备制造", "干式真空泵制造"],
        "name": "半导体设备与核心部件制造",
        "definition": "研发和制造用于晶圆制造、封装测试的专用设备及干式真空泵等关键核心部件的企业。",
        "aliases": ["半导体设备", "晶圆制造设备", "刻蚀设备", "薄膜沉积设备", "干式真空泵", "半导体设备部件"],
        "included_activities": ["晶圆制造专用设备制造", "封装测试专用设备制造", "半导体设备干式真空泵制造", "半导体设备关键部件制造"],
        "excluded_activities": ["晶圆制造", "半导体材料生产", "设备贸易租赁", "非原厂维修", "通用真空设备制造"],
        "observable_company_features": ["具有半导体专用设备或部件型号", "通过晶圆厂或封测厂验证", "具备设备装配测试产线"],
    },
    {
        "sources": ["基因细胞药物研发", "再生医学产品研发"],
        "name": "细胞与基因治疗产品研发制造",
        "definition": "研发、临床转化和生产免疫细胞、干细胞、基因治疗、基因编辑及组织工程产品的企业。",
        "aliases": ["细胞治疗", "基因治疗", "CGT", "干细胞药物", "免疫细胞药物", "再生医学产品"],
        "included_activities": ["免疫细胞和干细胞药物研发生产", "基因治疗和基因编辑药物研发生产", "组织工程和再生医学产品产业化", "细胞基因治疗中试生产"],
        "excluded_activities": ["仅提供细胞存储", "医院临床治疗服务", "普通基因检测", "基础研究未形成产品", "传统化学药生产"],
        "observable_company_features": ["具有细胞或基因治疗研发管线", "获得临床试验许可", "建有GMP中试或生产设施"],
        "supplementary_source_ids": ["beijing_cgt_2024"],
    },
    {
        "sources": ["生物药品制造", "重组蛋白与抗体定制化生产"],
        "name": "生物药与重组蛋白制造",
        "definition": "研发和生产抗体、重组蛋白、疫苗及其他生物技术药物，或提供相应中试和商业化生产服务的企业。",
        "aliases": ["生物药", "抗体药物", "重组蛋白", "生物制剂", "大分子药物", "生物药CDMO"],
        "included_activities": ["抗体药物研发生产", "重组蛋白和多肽生产", "疫苗等生物制剂生产", "生物药中试和商业化CDMO生产"],
        "excluded_activities": ["仅从事早期药物研究", "化学原料药生产", "药品批发零售", "细胞基因治疗产品生产"],
        "observable_company_features": ["具有生物药生产管线或CDMO平台", "具备GMP生物反应器和纯化设施", "拥有生物药注册或商业生产项目"],
        "supplementary_source_ids": ["beijing_biomanufacturing_2025"],
    },
]


REDEFINE = {
    "6G通信技术研发": {
        "name": "6G通信设备与产品研发",
        "definition": "研发6G通信芯片、器件、基站、测试设备、原型系统及通感算智融合通信产品的企业。",
        "aliases": ["6G设备", "6G芯片", "6G原型系统", "通感算智", "6G测试设备"],
        "included_activities": ["6G通信芯片和器件研发", "6G基站与原型系统研发", "6G测试测量设备制造", "通感算智融合通信产品研发"],
        "excluded_activities": ["仅从事6G基础理论研究", "通信运营服务", "传统4G或5G设备经销", "通信工程施工"],
        "observable_company_features": ["具有6G硬件原型或产品", "参与6G设备测试验证", "拥有6G通信产品研发团队或专利"],
    },
    "废旧锂离子电池回收服务": {
        "name": "退役锂电池回收与材料再生",
        "definition": "通过拆解、梯次利用、湿法或火法冶金等工艺处理退役锂电池并生产再生材料的企业。",
        "aliases": ["动力电池回收", "锂电池再生", "黑粉回收", "梯次利用", "电池材料再生"],
        "included_activities": ["退役锂电池拆解处理", "动力电池梯次利用", "锂镍钴等材料湿法或火法回收", "再生电池材料生产"],
        "excluded_activities": ["仅设置电池回收网点", "仅运输或贮存废旧电池", "新电池制造", "普通再生资源贸易"],
        "observable_company_features": ["具有废旧电池处理产线", "具备电池回收或梯次利用资质", "产出黑粉或再生电池材料"],
    },
    "新型储能电站": {
        "name": "新型储能系统与装备制造",
        "definition": "研发和制造电化学、液流、压缩空气等新型储能系统、储能变流器、控制设备及系统集成产品的企业。",
        "aliases": ["储能系统", "储能装备", "储能变流器", "PCS", "储能集成"],
        "included_activities": ["储能系统集成制造", "储能变流器和控制设备制造", "液流或压缩空气等储能装备制造", "储能电池包和机柜制造"],
        "excluded_activities": ["仅投资或运营储能电站", "储能项目施工", "储能电力交易", "动力电池整车应用"],
        "observable_company_features": ["具有储能系统或装备产品型号", "具备储能设备生产线", "能够交付储能系统集成产品"],
    },
    "新能源汽车充换电基础设施与运营": {
        "name": "新能源汽车充换电设备制造",
        "definition": "研发和制造新能源汽车充电桩、充电模块、换电设备及配套功率和控制设备的企业。",
        "aliases": ["充电桩", "充电模块", "换电设备", "超充设备", "充换电设备"],
        "included_activities": ["充电桩和充电模块制造", "超充设备制造", "换电站机电设备制造", "充换电功率和控制设备研发"],
        "excluded_activities": ["仅运营充电站或换电站", "充换电工程施工", "新能源汽车整车制造", "动力电池制造"],
        "observable_company_features": ["具有充换电设备产品和认证", "具备充电模块或整机生产线", "向充换电运营商交付硬件"],
    },
    "智能手机与AIoT平台": {
        "name": "AI智能终端制造",
        "definition": "研发和制造具备端侧人工智能能力的智能手机、平板、AIoT终端及相关智能硬件的企业。",
        "aliases": ["AI手机", "AI终端", "智能手机", "AIoT终端", "端侧AI设备"],
        "included_activities": ["AI手机和平板研发制造", "端侧AI智能硬件制造", "AIoT终端整机研发制造"],
        "excluded_activities": ["仅运营AIoT软件平台", "消费电子零售分销", "通用AI应用开发", "芯片设计"],
        "observable_company_features": ["具有自有AI终端产品型号", "具备终端整机设计或生产能力", "产品具有端侧推理能力"],
    },
    "氢能交通基础设施运营": {
        "name": "氢能装备与关键部件制造",
        "definition": "研发和制造制氢、储氢、运氢、加氢、燃料电池及高压流体系统等氢能装备和关键部件的企业。",
        "aliases": ["制氢设备", "储氢装备", "加氢设备", "燃料电池", "高压氢气设备", "氢能装备"],
        "included_activities": ["制氢装备制造", "储运氢装备制造", "加氢站核心设备制造", "燃料电池系统和关键部件制造", "高压氢气设备制造"],
        "excluded_activities": ["仅运营加氢站", "氢能车辆运营", "氢气贸易", "氢能工程咨询"],
        "observable_company_features": ["具有氢能装备或部件产品", "具备高压流体或燃料电池生产能力", "拥有氢能设备生产基地"],
    },
    "自动驾驶技术": {
        "name": "智能驾驶系统与核心部件",
        "definition": "研发并交付智能驾驶域控制器、感知系统、决策系统、线控执行系统及车载计算平台等产品的企业。",
        "aliases": ["智能驾驶系统", "自动驾驶系统", "智驾域控", "车载计算平台", "线控系统"],
        "included_activities": ["智能驾驶域控制器制造", "车载感知硬件和系统研发", "智能驾驶决策控制系统产品化", "线控制动转向系统制造", "车载计算平台制造"],
        "excluded_activities": ["仅运营Robotaxi", "自动驾驶测试场运营", "仅提供地图或数据标注", "整车制造"],
        "observable_company_features": ["具有量产智能驾驶硬件或系统", "获得车企定点或前装量产订单", "具备车规产品验证记录"],
    },
}


SOURCES = {
    "beijing_industry_2026h1": {
        "title": "2026年上半年北京规模以上工业运行情况",
        "url": "https://jxj.beijing.gov.cn/jxsj/jjyx/202607/t20260723_4780476.html",
        "date": "2026-07-23",
        "source_type": "government",
    },
    "beijing_machine_tools_2026": {
        "title": "顺义打造国家级工业母机创新高地",
        "url": "https://www.beijing.gov.cn/ywdt/gqrd/202605/t20260529_4672671.html",
        "date": "2026-05-29",
        "source_type": "government",
    },
    "beijing_quantum_2026": {
        "title": "量子精密测量公共服务平台在石景山启动",
        "url": "https://kw.beijing.gov.cn/xwdt/kcyx/xwdtyqqy/202606/t20260623_4711442.html",
        "date": "2026-06-23",
        "source_type": "government",
    },
    "beijing_low_altitude": {
        "title": "北京市促进低空经济产业高质量发展行动方案（2024-2027年）",
        "url": "https://fgw.beijing.gov.cn/fgwzwgk/2024zcwj/qtwj/202409/t20240930_3910602.htm",
        "date": "2024-09-30",
        "source_type": "government_policy",
    },
    "beijing_materials_2026": {
        "title": "北京经济技术开发区关于加快新材料产业创新发展的若干措施",
        "url": "https://www.beijing.gov.cn/zhengce/zhengcefagui/202609/t20260914_4863469.html",
        "date": "2026-09-14",
        "source_type": "government_policy",
    },
    "beijing_hydrogen_2026": {
        "title": "海德利森氢能科技产业园项目竣工",
        "url": "https://www.beijing.gov.cn/ywdt/gqrd/202607/t20260702_4743886.html",
        "date": "2026-07-02",
        "source_type": "government",
    },
    "beijing_xr_2026": {
        "title": "北京市智能眼镜产业高质量发展行动方案（2026—2030年）",
        "url": "https://www.beijing.gov.cn/zhengce/zhengcefagui/202604/t20260407_4575325.html",
        "date": "2026-04-07",
        "source_type": "government_policy",
    },
    "beijing_mems_2026": {
        "title": "怀柔科学城共性技术平台赋能企业芯片研发",
        "url": "https://invest.beijing.gov.cn/zwgk/sqgzdt/202605/t20260508_4641269.html",
        "date": "2026-05-08",
        "source_type": "government",
    },
    "beijing_cgt_2024": {
        "title": "北京市加快细胞与基因治疗产业创新发展三年行动方案",
        "url": "https://www.beijing.gov.cn/zhengce/gfxwj/202412/t20241227_3975455.html",
        "date": "2024-12-27",
        "source_type": "government_policy",
    },
    "beijing_biomanufacturing_2025": {
        "title": "北京生物制造中试平台入选国家首批名单",
        "url": "https://kfqgw.beijing.gov.cn/zwgkkfq/ztzl/lqztkfq/lqzx/zxxx/202512/t20251224_4360313.html",
        "date": "2025-12-24",
        "source_type": "government",
    },
    "beijing_alt_protein": {
        "title": "国内首个新蛋白食品科技创新基地落地丰台",
        "url": "https://nyncj.beijing.gov.cn/nyj/snxx/gqxx/543526781/index.html",
        "date": "2025",
        "source_type": "government",
    },
    "beijing_future_industries_2025": {
        "title": "北京市2025年政府工作报告",
        "url": "https://www.beijing.gov.cn/gongkai/jihua/zfgzbg/202501/t20250121_3994245.html",
        "date": "2025-01-21",
        "source_type": "government_policy",
    },
    "beijing_green_economy_2024": {
        "title": "北京市加快建设国际绿色经济标杆城市的实施意见",
        "url": "https://fgw.beijing.gov.cn/gzdt/fgzs/mtbdx/bzwlxw/202410/t20241029_3930477.htm",
        "date": "2024-10-29",
        "source_type": "government_policy",
    },
    "beijing_riscv_2026": {
        "title": "北京RISC-V数字基础设施创新中心正式成立",
        "url": "https://www.beijing.gov.cn/fuwu/lqfw/gggs/202606/t20260618_4706778.html",
        "date": "2026-06-18",
        "source_type": "government",
    },
    "beijing_asic_2026": {
        "title": "国产AI ASIC定制产业协同发展研讨会在北京举办",
        "url": "https://kw.beijing.gov.cn/xwdt/kcyx/kcyxkchd/202606/t20260629_4733708.html",
        "date": "2026-06-29",
        "source_type": "government",
    },
    "beijing_robot_2026": {
        "title": "推动人工智能与先进制造业深度融合",
        "url": "https://jxj.beijing.gov.cn/jxdt/zwyw/202605/t20260519_4656804.html",
        "date": "2026-05-19",
        "source_type": "government",
    },
    "beijing_quantum_task_2025": {
        "title": "北京市量子精密测量创新任务",
        "url": "https://kw.beijing.gov.cn/zwgk/zcwj/202501/W020250127443676836107.pdf",
        "date": "2025-01-27",
        "source_type": "government_policy",
    },
    "beijing_materials_event_2026": {
        "title": "2026北京新材料产业促进会暨京津冀石墨烯产业推介会",
        "url": "https://www.beijing.gov.cn/ywdt/gqrd/202609/t20260918_4869962.html",
        "date": "2026-09-18",
        "source_type": "government",
    },
    "beijing_future_industries_2026": {
        "title": "北京打造未来产业创新高地",
        "url": "https://www.beijing.gov.cn/fuwu/lqfw/gggs/202603/t20260309_4551989.html",
        "date": "2026-03-09",
        "source_type": "government",
    },
    "miit_industry_2026": {
        "title": "工业和信息化部：2025年工业和信息化发展主要目标任务顺利完成",
        "url": "https://www.miit.gov.cn/xwfb/bldhd/art/2026/art_4ba01aa6d2f54ced8ba3490ea4fb52c4.html",
        "date": "2026",
        "source_type": "government",
    },
}


SUPPLEMENT_BY_TRACK = {
    "6G通信设备与产品研发": ["beijing_future_industries_2025"],
    "DRAM存储芯片": ["beijing_industry_2026h1"],
    "GaN功率器件": ["beijing_materials_2026"],
    "MEMS传感器制造": ["beijing_mems_2026"],
    "动力电池与储能": ["beijing_materials_2026"],
    "替代蛋白制造": ["beijing_alt_protein"],
    "氢能装备与关键部件制造": ["beijing_hydrogen_2026"],
    "汽车电子电气架构": ["beijing_green_economy_2024"],
    "智能驾驶系统与核心部件": ["beijing_green_economy_2024"],
    "电池材料制造": ["beijing_materials_2026"],
    "RISC-V处理器核研发": ["beijing_riscv_2026"],
    "芯片定制设计服务": ["beijing_asic_2026"],
    "车规级芯片设计": ["beijing_green_economy_2024"],
    "智能终端与边缘SoC芯片设计": ["beijing_xr_2026"],
    "高端船舶制造": ["miit_industry_2026"],
    "新能源汽车电驱系统制造": ["beijing_green_economy_2024"],
}


ADDITIONS = [
    {
        "name": "工业机器人制造",
        "definition": "研发和制造用于工业生产的多关节机器人、协作机器人、移动操作机器人及控制器、伺服和末端执行器的企业。",
        "aliases": ["工业机器人", "协作机器人", "机械臂", "机器人控制器", "末端执行器"],
        "included_activities": ["工业机器人整机研发制造", "协作机器人制造", "工业机器人控制器和核心部件制造"],
        "excluded_activities": ["仅提供机器人系统集成服务", "消费级机器人销售", "人形机器人整机制造"],
        "observable_company_features": ["具有工业机器人产品型号", "具备机器人整机或核心部件生产能力", "产品用于工厂自动化"],
        "supplementary_source_ids": ["beijing_industry_2026h1", "beijing_robot_2026"],
    },
    {
        "name": "高端工业母机与核心部件制造",
        "definition": "研发和制造高端数控机床、五轴加工中心、数控系统、主轴、丝杠、导轨等工业母机整机和核心部件的企业。",
        "aliases": ["工业母机", "高端数控机床", "五轴加工中心", "数控系统", "机床核心部件"],
        "included_activities": ["高端数控机床研发制造", "数控系统制造", "机床主轴丝杠导轨等核心部件制造"],
        "excluded_activities": ["普通机床经销", "仅提供机床维修", "仅开发通用设计软件"],
        "observable_company_features": ["具有数控机床整机产品", "具备机床核心部件研发生产线", "开展机床样机试制和应用验证"],
        "supplementary_source_ids": ["beijing_machine_tools_2026", "beijing_industry_2026h1"],
    },
    {
        "name": "量子精密测量仪器制造",
        "definition": "研发和制造基于量子效应的磁场、时间频率、重力、光学或其他精密测量仪器及核心器件的企业。",
        "aliases": ["量子精密测量", "量子传感器", "原子磁力计", "量子磁测", "超导探测器"],
        "included_activities": ["量子测量仪器研发制造", "量子传感器及读出系统制造", "量子测量核心器件制造"],
        "excluded_activities": ["纯量子理论研究", "仅提供量子教育培训", "传统测量仪器销售"],
        "observable_company_features": ["具有量子传感或测量产品", "开展量子仪器工程化和中试", "具备量子芯片或探测器研发能力"],
        "supplementary_source_ids": ["beijing_quantum_2026", "beijing_quantum_task_2025"],
    },
    {
        "name": "半导体关键材料制造",
        "definition": "研发和制造半导体衬底、外延片、大硅片、光掩模版、靶材、电子化学品及先进封装材料的企业。",
        "aliases": ["半导体材料", "大硅片", "光掩模版", "溅射靶材", "电子化学品", "封装基板", "ABF膜"],
        "included_activities": ["半导体衬底和外延材料制造", "晶圆制造用材料生产", "先进封装关键材料生产"],
        "excluded_activities": ["普通化工原料贸易", "芯片设计", "半导体设备制造"],
        "observable_company_features": ["产品通过晶圆厂或封测厂验证", "具备电子级材料生产线", "生产半导体专用材料"],
        "supplementary_source_ids": ["beijing_materials_2026", "beijing_materials_event_2026"],
    },
    {
        "name": "高性能纤维与复合材料制造",
        "definition": "研发和制造碳纤维、玄武岩纤维、陶瓷纤维及其高性能复合材料和结构件的企业。",
        "aliases": ["高性能纤维", "碳纤维复合材料", "玄武岩纤维", "陶瓷纤维", "先进复合材料"],
        "included_activities": ["高性能纤维生产", "复合材料制备", "高性能复合材料结构件制造"],
        "excluded_activities": ["普通纺织纤维生产", "仅销售复合材料", "玻璃钢通用制品生产"],
        "observable_company_features": ["具备高性能纤维或复材生产线", "产品用于航空航天、机器人或新能源", "具有材料牌号或验证记录"],
        "supplementary_source_ids": ["beijing_materials_2026", "beijing_materials_event_2026"],
    },
]


def unique(items: list[Any]) -> list[Any]:
    result = []
    seen = set()
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, (dict, list)) else str(item)
        if item not in (None, "") and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def slug(name: str) -> str:
    import hashlib
    return "track_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]


def merge_tracks(source_tracks: list[dict[str, Any]], spec: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(source_tracks[0])
    result["track_id"] = slug(spec["name"])
    result["canonical_key"] = result["track_id"]
    result["name"] = spec["name"]
    result["definition"] = spec["definition"]
    result["aliases"] = spec["aliases"]
    for field in (
        "core_products_services", "core_company_types", "supporting_company_types", "shared_demand_drivers",
        "included_activities", "excluded_activities", "chain_roles", "observable_company_features",
        "supporting_event_ids", "topics", "regions", "uncertainties",
    ):
        result[field] = unique([x for track in source_tracks for x in track.get(field, [])])
    result["event_count"] = len(set(x for track in source_tracks for x in track.get("supporting_event_ids", [])))
    # Source URLs are not present in the dashboard export, so summing counts
    # could double-count the same publication across overlapping tracks.
    result["source_count"] = max(track.get("source_count", 0) for track in source_tracks)
    result["source_track_ids"] = [track["track_id"] for track in source_tracks]
    result["supplementary_source_ids"] = spec.get("supplementary_source_ids", [])
    result["curation_action"] = "MERGE"
    for field in ("included_activities", "excluded_activities", "observable_company_features", "core_products_services"):
        if field in spec:
            result[field] = spec[field]
    return result


def clean_term(value: str) -> str:
    return re.sub(r"[。；，、]+$", "", str(value)).strip()


def compile_classification_fields(track: dict[str, Any]) -> None:
    if not track.get("aliases"):
        shortened = re.sub(r"(研发制造|研发|制造|技术|设计服务|服务)$", "", track["name"])
        track["aliases"] = unique([track["name"], shortened])
    phrases = list(track.get("aliases", [])) + list(track.get("core_products_services", []))
    terms = []
    for phrase in phrases:
        phrase = clean_term(phrase)
        if 2 <= len(phrase) <= 40 and phrase not in terms:
            terms.append(phrase)
    track["positive_terms"] = terms[:30]
    track["negative_terms"] = unique([clean_term(x) for x in track.get("excluded_activities", [])])[:20]
    track["classification_rules"] = {
        "match": "至少存在一项可归属于本赛道核心产品、制造活动或已产业化研发成果的明确证据；仅公司名称、宽泛经营范围或通用技术服务表述不能判为MATCH。",
        "not_match": "企业信息充分，且主营产品或实际经营活动仅属于排除项或其他相邻赛道。",
        "insufficient_evidence": "只出现宽泛关键词、规划性表述或工商模板词，无法确认企业具有相应产品、研发或生产活动。",
        "multi_label_allowed": True,
    }


def addition_to_track(spec: dict[str, Any]) -> dict[str, Any]:
    track_id = slug(spec["name"])
    return {
        "track_id": track_id, "canonical_key": track_id, "name": spec["name"], "aliases": spec["aliases"],
        "definition": spec["definition"], "enterprise_archetype": spec["definition"],
        "core_products_services": spec["aliases"], "core_company_types": [], "supporting_company_types": [],
        "shared_demand_drivers": [], "included_activities": spec["included_activities"],
        "excluded_activities": spec["excluded_activities"], "chain_roles": [],
        "observable_company_features": spec["observable_company_features"], "summary": "", "why_now": "",
        "activity_assessment": {}, "industry_chain_analysis": "", "uncertainties": [], "signal_statistics": {},
        "supporting_event_ids": [], "topics": [], "regions": ["北京市"], "confidence": None,
        "status": "candidate", "heat": "待复核", "event_count": 0, "source_count": len(spec["supplementary_source_ids"]),
        "supplementary_source_ids": spec["supplementary_source_ids"], "source_track_ids": [], "curation_action": "ADD",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed/tracks_curated"))
    parser.add_argument("--review-dir", type=Path, default=Path("outputs/track_curation"))
    args = parser.parse_args()
    original = json.loads(args.input.read_text(encoding="utf-8"))["tracks"]
    by_name = {track["name"]: track for track in original}
    consumed: set[str] = set(DELETE)
    curated: list[dict[str, Any]] = []
    review: list[dict[str, str]] = []

    for spec in MERGES:
        sources = [by_name[name] for name in spec["sources"]]
        curated.append(merge_tracks(sources, spec))
        consumed.update(spec["sources"])
        for name in spec["sources"]:
            review.append({"original_name": name, "action": "MERGE", "result_name": spec["name"], "reason": "合并重叠或易随标准漂移的相邻定义"})

    for track in original:
        name = track["name"]
        if name in consumed:
            if name in DELETE:
                review.append({"original_name": name, "action": "DELETE", "result_name": "", "reason": DELETE[name]})
            continue
        result = deepcopy(track)
        result["source_track_ids"] = [track["track_id"]]
        if name in REDEFINE:
            spec = REDEFINE[name]
            result["name"] = spec["name"]
            result["definition"] = spec["definition"]
            result["aliases"] = spec["aliases"]
            result["track_id"] = slug(spec["name"])
            result["canonical_key"] = result["track_id"]
            for field in ("included_activities", "excluded_activities", "observable_company_features", "core_products_services"):
                if field in spec:
                    result[field] = spec[field]
            result["curation_action"] = "REDEFINE"
            review.append({"original_name": name, "action": "REDEFINE", "result_name": spec["name"], "reason": "将技术、运营或服务表述改为可识别的产品制造企业集合"})
        else:
            result["curation_action"] = "KEEP"
            review.append({"original_name": name, "action": "KEEP", "result_name": name, "reason": "企业集合可独立判断，保留平铺多标签"})
        curated.append(result)

    for spec in ADDITIONS:
        track = addition_to_track(spec)
        curated.append(track)
        review.append({"original_name": "", "action": "ADD", "result_name": spec["name"], "reason": "北京市近期产业化证据明确且原看板缺失"})

    for track in curated:
        # The dashboard's product recommendations were produced without the
        # Lenovo SMB catalog and must not leak into the remapping input.
        track.pop("possible_it_needs", None)
        track.pop("smb_value_analysis", None)
        track["supplementary_source_ids"] = unique(
            track.get("supplementary_source_ids", []) + SUPPLEMENT_BY_TRACK.get(track["name"], [])
        )
        track["legacy_dashboard_status"] = track.get("status")
        track["legacy_dashboard_confidence"] = track.get("confidence")
        track["confidence"] = None
        evidence_sources = track.get("source_count", 0) + len(track["supplementary_source_ids"])
        track["evidence_source_count"] = evidence_sources
        track["evidence_grade"] = "A" if evidence_sources >= 5 else "B" if evidence_sources >= 3 else "C" if evidence_sources >= 2 else "D"
        track["evidence_status"] = "VALIDATED" if evidence_sources >= 2 else "PROVISIONAL"
        compile_classification_fields(track)
    curated.sort(key=lambda item: item["name"])
    names = [track["name"] for track in curated]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate curated track names")
    ids = [track["track_id"] for track in curated]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate curated track IDs")
    required = ("definition", "included_activities", "excluded_activities", "observable_company_features", "positive_terms", "negative_terms")
    for track in curated:
        missing = [field for field in required if not track.get(field)]
        if missing:
            raise ValueError(f"{track['name']}: missing {', '.join(missing)}")
        unknown_sources = set(track["supplementary_source_ids"]) - set(SOURCES)
        if unknown_sources:
            raise ValueError(f"{track['name']}: unknown sources {sorted(unknown_sources)}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.review_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "source_track_count": len(original), "curated_track_count": len(curated),
        "actions": {action: sum(row["action"] == action for row in review) for action in ("KEEP", "REDEFINE", "MERGE", "DELETE", "ADD")},
        "scope": "北京市制造业热门赛道；平铺、多标签；不构建父子层级",
        "product_mapping_status": "pending",
    }
    payload = {"metadata": metadata, "sources": SOURCES, "tracks": curated}
    (args.output_dir / "tracks_classification_ready.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (args.output_dir / "tracks_classification_ready.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for track in curated:
            handle.write(json.dumps(track, ensure_ascii=False, separators=(",", ":")) + "\n")
    csv_fields = (
        "track_id", "name", "curation_action", "definition", "aliases", "included_activities",
        "excluded_activities", "observable_company_features", "positive_terms", "negative_terms",
        "evidence_grade", "evidence_status", "evidence_source_count", "supplementary_source_ids",
    )
    with (args.output_dir / "tracks_classification_ready.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        for track in curated:
            row = {field: track.get(field) for field in csv_fields}
            for field, value in row.items():
                if isinstance(value, (list, dict)):
                    row[field] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            writer.writerow(row)
    with (args.review_dir / "track_decisions.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("original_name", "action", "result_name", "reason"))
        writer.writeheader(); writer.writerows(review)
    (args.review_dir / "supplementary_sources.json").write_text(json.dumps(SOURCES, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
