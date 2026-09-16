# Agent 驱动的赛道洞察 POC 方案

## 一、目标

本方案建立一条面向指定地区、时间和产业范围的最小赛道洞察链路：

```text
研究范围
→ Agent 拆分研究任务
→ 百炼 Responses API 联网搜索发现候选 URL并结构化筛选
→ 程序去重和调度
→ 静态抓取或百炼网页提取工具取得页面内容
→ 事件提取 Agent 生成事件与证据
→ 事件归并为 Topic
→ Topic 形成候选 Track
→ 候选 Track 解读与分析
→ 输出候选赛道报告
```

POC 输出候选 Track，不执行候选 Track 的准入验证。最终结果由人工审核评价。

## 二、核心概念

### 2.1 Event

Event 是材料中一项可以独立描述的政策或产业事实。一篇页面可以产生零到多条 Event，每条 Event 必须绑定原文证据和来源 URL。

POC 使用六类 Event：

- `policy_support`：政策、规划、资金、申报、试点和认定；
- `project_delivery`：签约、开工、建设、投产、扩建、园区和集群建设；
- `investment_financing`：投资、融资和并购；
- `business_operation`：产品、订单、合作、产能和市场拓展；
- `technology_industrialization`：技术突破、成果转化、标准和产业协作；
- `market_change`：需求、供给、价格、产能和景气变化。

签约、开工、投产等具体动作保存在 `stage` 和 `action` 中。

### 2.2 Topic

Topic 是从多条 Event 中归纳出的产业观察对象，描述反复出现的技术、产品、应用场景、企业经营活动或产业方向。Topic 可以较宽，也可以暂时边界不完整。

### 2.3 候选 Track

候选 Track 是围绕一类企业集合形成的赛道假设：

> 一组具有共同经营活动、可观察企业特征、相对稳定产业链角色，并可能产生相似、可复制 IT 需求的企业集合；同时有持续或多类型外部信号支持。

候选 Track 不是相似 Topic 的简单拼接。Topic 必须先被重新解释为企业经营活动、产业链角色和潜在 IT 需求，再围绕共同企业原型归并。

## 三、人类可读的处理步骤

### 3.1 拆分研究任务

规划 Agent 根据地区、时间、产业范围、信号类型、来源类型和搜索预算，生成若干语义完整的研究任务包。

一个任务包应能独立执行，例如：

> 北京市装备制造领域近 24 个月的签约、开工、投产和扩产事件，重点搜索政府、园区、企业官网和行业媒体。

程序不对地区、产业、信号和来源做无差别笛卡尔积，只执行：

- Schema 校验；
- 重复任务检查；
- 搜索预算检查；
- 来源覆盖检查；
- 必要的按地区有限拆分；
- 调度、重试和进度保存。

### 3.2 搜索候选 URL

规划模型在研究任务包中生成有限数量的查询任务，程序校验后逐个调度。每个查询任务先调用百炼 OpenAI 兼容 Responses API，并启用 `web_search` 工具，从响应的 `web_search_call.action.sources` 取得真实来源 URL；随后调用 Chat Completions 对固定来源集合进行结构化筛选，返回保留结论及理由。该阶段不负责完整阅读页面或提取 Event。

查询任务由程序显式调度，不能把一个宽泛研究范围交给模型自行无限搜索。程序控制查询数量、复核批大小、候选 URL 上限、来源类别覆盖和停止条件；Responses API 返回的全部去重来源均保留并分批复核，不按固定结果条数截断。模型负责理解查询意图以及判断搜索结果是否值得进入后续读取。

搜索结果分为：

- `content_page`：政策正文、新闻、公告、企业动态或 PDF 等具体材料；
- `source_entry`：栏目、站内搜索、RSS、Sitemap 或公开目录等候选来源入口。

主流程只将 `content_page` 加入待阅读队列。`source_entry` 作为来源线索单独保存。

### 3.3 URL 记录与去重

程序分别保存百炼返回的原始 Responses 响应和模型筛选结果，包括搜索词、搜索时间、结果引用、候选 URL、来源域名、筛选结论和理由。

POC 只做 URL 级轻量去重：

- 统一主机名大小写；
- 移除锚点；
- 移除已知追踪参数；
- 合并同一规范化 URL 的多次发现记录。

追加搜索时，搜索结果仍可能出现旧 URL，但已处理 URL 不再进入阅读队列。

### 3.4 分级取得页面内容

候选 URL 去重后，先根据搜索标题、摘要、来源和时间执行高召回轻量预筛。只排除机构职能、领导信息、居民服务、人事任免等明确无关结果；无法确定的候选继续处理。

剩余 URL 依次进入两条读取通道：

```text
普通 HTTP 静态抓取
→ HTML/PDF 轻量文本提取
→ 确定性可读性检查
    ├─ 可读：将本地文本交给百炼模型提取 Event
    └─ 不可读：调用百炼网页提取工具，再由百炼模型提取 Event
```

静态抓取只做允许失败的快速通道，不承担通用网页适配。轻量处理包括：

- 检查 HTTP 状态和 Content-Type；
- 识别登录、验证码、反爬和不存在页面；
- 删除 `script`、`style` 等明确无用节点；
- 提取标题、Meta、可见文本及 PDF 文本；
- 按配置限制交给模型的最大文本长度。

可读性检查只判断抓取结果是否足以供模型阅读，不判断业务相关性。检查项包括有效文本长度、连续段落、脚本空壳、拦截标记和标题一致性。阈值全部配置化。

静态结果使用以下状态：

- `static_usable`：HTML 文本可以交给模型；
- `pdf_usable`：PDF 文本可以交给模型；
- `remote_reader_required`：静态结果为空、动态渲染、被拦截或正文不完整；
- `inaccessible`：静态抓取和百炼网页提取工具均无法取得有效内容。

事件提取 Agent 使用本地文本时，负责识别元信息、判断相关性并抽取 Event。进入 `remote_reader_required` 时，程序调用百炼网页提取工具取得页面内容，再使用同一个事件提取提示词完成相同输出。两条通道共用 `PageReview` 与 `Event` 契约。

Topic 和 Track 只消费结构化 Event，不消费原始 HTML。静态抓取失败后不为普通长尾网站编写专用适配器；PDF 附件下载和重要证据原始文件保存可以复用现有 BlobStore。

### 3.5 合并明显重复的 Event

程序使用主体、动作、对象、地区和大致时间生成重复候选。高度一致的 Event 归入同一个独立事件组，同时保留所有来源。

同一项目的签约、开工和投产是不同阶段 Event，不合并成一个事件。语义不明确的重复候选交给 Agent 判断。POC 只消除明显转载和重复报道。

### 3.6 形成 Topic

Topic 形成采用两条通道：

1. **匹配已有 Topic**：将新 Event 与现有 Topic 定义比较，记录匹配 Topic、置信度和理由；
2. **发现新 Topic**：将无法稳定匹配的 Event 分批交给 Topic Agent，根据产品、技术、应用、企业经营活动和产业链角色分组、命名并生成定义。

Event 不按地区、来源或 Event 类型单独形成 Topic。不同类型的政策、投产、融资和订单 Event 可以共同支持同一个产业 Topic。

事件量较大时采用两层归并：

```text
Event 小批次
→ 局部 Topic
→ 局部 Topic 与代表 Event 汇总
→ 全局 Topic 合并
→ 统一 Topic 注册表
```

单个孤立 Event 作为主题线索保留，不单独生成稳定 Topic。

### 3.7 Topic 形成候选 Track

Track Agent 先将 Topic 转换为可比较的企业视角，包括：

- 产品或服务；
- 应用场景；
- 企业经营活动；
- 产业链角色；
- 代表性企业类型；
- 可能产生的 IT 需求。

Topic 在候选 Track 中分为：

- `core_topics`：直接描述企业产品、服务和经营活动；
- `supporting_topics`：提供政策、市场、技术和应用信号；
- `adjacent_topics`：概念相关但企业集合或产业链角色不同。

Track Agent 围绕共同企业原型归并 Topic。主要判断标准是：

- 是否指向相同或高度重合的企业群体；
- 企业主要经营活动是否一致；
- 产业链角色是否一致或构成同一业务交付；
- 是否可以使用相近的公开特征识别；
- 是否可能产生相似、可复制的 IT 需求。

地域、发布机构、政策名称和 Event 类型不同不单独触发拆分。产品制造商、解决方案商和下游使用企业如果经营活动及 IT 需求明显不同，应形成不同候选或相邻方向。

候选生成阶段执行一次全局合并，消除近义候选及同级父子重复，但不执行候选 Track 准入验证。

### 3.8 候选 Track 解读与分析

每个候选 Track 生成独立分析任务。分析 Agent 读取候选定义、关联 Topic、独立事件组、来源和地区时间分布，输出：

- 赛道定义、纳入范围、排除范围和相邻方向；
- 当前值得关注的政策、项目、企业、技术和市场信号；
- 相关页面、独立事件、独立来源、来源类型和 Event 类型统计；
- 时间分布、主要区域和新增区域；
- 产业链结构、主要企业活动和企业识别特征；
- 从经营活动到 IT 需求及 SMB 覆盖方式的因果链；
- 关键证据和主要不确定性。

缺少可比历史基线时，输出“近期活跃”或“历史不足”，不强行判断升温或降温。

分析材料不足时，可以生成少量定向补充研究任务。补充任务仍按“搜索 URL—去重—阅读—提取 Event”的主流程执行。

## 四、数据契约

所有对象使用稳定 ID，通过 ID 引用，不在下游复制上游事实。时间采用 ISO 8601，枚举使用下文给定值。

通用字段规则：

- ID、对象间引用、来源 URL、处理版本和状态为必填字段；
- 页面发布日期、事件日期、主体、对象、地区和数值无法确认时使用 `null`，不得推断补齐；
- 列表字段没有结果时使用空数组，不使用虚构占位值；
- `source_count` 优先按规范化发布主体计数，发布主体未知时按可注册域名计数；
- 搜索摘要只用于候选发现，只有页面原文片段可以成为 Event 证据。

所有模型产物使用统一信封，便于校验和追溯：

```json
{
  "schema_version": "1.0",
  "processor_version": "page-review-v1",
  "model_run_id": "model_run_01JXYZ",
  "generated_at": "2026-09-16T10:30:00+08:00",
  "data": {}
}
```

下文示例展示 `data` 内的业务对象。

### 4.1 ResearchPlan 与 WorkPackage

```json
{
  "plan_id": "plan_beijing_manufacturing_24m_v1",
  "created_at": "2026-09-16T10:00:00+08:00",
  "scope": {
    "regions": ["北京市"],
    "industry_scopes": ["制造业"],
    "start_date": "2024-09-16",
    "end_date": "2026-09-16"
  },
  "work_packages": [
    {
      "work_package_id": "wp_equipment_projects_beijing",
      "objective": "发现北京装备制造领域的项目落地和产能变化",
      "regions": ["北京市"],
      "industry_scopes": ["装备制造"],
      "signal_types": ["签约", "开工", "投产", "扩产"],
      "source_classes": ["government", "park", "company", "industry_media"],
      "search_queries": [
        {
          "search_task_id": "search_equipment_beijing_01",
          "query": "北京 装备制造 生产线 投产",
          "purpose": "发现装备制造企业生产线投产事件",
          "target_source_classes": ["government", "park", "company", "industry_media"]
        }
      ],
      "max_searches": 20,
      "max_candidates": 100,
      "split_strategy": "none",
      "stop_rule": "budget_or_no_new_candidates"
    }
  ]
}
```

约束：

- `split_strategy` 仅允许 `none`、`per_region`；
- 程序不得对其他字段自动做笛卡尔积；
- `search_queries` 由规划模型生成，程序只做数量、重复、地区、时间和来源覆盖校验；
- 每个 `search_task_id` 只对应一个明确查询、一次百炼 Responses 联网搜索和一次结构化筛选；
- `source_classes` 使用 `government`、`park`、`association`、`company`、`industry_media`、`authoritative_media`、`investment_institution`、`other`。

### 4.2 SearchExecutionResult

```json
{
  "work_package_id": "wp_equipment_projects_beijing",
  "searches": [
    {
      "search_task_id": "search_equipment_beijing_01",
      "query": "北京 装备制造 生产线 投产",
      "searched_at": "2026-09-16T10:10:00+08:00",
      "provider": "bailian",
      "model": "qwen3.7-flash",
      "tool": {
        "type": "web_search",
        "engine": "search_pro",
        "count": 20
      },
      "raw_results": [
        {
          "refer": "ref_1",
          "url": "https://example.com/article/123",
          "title": "某装备制造生产线在京投产",
          "snippet": "该项目位于北京经开区……",
          "source_domain": "example.com",
          "possible_published_at": "2026-08-20"
        }
      ],
      "judgments": [
        {
          "refer": "ref_1",
          "decision": "keep",
          "url_type": "content_page",
          "source_class": "park",
          "candidate_reason": "可能包含制造业项目投产事件"
        }
      ],
      "completion_reason": "result_limit_reached"
    }
  ],
  "completion_reason": "search_budget_reached"
}
```

约束：

- 每个查询任务对应一次启用百炼 Responses `web_search` 工具的调用及一次固定结果复核；
- `raw_results` 原样保存 API 返回的搜索条目，不能只保存模型选中的结果；
- `judgments.refer` 必须引用同次调用的 `raw_results.refer`；
- `decision` 使用 `keep`、`maybe`、`drop`，其中 `keep` 和 `maybe` 进入程序预筛；
- 工作包级 `completion_reason` 使用 `search_budget_reached`、`candidate_budget_reached`、`no_new_candidates`、`blocked`。

程序根据全部 `SearchExecutionResult` 生成 `SearchCoverageAudit`：

```json
{
  "plan_id": "plan_beijing_manufacturing_24m_v1",
  "work_package_count": 8,
  "completed_work_package_count": 8,
  "query_count": 64,
  "candidate_count": 320,
  "new_candidate_count": 214,
  "distinct_domain_count": 47,
  "source_class_counts": {
    "government": 92,
    "park": 44,
    "company": 51,
    "industry_media": 27
  },
  "dominant_domains": [
    {"domain": "example.gov.cn", "candidate_count": 38}
  ],
  "coverage_gaps": [],
  "warnings": []
}
```

覆盖审计只报告缺口和集中度，不自动补造研究任务。需要补充时，将缺口交回规划 Agent 生成新的 WorkPackage。

### 4.3 URLCandidate

```json
{
  "candidate_url_id": "url_01JXYZ",
  "canonical_url": "https://example.com/article/123",
  "url_type": "content_page",
  "title": "某装备制造生产线在京投产",
  "snippet": "该项目位于北京经开区……",
  "source_domain": "example.com",
  "source_class": "park",
  "possible_published_at": "2026-08-20",
  "first_discovered_at": "2026-09-16T10:10:00+08:00",
  "discovered_by": [
    {
      "work_package_id": "wp_equipment_projects_beijing",
      "query": "北京 装备制造 生产线 投产"
    }
  ],
  "prefilter_status": "passed",
  "acquisition_status": "pending",
  "review_status": "pending"
}
```

约束：

- `prefilter_status` 使用 `pending`、`passed`、`excluded`；
- `acquisition_status` 使用 `pending`、`static_usable`、`pdf_usable`、`remote_reader_required`、`inaccessible`；
- `review_status` 使用 `pending`、`queued_for_review`、`reviewed_relevant`、`reviewed_irrelevant`、`reviewed_uncertain`、`access_failed`。

### 4.4 PageAcquisition

```json
{
  "page_acquisition_id": "acq_01JXYZ",
  "candidate_url_id": "url_01JXYZ",
  "attempted_at": "2026-09-16T10:20:00+08:00",
  "http_status": 200,
  "content_type": "text/html",
  "raw_blob_key": "sha256/ab/abcdef",
  "text_blob_key": "sha256/cd/cdef01",
  "extracted_text_chars": 8420,
  "readability_checks": {
    "blocked_marker_found": false,
    "script_shell_detected": false,
    "minimum_text_satisfied": true,
    "coherent_paragraphs_found": true,
    "title_consistent": true
  },
  "acquisition_status": "static_usable",
  "failure_reason": null
}
```

`raw_blob_key` 和 `text_blob_key` 可以为空；当 `acquisition_status` 为 `remote_reader_required` 或 `inaccessible` 时必须记录原因。

### 4.5 PageReview 与 Event

```json
{
  "page_review_id": "review_01JXYZ",
  "candidate_url_id": "url_01JXYZ",
  "page_acquisition_id": "acq_01JXYZ",
  "reading_mode": "local_text",
  "reader_provider": null,
  "access_status": "success",
  "metadata": {
    "title": "某装备制造生产线在京投产",
    "publisher": "北京某产业园",
    "published_at": "2026-08-20",
    "content_type": "html"
  },
  "relevance": "relevant",
  "relevance_reason": "包含制造业企业生产线投产事实",
  "events": [
    {
      "event_id": "evt_01JXYZ",
      "page_review_id": "review_01JXYZ",
      "candidate_url_id": "url_01JXYZ",
      "event_type": "project_delivery",
      "stage": "投产",
      "subject": "某装备制造企业",
      "action": "正式投产",
      "object": "某型装备生产线",
      "industry_objects": ["装备制造", "生产线"],
      "business_activity": "装备研发和制造",
      "chain_role": "装备制造商",
      "region": "北京经开区",
      "event_date": "2026-08-20",
      "amounts": [],
      "evidence_quote": "……某型装备生产线正式投产……",
      "confidence": "high"
    }
  ]
}
```

约束：

- `access_status` 使用 `success`、`partial`、`failed`；
- `reading_mode` 使用 `local_text`、`bailian_web_extractor`；
- `reader_provider` 在 `local_text` 时为空，在远程读取时固定为 `bailian`；
- `relevance` 使用 `relevant`、`irrelevant`、`uncertain`；
- `content_type` 使用 `html`、`pdf`、`other`；
- `confidence` 使用 `high`、`medium`、`low`；
- 每条 Event 必须有非空 `evidence_quote`，并通过 `page_review_id` 追溯到 URL。

### 4.6 IndependentEventGroup

```json
{
  "event_group_id": "ieg_01JXYZ",
  "representative_event_id": "evt_01JXYZ",
  "event_ids": ["evt_01JXYZ", "evt_01JABC"],
  "dedup_basis": {
    "subject": "某装备制造企业",
    "action": "投产",
    "object": "某型装备生产线",
    "region": "北京经开区",
    "time_bucket": "2026-08"
  },
  "source_count": 2
}
```

### 4.7 Topic

```json
{
  "topic_id": "topic_industrial_vision_inspection",
  "name": "工业视觉质检",
  "definition": "围绕工业生产过程中的视觉缺陷检测设备、软件和应用形成的产业主题",
  "aliases": ["智能质检", "机器视觉检测"],
  "keywords": ["视觉检测", "缺陷识别", "质量检测"],
  "products_services": ["视觉检测设备", "缺陷识别软件"],
  "applications": ["生产线质量检测"],
  "business_activities": ["设备研发制造", "质检软件研发", "系统实施"],
  "chain_roles": ["设备供应商", "软件供应商", "解决方案服务商"],
  "event_group_ids": ["ieg_01JXYZ"],
  "representative_event_ids": ["evt_01JXYZ"],
  "independent_event_count": 3,
  "source_count": 3,
  "origin": "new_topic_discovery"
}
```

`origin` 使用 `existing_topic_match`、`new_topic_discovery`、`topic_merge`。

### 4.8 CandidateTrack

```json
{
  "candidate_track_id": "ctrk_industrial_vision_solutions",
  "name": "工业视觉质检设备与解决方案",
  "definition": "从事工业视觉质量检测设备、软件和整体解决方案研发、生产与交付的企业集合",
  "enterprise_archetype": "工业视觉检测设备制造商、质检软件企业和整体解决方案服务商",
  "core_topic_ids": ["topic_industrial_vision_inspection"],
  "supporting_topic_ids": ["topic_manufacturing_quality_digitalization"],
  "adjacent_topic_ids": ["topic_industrial_camera"],
  "included_activities": ["视觉检测设备研发制造", "缺陷识别软件研发", "质检系统集成"],
  "excluded_activities": ["仅使用视觉质检的制造企业", "通用安防摄像头制造"],
  "chain_roles": ["设备供应商", "软件供应商", "解决方案服务商"],
  "observable_company_features": ["产品包含工业视觉检测设备或软件", "具有制造业质检项目案例"],
  "possible_it_needs": ["AI训练与推理算力", "研发工作站", "边缘计算", "项目数据存储"],
  "event_group_ids": ["ieg_01JXYZ"],
  "regions": ["北京市"],
  "status": "candidate"
}
```

### 4.9 CandidateTrackAnalysis

```json
{
  "analysis_id": "analysis_ctrk_industrial_vision_solutions_v1",
  "candidate_track_id": "ctrk_industrial_vision_solutions",
  "summary": "该候选赛道围绕工业生产中的视觉质量检测设备、软件和项目交付形成。",
  "why_now": "近期同时出现技术改造政策、质检项目落地和相关企业业务扩展信号。",
  "signal_statistics": {
    "reviewed_page_count": 8,
    "independent_event_count": 5,
    "source_count": 4,
    "source_classes": ["government", "park", "company"],
    "event_types": ["policy_support", "project_delivery", "business_operation"],
    "regions": ["北京市", "北京经开区"],
    "new_regions": ["北京经开区"]
  },
  "activity_assessment": {
    "label": "recently_active",
    "historical_comparability": false,
    "basis": "近24个月存在多类信号，但缺少可比历史覆盖。"
  },
  "industry_chain_analysis": "候选企业主要位于视觉检测设备、软件和系统交付环节。",
  "smb_value_analysis": [
    {
      "business_activity": "视觉模型研发和设备调试",
      "workload": "图像训练、现场推理和项目数据管理",
      "it_need": "AI工作站、边缘计算设备和数据存储",
      "coverage_path": "面向研发和项目交付团队提供标准化配置及实施服务"
    }
  ],
  "evidence_event_group_ids": ["ieg_01JXYZ"],
  "uncertainties": ["企业规模分布仍需补充", "缺少可比历史覆盖，暂不判断升温幅度"]
}
```

`activity_assessment.label` 使用 `newly_observed`、`recently_active`、`continuously_active`、`insufficient_history`。

## 五、技术实现方案

### 5.1 模块划分

在现有 Python、PostgreSQL、SQLAlchemy、任务表和 LLM 客户端基础上增加：

```text
research_planning      研究计划生成与校验
search_execution       百炼联网搜索调用、结果筛选与留档
url_registry           URL 规范化、去重与阅读队列
content_acquisition    静态抓取、轻量提取与读取路由
page_review            本地文本或百炼网页提取与 Event 提取
event_grouping         明显重复 Event 归组
topic_building         已知 Topic 匹配与新 Topic 发现
track_building         企业原型生成与候选 Track 归并
track_analysis         候选 Track 补充研究与解读
reporting              结构化结果和人类可读报告
```

### 5.2 阿里云百炼统一 API 调用

- 全部规划、搜索、页面审查、Event、Topic、Track 和分析任务使用阿里云百炼 API；
- 共用一个 API Key、Base URL、HTTP 客户端、重试策略和调用日志；
- 普通语义任务调用 Chat Completions；搜索任务调用 Responses API 并启用 `web_search` 工具，来源从 `output[].action.sources` 提取；静态抓取不可用时调用 Responses API 的 `web_extractor` 工具；
- 每类任务使用独立提示词和 Pydantic 输出模型；
- 一次调用只处理一个查询任务或一个有限批次；
- JSON 先做 Schema 校验，格式错误只进行有限修复重试；
- 每次调用保存请求 ID、模型、工具、提示词版本、输入指纹、时间、用量和原始响应；
- 相同输入指纹和处理版本复用已有结果；
- 搜索、阅读、Topic 和 Track 任务均可从数据库状态恢复。

客户端对上层暴露统一接口：

```text
run(task_type, input_data, response_schema, prompt_version, timeout, tools)
→ ModelRunResult
```

`ModelRunResult` 至少包含 `model_run_id`、任务类型、开始和结束时间、请求 ID、状态、模型、工具调用、用量、原始响应、结构化结果及错误代码。

POC 默认模型为 `qwen3.7-flash`，用于研究计划、联网搜索、搜索结果筛选、页面审查、Event 抽取、Topic 和候选 Track 生成。模型路由保持配置化；只有小批量人工评估表明某一阶段质量不足时，才将该阶段切换为百炼平台内支持相应接口的更强模型。联网搜索工具调用单独计入搜索预算。

### 5.3 程序与 Agent 分工

程序负责：

- Schema、枚举和引用完整性；
- URL 规范化与去重；
- 标题摘要预筛、静态抓取和可读性路由；
- 任务调度、批次、预算、重试和恢复；
- 事件、来源、时间和区域统计；
- 对象版本与最终报告组装。

Agent 负责：

- 研究任务的语义拆分；
- 查询词设计和候选 URL 发现；
- 页面阅读、相关性判断和 Event 抽取；
- 模糊重复事件判断；
- Topic 匹配、发现、命名和合并；
- 企业原型、候选 Track 和边界生成；
- 候选 Track 的产业与 SMB 价值解读。

### 5.4 核心政策库

核心政策库可以增加独立的目录同步通道，通过公开 API、Sitemap、RSS、站内搜索或分页目录枚举近 24 个月政策 URL。目录同步只保存 URL、标题、发布日期和发布机构，具体页面继续进入统一 URL 账本和阅读流程。

该通道与开放产业信号搜索相互独立，可以在主链路验证后接入。

### 5.5 持久化与状态流转

新增对象至少映射为以下持久化表：

```text
research_plan
research_work_package
model_run
search_query
search_result
url_candidate
url_discovery
page_acquisition
page_review
event
independent_event_group
independent_event_group_member
topic
topic_event_group
candidate_track
candidate_track_topic
candidate_track_analysis
```

关键唯一约束：

- `url_candidate.canonical_url` 唯一；
- `url_discovery` 按候选 URL、任务包和查询去重；
- 同一候选 URL、读取器版本和输入指纹只生成一份有效 `page_review`；
- 同一 Event、Topic、Track 的下游关系使用关联表，不复制事实；
- 模型产物只有通过 Schema 校验后才能推进下游状态。

所有可执行任务共用状态：

```text
pending → running → completed
                  ├→ failed_retryable → pending
                  └→ failed_terminal
```

每完成一个任务立即提交结果和状态。恢复运行时只领取 `pending` 或可重试失败任务，不依赖对话上下文推断进度。

### 5.6 配置

新增配置至少包括：

- 百炼 API Key、OpenAI 兼容 Base URL、模型路由、超时和最大并发；
- 联网搜索引擎、单次结果数、工具参数和搜索调用预算；
- 百炼网页提取工具开关、超时和最大输入长度；
- 规划任务数、搜索次数和候选 URL 预算；
- 来源类别覆盖要求和集中度告警；
- URL 规范化及追踪参数列表；
- 标题摘要预筛规则；
- 静态抓取超时、响应大小和允许的 Content-Type；
- 可读文本阈值、拦截标记和最大模型输入长度；
- 搜索结果复核、阅读、Topic 和 Track 批次大小；
- 每类任务的提示词版本和输出 Schema 版本。

密钥只通过 `.env` 中的 `DASHSCOPE_API_KEY` 注入；模型可通过 `BAILIAN_MODEL` 覆盖，兼容接口地址可通过 `BAILIAN_BASE_URL` 覆盖，其他工具和预算参数存放在版本化配置文件中。

业务阈值和运行预算不得写死在提示词或代码中。

### 5.7 阶段入口

CLI 至少提供以下可组合命令：

```text
research-plan-build       生成研究计划
research-plan-validate    校验任务覆盖和预算
search-run                执行搜索任务包
url-status                查看候选 URL 和去重统计
acquisition-work          执行静态抓取及读取路由
page-review-work          执行本地文本或百炼网页提取
event-group-build         合并明显重复 Event
topic-build               匹配和发现 Topic
track-build               生成并全局合并候选 Track
track-analyze             生成候选 Track 解读
research-run              按依赖关系执行完整 POC
```

`research-run` 只是阶段编排器；各阶段命令必须能够独立重跑和恢复。

### 5.8 批次建议

- 搜索：一个百炼 Responses 联网调用处理一个查询任务，随后对固定来源集合执行一次结构化筛选；一个 WorkPackage 包含有限数量的查询任务；
- 阅读：每批 5～10 个 URL；
- Topic 局部分组：每批 30～50 个独立事件组；
- Track 局部生成：每批 10～20 个 Topic；
- 全局 Topic 和 Track 合并使用压缩后的定义、企业原型和代表 Event，不重新输入全部网页内容。

批次大小均应配置化，并依据模型上下文和实际质量调整。

## 六、POC 实施顺序

### 阶段一：URL 到 Event

实现研究计划、搜索结果、URL 账本、静态抓取路由、阅读队列和 Event 契约，跑通：

```text
研究范围
→ WorkPackage
→ 候选 URL
→ URL 去重
→ PageAcquisition
→ 本地文本或百炼网页提取
→ PageReview
→ Event
```

### 阶段二：Event 到候选 Track

实现明显重复 Event 归组、Topic 匹配与发现、企业原型生成和候选 Track 归并。

### 阶段三：赛道解读与输出

实现候选 Track 分析、必要的定向补充研究、结构化输出和人类可读报告。

### 阶段四：核心政策目录同步

选取少量高价值政策库，接入公开目录、API、RSS 或 Sitemap，补充稳定的政策 URL 覆盖。

## 七、POC 验收

POC 通过人工抽查验证：

- 规划 Agent 是否生成了语义完整且来源不过度集中的研究任务；
- 百炼原始 Responses 结果和模型筛选结论是否分别留档，并能说明每个候选 URL 的发现过程；
- 已处理 URL 是否不会重复进入阅读队列；
- 静态可读页面是否进入本地文本通道，动态或空壳页面是否进入百炼网页提取通道；
- 静态通道与百炼网页提取通道的处理时间、输入字符数、成功率和事件有效率是否可统计；
- 页面审查产生的 Event 是否由原文片段直接支持；
- 明显转载是否归入同一独立事件组；
- Topic 内 Event 是否具有一致的产业含义；
- 候选 Track 是否对应可理解的企业集合，而非政策工具或泛技术名词；
- 赛道解读是否区分事实、分析和不确定性；
- SMB IT 需求是否能够从企业经营活动和业务负载推导。

最终交付包括研究计划、搜索日志、候选 URL、页面获取记录、页面审查结果、Event、独立事件组、Topic、候选 Track、候选 Track 分析和人类可读候选赛道报告。
