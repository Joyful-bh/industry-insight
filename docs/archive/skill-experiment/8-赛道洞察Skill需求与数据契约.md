# 赛道洞察 Skill 需求与数据契约

## 一、目标与定位

赛道洞察 Skill 面向联想 SMB 业务人员，围绕用户指定的研究主题、目标区域和时间范围，调研政策、产业、项目、企业、技术及市场信号，识别具有中小企业基础、可复制 IT 需求和销售可覆盖性的重点赛道。

Skill 由 AI Agent 直接执行研究规划、联网搜索、来源判断、事件抽取、主题归纳、赛道形成、补充调研和业务分析。确定性的抓取、正文解析、URL 规范化、证据定位、数据校验、统计计算及报告渲染由 Skill 内脚本完成。

Skill 默认不依赖 PostgreSQL、Docker、Alembic 或单独配置的模型 API。每次调研使用一个自包含的文件工作区保存输入、过程数据、证据、检查点和最终交付物。

固定研究目的是：

> 识别适合联想 SMB 业务的重点赛道。

## 二、范围

### 2.1 包含范围

- 根据研究主题、目标区域和时间范围制定调研计划；
- 搜索并筛选政府、园区、产业组织、企业、投资机构和媒体等公开来源；
- 抓取和解析 HTML、PDF 及用户指定的本地文件；
- 判断材料相关性并抽取具有原文证据的结构化 Event；
- 将 Event 归纳为产业 Topic；
- 将 Topic 组合为具有明确企业集合和经营活动的候选 Track；
- 分析 Track 的近期信号、产业链、企业特征和联想 SMB IT 价值；
- 在来源、证据或产业边界不足时主动补充调研；
- 输出可追溯的结构化文件、Markdown 报告和独立 HTML 看板；
- 在用户选择增量更新时读取已有调研结果并合并新增信号。

### 2.2 不包含范围

- 库外企业名单或销售联系人挖掘；
- CRM 商机创建、商机评分或销售自动触达；
- 面向单个企业的客户标签生产；
- 需要登录、付费订阅或绕过访问控制的数据采集；
- 对来源未明确披露的事实进行推断补齐；
- 将搜索结果摘要、模型常识或无法定位的转述作为正式证据。

## 三、用户输入契约

### 3.1 必填输入

| 字段 | 类型 | 说明 |
|---|---|---|
| `research_topic` | 字符串 | 研究主题或产业范围，例如“制造业”“机器人产业” |
| `target_regions` | 字符串数组 | 目标区域，至少一项，例如 `["北京市"]` |
| `time_range` | 对象 | 明确的开始和结束日期，或可解析的相对时间范围 |

研究目的不由用户填写，固定为“识别适合联想 SMB 业务的重点赛道”。

### 3.2 可选输入

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `specified_sources` | 字符串数组 | `[]` | 用户指定的网站 URL 或本地文件路径 |
| `excluded_directions` | 字符串数组 | `[]` | 明确不纳入研究的方向 |
| `expected_track_count` | 正整数或 `null` | `null` | 期望输出的重点赛道数量，不作为凑数要求 |
| `incremental_update` | 布尔值 | `false` | 是否在已有调研结果上增量更新 |
| `existing_result_path` | 字符串或 `null` | `null` | 增量更新时的已有结果目录 |

### 3.3 输入解释规则

- 用户使用“最近一年”“近两年”等相对时间时，Agent 必须在 `manifest.json` 中写入解析后的绝对日期。
- `incremental_update=true` 时必须能够定位有效的已有结果目录；无法定位时停止增量合并，并向用户说明需要的路径。
- 指定网站或文件是优先研究材料，不代表其内容自动可信或自动相关。
- `expected_track_count` 是结果规模偏好。证据不足时允许少于该数量，不得为满足数量而降低质量门槛。
- 排除方向同时约束搜索、Topic 归纳和 Track 形成；若材料只在证据背景中提及排除方向，可以保留材料，但不得将其形成候选 Track。

### 3.4 标准输入示例

```json
{
  "research_topic": "制造业",
  "target_regions": ["北京市"],
  "time_range": {
    "start_date": "2024-09-20",
    "end_date": "2026-09-20"
  },
  "specified_sources": [],
  "excluded_directions": [],
  "expected_track_count": null,
  "incremental_update": false,
  "existing_result_path": null
}
```

## 四、Agent 工作职责

### 4.1 Agent 负责的工作

- 理解输入并形成研究边界；
- 将研究范围拆分为可执行的研究问题；
- 设计和调整搜索策略；
- 对搜索结果进行相关性和来源类型判断；
- 阅读取得的正文并抽取 Event；
- 判断 Event 之间的重复、关联和产业含义；
- 归纳和修订 Topic；
- 形成和合并候选 Track；
- 判断 Track 粒度、企业集合、产业链角色和 SMB 价值；
- 检查来源覆盖、时间覆盖、证据充分性和不确定性；
- 根据质量缺口发起补充调研；
- 生成 `report.md` 和 `dashboard.html` 所需的完整内容；
- 在每个阶段更新检查点，并在中断后依据检查点继续执行。

### 4.2 脚本负责的工作

- URL 规范化、跟踪参数移除和去重；
- HTTP 获取、主机限速、响应大小限制和公网地址检查；
- HTML 正文清洗和 PDF 文本提取；
- 文件哈希、正文哈希和稳定 ID 生成；
- JSON、JSONL 和字段枚举校验；
- Event 证据在正文中的定位；
- 日期和重要数字的证据覆盖检查；
- Event、Topic、Track 引用完整性检查；
- 来源、事件类型、地区和时间分布统计；
- Markdown 报告和独立 HTML 看板渲染；
- 文件安全写入及工作区完整性检查。

### 4.3 编排原则

Agent 可以根据研究情况调整步骤顺序和搜索方向，但不得绕过质量门槛。推荐的基础循环为：

```text
理解需求
  → 制定研究计划
  → 搜索与筛选来源
  → 批量抓取和解析
  → 抽取并校验 Event
  → 检查来源与证据缺口
  → 必要时补充调研
  → 归纳 Topic
  → 形成候选 Track
  → 分析 SMB 价值
  → 全局质量校验
  → 生成报告和看板
```

补充调研可以发生在 Event、Topic 或 Track 阶段。触发条件包括：

- 必需来源类别缺失或来源过度集中；
- 重点结论仅有一个来源；
- 页面正文不足或关键证据无法定位；
- Topic 的产业边界或事件一致性不清晰；
- Track 无法描述明确的企业集合、经营活动或产业链角色；
- SMB IT 需求缺乏从经营活动到工作负载的推导依据；
- 用户指定的期望赛道数量尚未达到，但仍存在有证据价值的研究空白。

补充调研应针对已识别的缺口，不重复执行没有新增价值的宽泛搜索。连续补充仍无法解决时，保留不确定性或将对象降为观察项。

## 五、结果工作区契约

### 5.1 目录结构

每次新调研创建独立目录；增量更新默认在已有目录中创建新版本，不覆盖旧版正式结果。

```text
results/
└── <研究主题>-<目标区域>-<运行日期>/
    ├── manifest.json
    ├── checkpoint.json
    ├── research-plan.md
    ├── sources.jsonl
    ├── pages/
    │   ├── index.jsonl
    │   ├── <source_id>.md
    │   └── attachments/
    ├── events.jsonl
    ├── topics.json
    ├── tracks.json
    ├── evidence/
    │   └── evidence-index.jsonl
    ├── validation.json
    ├── report.md
    └── dashboard.html
```

`report.md` 和 `dashboard.html` 是面向业务用户的正式交付物。其余文件用于继续执行、数据校验和证据追溯。

### 5.2 通用文件规则

- 所有文本文件使用 UTF-8 编码和 LF 换行。
- JSON 对象字段使用 `snake_case`。
- 日期使用 `YYYY-MM-DD`，时间使用带时区的 ISO 8601 格式。
- 未知的标量值使用 `null`，无结果的集合使用空数组。
- 不得使用“未知”“待补充”等虚构字符串代替 `null`。
- ID 在一个结果工作区内唯一，并在增量更新中保持稳定。
- 推荐 ID 使用对象类型前缀加稳定摘要，例如 `evt_4f92c8d31a7b`。
- JSONL 文件每行必须是一个完整 JSON 对象；已经成功写入的行不得原地修改。
- 正式结果文件写入完成并通过校验后再替换当前版本，避免中断产生半成品。

## 六、运行清单与检查点

### 6.1 `manifest.json`

`manifest.json` 记录本次研究的稳定输入、版本和结果概览。

```json
{
  "schema_version": "track-insight-workspace-v1",
  "skill_version": "track-insight-v1",
  "research_id": "res_20260920_beijing_manufacturing",
  "research_purpose": "识别适合联想 SMB 业务的重点赛道",
  "research_topic": "制造业",
  "target_regions": ["北京市"],
  "start_date": "2024-09-20",
  "end_date": "2026-09-20",
  "specified_sources": [],
  "excluded_directions": [],
  "expected_track_count": null,
  "incremental_update": false,
  "parent_research_id": null,
  "created_at": "2026-09-20T10:00:00+08:00",
  "completed_at": null,
  "status": "running",
  "counts": {
    "sources": 0,
    "usable_pages": 0,
    "events": 0,
    "topics": 0,
    "candidate_tracks": 0,
    "watchlist_tracks": 0
  }
}
```

`status` 取值：

- `running`：正在执行；
- `completed`：正式交付物和全局校验均已完成；
- `incomplete`：存在无法自动解决的必要缺口，但已保留可用结果和原因；
- `failed`：未形成可用结果。

### 6.2 `checkpoint.json`

`checkpoint.json` 是 Agent 的续跑入口，不保存长篇分析，只保存当前状态和下一步动作。

必需字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `current_stage` | 枚举 | 当前工作阶段 |
| `completed_stages` | 字符串数组 | 已通过阶段校验的阶段 |
| `pending_items` | 对象数组 | 尚未完成的具体对象或批次 |
| `quality_gaps` | 对象数组 | 仍需处理的质量缺口 |
| `next_actions` | 字符串数组 | Agent 恢复后优先执行的动作 |
| `updated_at` | 时间 | 最后更新时间 |

`current_stage` 取值：

- `planning`
- `discovery`
- `acquisition`
- `event_extraction`
- `topic_synthesis`
- `track_synthesis`
- `track_analysis`
- `validation`
- `reporting`
- `completed`

## 七、来源与页面数据契约

### 7.1 `sources.jsonl`

每行记录一个规范化来源。相同 `canonical_url` 在同一工作区内只能有一个 Source，但可以记录多个发现方式。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `source_id` | 字符串 | 是 | 稳定来源 ID |
| `url` | 字符串或 `null` | 条件必填 | 原始 URL；本地文件可以为空 |
| `canonical_url` | 字符串或 `null` | 条件必填 | 规范化 URL |
| `local_path` | 字符串或 `null` | 条件必填 | 用户指定本地文件路径 |
| `title` | 字符串或 `null` | 否 | 页面或文件标题 |
| `publisher` | 字符串或 `null` | 否 | 发布主体 |
| `source_domain` | 字符串或 `null` | 否 | 来源域名 |
| `source_class` | 枚举 | 是 | 来源类别 |
| `url_type` | 枚举 | 是 | 内容页或来源入口 |
| `possible_published_at` | 日期或 `null` | 否 | 搜索阶段识别的发布日期 |
| `decision` | 枚举 | 是 | 搜索筛选结论 |
| `decision_reason` | 字符串 | 是 | 判断理由 |
| `discoveries` | 对象数组 | 是 | 查询词、发现时间和研究问题 |
| `acquisition_status` | 枚举 | 是 | 页面取得状态 |
| `page_path` | 字符串或 `null` | 否 | 清洗后正文文件路径 |
| `content_hash` | 字符串或 `null` | 否 | 正文哈希 |
| `error` | 对象或 `null` | 否 | 无法取得时的错误信息 |

`source_class` 取值：

- `government`
- `park`
- `association`
- `company`
- `industry_media`
- `authoritative_media`
- `investment_institution`
- `other`

`url_type` 取值：`content_page`、`source_entry`。

`decision` 取值：`keep`、`maybe`、`drop`。只有 `keep` 和 `maybe` 默认进入正文取得环节。

`acquisition_status` 取值：

- `pending`
- `usable`
- `insufficient`
- `inaccessible`
- `failed`

### 7.2 `pages/index.jsonl`

每行记录一个已尝试取得的页面或文件版本。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `page_id` | 字符串 | 是 | 页面版本 ID |
| `source_id` | 字符串 | 是 | 对应来源 ID |
| `requested_url` | 字符串或 `null` | 否 | 请求 URL |
| `final_url` | 字符串或 `null` | 否 | 重定向后的 URL |
| `content_type` | 枚举 | 是 | `html`、`pdf`、`other` |
| `acquisition_method` | 枚举 | 是 | `http_html`、`http_pdf`、`agent_reader`、`ocr`、`local_file` |
| `http_status` | 整数或 `null` | 否 | HTTP 状态码 |
| `title` | 字符串或 `null` | 否 | 正文标题 |
| `published_at` | 日期或 `null` | 否 | 正文确认的发布日期 |
| `content_path` | 字符串或 `null` | 否 | 清洗后正文路径 |
| `content_chars` | 非负整数 | 是 | 正文字符数 |
| `content_hash` | 字符串或 `null` | 否 | 正文哈希 |
| `quality_status` | 枚举 | 是 | `usable`、`insufficient`、`inaccessible` |
| `quality_reasons` | 字符串数组 | 是 | 质量判断依据 |
| `relevance` | 枚举或 `null` | 否 | `relevant`、`possibly_relevant`、`irrelevant` |
| `document_type` | 枚举或 `null` | 否 | 页面材料类型 |
| `review_reason` | 字符串或 `null` | 否 | 相关性判断依据 |
| `review_regions` | 字符串数组 | 是 | 正文明确涉及的区域 |
| `review_industries` | 字符串数组 | 是 | 正文明确涉及的产业 |
| `content_sufficient` | 布尔值或 `null` | 否 | 正文是否足以进行 Event 抽取 |
| `fetched_at` | 时间或 `null` | 否 | 取得时间 |

正文文件必须保留足以定位 Event 证据的原文，不得只保存摘要。

`document_type` 取值：

- `policy`
- `application_or_funding`
- `recognition_or_list`
- `project`
- `enterprise_news`
- `investment_financing`
- `market_report`
- `park_or_cluster`
- `industry_report`
- `other`

## 八、Event 数据契约

### 8.1 `events.jsonl`

每行是一条能够独立描述的政策或产业事实。一个页面可以产生零到多条 Event。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `event_id` | 字符串 | 是 | 稳定 Event ID |
| `source_id` | 字符串 | 是 | 来源 ID |
| `page_id` | 字符串 | 是 | 页面版本 ID |
| `event_type` | 枚举 | 是 | 事件类型 |
| `event_status` | 枚举 | 是 | 事件所处状态 |
| `title` | 字符串 | 是 | 事件标题 |
| `summary` | 字符串 | 是 | 事件摘要 |
| `signal_date` | 日期或 `null` | 否 | 事件发生或信号日期 |
| `date_precision` | 枚举 | 是 | 日期精度 |
| `regions` | 字符串数组 | 是 | 涉及区域 |
| `industries` | 字符串数组 | 是 | 涉及产业 |
| `industry_objects` | 字符串数组 | 是 | 产品、技术、项目或应用对象 |
| `topic_hint` | 字符串 | 是 | 初步产业主题提示 |
| `entities` | 对象数组 | 是 | 主体名称和类型 |
| `chain_roles` | 字符串数组 | 是 | 产业链角色 |
| `smb_relevance` | 枚举 | 是 | 对 SMB 的相关程度 |
| `smb_reason` | 字符串 | 是 | 相关度理由 |
| `confidence` | 0～1 数值 | 是 | 抽取置信度 |
| `evidence` | 对象数组 | 是 | 一条或多条原文证据 |
| `evidence_coverage` | 对象 | 是 | 主体、动作、日期和数字的证据索引 |
| `event_fingerprint` | 字符串 | 是 | 事件去重指纹 |
| `duplicate_group_id` | 字符串或 `null` | 否 | 跨页面重复事件分组 |

`event_type` 取值：

- `policy_release`
- `application_or_funding`
- `recognition_or_list`
- `project_progress`
- `enterprise_operation`
- `investment_financing`
- `technology_commercialization`
- `market_change`
- `park_or_cluster`
- `other_industry_signal`

`event_status` 取值：

- `published`
- `supported`
- `planned`
- `in_progress`
- `completed`
- `observed`

`date_precision` 取值：`day`、`month`、`year`、`unknown`。

`smb_relevance` 取值：`high`、`medium`、`low`、`unclear`。

### 8.2 Event 证据对象

```json
{
  "evidence_id": "evd_684b7b82e23c",
  "quote": "支持事件事实的页面原文片段",
  "source_id": "src_29d10f52ac3e",
  "page_id": "page_b26db847511a",
  "content_path": "pages/src_29d10f52ac3e.md",
  "start_offset": 1200,
  "end_offset": 1220,
  "evidence_type": "local_text"
}
```

证据规则：

- `quote` 必须能在对应正文中定位；
- 不得将搜索摘要作为 `quote`；
- 无法取得正文的来源不能产生正式 Event；
- Event 的主体和核心动作必须有证据覆盖；
- 摘要中的重要日期和数字必须出现在被标记的证据中；
- 证据无法支持核心事实时丢弃该 Event，而不是降低置信度后保留；
- 同一事实被多个页面报道时保留各来源证据，并使用 `duplicate_group_id` 归组。

## 九、Topic 数据契约

### 9.1 `topics.json`

Topic 是一组具有一致产业含义的 Event 所形成的观察对象。

```json
{
  "schema_version": "topic-v1",
  "topics": [
    {
      "topic_id": "topic_5e201f0fd901",
      "canonical_key": "industrial-robot-integration",
      "label": "工业机器人系统集成",
      "definition": "Topic 的明确产业定义",
      "aliases": [],
      "keywords": [],
      "regions": ["北京市"],
      "industries": ["制造业"],
      "summary": "支持事件反映的主要变化",
      "event_ids": ["evt_4f92c8d31a7b"],
      "first_seen_at": "2024-10-01",
      "latest_seen_at": "2026-08-20",
      "confidence": 0.84,
      "status": "candidate"
    }
  ],
  "unassigned_event_ids": []
}
```

Topic 规则：

- `event_ids` 中的 Event 必须存在并通过 Event 校验；
- Topic 应描述反复出现的产品、技术、应用、经营活动或产业方向；
- 同一 Topic 内的 Event 必须具有一致的产业含义，不能仅因共同出现宽泛词语而归组；
- Topic 名称不得仅使用地区、政策工具或宽泛上位概念；
- 没有适合 Topic 的 Event 可以进入 `unassigned_event_ids`，不得强制归类；
- 增量更新时优先匹配已有 Topic；只有边界确实不同才创建新 Topic；
- Topic 合并或拆分后必须保持 Event 引用完整，并在研究计划中说明判断依据。

## 十、Track 数据契约

### 10.1 `tracks.json`

Track 是经过业务验证的候选赛道，应对应具有共同经营活动、可观察企业特征、相对稳定产业链角色和相似可复制 IT 需求的企业集合。

每条 Track 包含：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `track_id` | 字符串 | 是 | 稳定 Track ID |
| `canonical_key` | 字符串 | 是 | 工作区内唯一规范键 |
| `name` | 字符串 | 是 | 赛道名称 |
| `aliases` | 字符串数组 | 是 | 常见别名 |
| `definition` | 字符串 | 是 | 赛道定义 |
| `topic_memberships` | 对象数组 | 是 | Topic、角色、相关度和理由 |
| `enterprise_archetype` | 字符串 | 是 | 企业集合原型 |
| `core_products_services` | 字符串数组 | 是 | 核心产品和服务 |
| `core_company_types` | 字符串数组 | 是 | 核心企业类型 |
| `supporting_company_types` | 字符串数组 | 是 | 配套企业类型 |
| `shared_demand_drivers` | 字符串数组 | 是 | 共同需求驱动因素 |
| `included_activities` | 字符串数组 | 是 | 纳入的经营活动 |
| `excluded_activities` | 字符串数组 | 是 | 排除的经营活动 |
| `chain_roles` | 字符串数组 | 是 | 产业链角色 |
| `observable_company_features` | 字符串数组 | 是 | 可用于识别企业的公开特征 |
| `possible_it_needs` | 字符串数组 | 是 | 可能的 IT 需求分类 |
| `supporting_event_ids` | 字符串数组 | 是 | 支持 Event |
| `regions` | 字符串数组 | 是 | 信号涉及区域 |
| `granularity_assessment` | 对象 | 是 | 赛道粒度判断 |
| `analysis` | 对象 | 是 | 赛道解读和 SMB 分析 |
| `confidence` | 0～1 数值 | 是 | 综合置信度 |
| `status` | 枚举 | 是 | `candidate` 或 `watchlist` |

`topic_memberships.role` 取值：

- `core`：直接描述企业产品、服务和经营活动；
- `supporting`：提供政策、市场、技术或应用信号；
- `adjacent`：与赛道相关但企业集合或产业链角色不同。

### 10.2 Track 分析对象

`analysis` 至少包含：

| 字段 | 类型 | 说明 |
|---|---|---|
| `summary` | 字符串 | 赛道综合结论 |
| `why_now` | 字符串 | 当前值得关注的原因 |
| `signal_statistics` | 对象 | 独立事件、来源、类型、时间和区域统计 |
| `activity_assessment` | 对象 | 活跃度标签、历史可比性和依据 |
| `industry_chain_analysis` | 字符串 | 产业链及企业参与方式 |
| `smb_value_analysis` | 对象数组 | 经营活动到覆盖路径的因果链 |
| `evidence_event_ids` | 字符串数组 | 分析所使用的 Event |
| `uncertainties` | 字符串数组 | 证据局限和待确认事项 |

`activity_assessment.label` 取值：

- `newly_observed`
- `recently_active`
- `continuously_active`
- `insufficient_history`

每个 `smb_value_analysis` 对象必须包含：

```json
{
  "business_activity": "企业正在进行的经营或生产活动",
  "workload": "该活动产生的人员、数据、计算、协同或设备负载",
  "it_need": "由工作负载推导出的 IT 需求",
  "coverage_path": "联想 SMB 可以识别和覆盖该类客户的方式"
}
```

### 10.3 Track 成立规则

候选 Track 必须同时满足：

- **信号成立**：具有多个有效 Event，且来源不应全部来自同一转载链；
- **边界成立**：具有明确的纳入范围、排除范围和相邻方向；
- **企业集合成立**：能够描述参与企业、经营活动和产业链角色；
- **经营价值成立**：能够形成可解释、可复制的 SMB IT 需求及覆盖路径。

不满足全部条件但具有持续观察价值的对象标记为 `watchlist`。不得为了满足 `expected_track_count` 将观察项提升为 `candidate`。

Track 名称和定义不得仅由目标区域、政策工具、单个项目、单家企业或泛化技术概念构成。

## 十一、证据索引契约

### 11.1 `evidence/evidence-index.jsonl`

证据索引用于从最终结论反向定位至 Event、页面和 URL。

| 字段 | 类型 | 说明 |
|---|---|---|
| `evidence_id` | 字符串 | 证据 ID |
| `event_id` | 字符串 | 所属 Event |
| `source_id` | 字符串 | 来源 ID |
| `page_id` | 字符串 | 页面版本 ID |
| `source_url` | 字符串或 `null` | 来源 URL |
| `content_path` | 字符串 | 本地正文路径 |
| `quote` | 字符串 | 原文片段 |
| `start_offset` | 整数或 `null` | 起始位置 |
| `end_offset` | 整数或 `null` | 结束位置 |
| `quote_hash` | 字符串 | 证据哈希 |

同一个 `evidence_id` 只能指向一个确定的页面版本和原文片段。

## 十二、质量校验契约

### 12.1 阶段校验

#### 来源阶段

- URL 经过规范化并去除已知跟踪参数；
- 同一规范化 URL 不重复建立 Source；
- 搜索结果保留发现查询和筛选理由；
- 来源类别和域名分布可统计；
- 用户指定来源均有处理结果或明确失败原因。

#### 页面阶段

- 正文文件存在且哈希与索引一致；
- 页面标题、发布日期和正文无法确认时使用 `null`；
- 登录页、验证码页、空壳页和错误页不得标为 `usable`；
- 解析失败不得伪装为内容不相关。

#### Event 阶段

- 字段满足枚举、长度和数量约束；
- 每条 Event 至少有一条原文证据；
- 证据能够在对应正文中定位；
- 主体和核心动作有直接证据；
- 重要日期和数字有直接证据；
- 不相关或正文不足的页面不生成 Event；
- 重复报道保留多来源证据，但统计独立事件时按重复组去重。

#### Topic 阶段

- 所有 Event 引用存在；
- Topic 内 Event 产业含义一致；
- 未分配 Event 被明确记录；
- Topic 名称、定义和事件集合相互一致；
- 合并或拆分不会产生丢失引用。

#### Track 阶段

- 所有 Topic 和 Event 引用存在；
- Track 粒度符合企业集合定义；
- 纳入范围、排除范围和相邻方向明确；
- 企业类型、经营活动和产业链角色相互一致；
- SMB IT 需求具有完整因果链；
- 关键判断由 `evidence_event_ids` 支撑；
- 不确定性被显式记录；
- 证据不足的对象标记为 `watchlist`。

### 12.2 全局校验

正式交付前必须检查：

- 工作区所有 JSON 和 JSONL 文件可以解析；
- 所有 ID 唯一，所有引用可解析；
- 文件路径存在且位于当前结果工作区内；
- `manifest.json` 中数量与实际文件一致；
- 每条候选 Track 可以追溯到 Topic、Event、Page 和 Source；
- `report.md` 和 `dashboard.html` 只使用通过校验的数据；
- 报告中的事实、分析判断和不确定性能够区分；
- 不存在将搜索摘要作为正式证据的情况；
- 不存在为了达到期望数量而生成的无充分证据 Track。

### 12.3 `validation.json`

```json
{
  "valid": true,
  "validated_at": "2026-09-20T18:00:00+08:00",
  "checks": [
    {
      "name": "event_evidence_resolvable",
      "status": "passed",
      "checked": 42,
      "failed": 0,
      "details": []
    }
  ],
  "errors": [],
  "warnings": []
}
```

存在引用错误、证据错误或正式 Track 不满足成立条件时，`valid` 必须为 `false`，`manifest.status` 不得设为 `completed`。

## 十三、增量更新契约

增量更新以已有结果目录为基线，必须保留历史可追溯性。

- 读取已有 `manifest.json`、`sources.jsonl`、`events.jsonl`、`topics.json` 和 `tracks.json`；
- 校验已有工作区后再开始增量更新；
- 复用未变化页面的正文和内容哈希；
- 已存在的 Source、Event、Topic 和 Track 在语义未变化时保持原 ID；
- 新来源和新事件追加写入，不覆盖原始记录；
- Topic 或 Track 定义发生变化时生成新版本，并记录上一版本 ID；
- 新 Event 优先匹配已有 Topic 和 Track，再判断是否产生新对象；
- 重新计算时间窗口、独立来源、独立事件和区域变化；
- 新版 `report.md` 和 `dashboard.html` 只展示当前有效结果；
- 旧版结果目录保持不变，以便比较和回退。

## 十四、报告交付契约

### 14.1 `report.md`

报告至少包含：

1. 研究范围和数据截止时间；
2. 调研方法和来源覆盖概览；
3. 重点赛道总览；
4. 每个候选 Track 的定义、边界和企业集合；
5. 当前值得关注的政策、项目、企业、技术和市场信号；
6. 产业链角色和可观察企业特征；
7. SMB IT 需求和覆盖路径；
8. 支撑 Event、来源链接和关键原文证据；
9. 观察项；
10. 主要不确定性和证据局限。

报告不得包含 Agent 的思考过程、调试过程、失败尝试或被放弃的中间方案。

### 14.2 `dashboard.html`

看板必须是可以直接打开的独立 HTML 文件，不依赖数据库或运行中的后端服务。至少展示：

- Track、Topic、Event 和来源数量；
- 候选与观察项状态；
- Track 搜索和筛选；
- Track 定义、企业集合和近期变化；
- 核心产品服务、企业类型和可观察特征；
- 支持 Topic、Event 和来源统计；
- SMB 经营活动、工作负载、IT 需求和覆盖路径；
- 不确定性。

## 十五、现有项目迁移映射

| 现有能力或对象 | 处理方式 | Skill 中的位置 |
|---|---|---|
| ResearchPlan、WorkPackage | 调整后保留 | `research-plan.md`、`manifest.json` |
| SearchTask、SearchResult | 合并简化 | `research-plan.md`、`sources.jsonl.discoveries` |
| UrlCandidate、UrlDiscovery | 调整后保留 | `sources.jsonl` |
| PageCapture | 调整后保留 | `pages/index.jsonl` 和 `pages/*.md` |
| PageAnalysis | 合并简化 | Source/Page 质量字段和 Event 抽取结果 |
| Event、EventEvidence | 基本保留 | `events.jsonl`、`evidence-index.jsonl` |
| Topic、TopicEvent | 基本保留 | `topics.json` |
| CandidateTrack、TrackTopic | 基本保留 | `tracks.json` |
| CandidateTrackAnalysis | 合并进入 Track | `tracks.json[].analysis` |
| URL 规范化 | 保留为确定性脚本 | `scripts/normalize_urls.py` |
| HTML/PDF 抓取和解析 | 保留并简化 | `scripts/fetch_pages.py`、`scripts/extract_content.py` |
| Event 证据校验 | 保留为确定性脚本 | `scripts/validate_workspace.py` |
| 统计计算 | 保留为确定性脚本 | `scripts/validate_workspace.py` 或报告构建脚本 |
| HTML 看板 | 调整为文件输入 | `scripts/build_dashboard.py` 和 `assets/` |
| 百炼客户端和模型提示词调用 | 删除 | 由执行 Skill 的 Agent 直接完成 |
| PipelineRun、Job、任务租约 | 删除 | 使用 `checkpoint.json` |
| ModelRun | 删除 | 保留 Skill 版本和输入输出文件，不记录内部模型调用 |
| SQLAlchemy、PostgreSQL、Alembic | 删除 | 使用自包含结果目录 |

## 十六、Skill 资源结构

```text
.agents/skills/track-insight/
├── SKILL.md
├── agents/
│   └── openai.yaml
├── references/
│   ├── research-method.md
│   ├── event-contract.md
│   ├── topic-contract.md
│   ├── track-contract.md
│   ├── evidence-and-quality.md
│   └── output-contract.md
├── scripts/
│   ├── init_workspace.py
│   ├── normalize_urls.py
│   ├── fetch_pages.py
│   ├── extract_content.py
│   ├── validate_workspace.py
│   └── build_dashboard.py
└── assets/
    └── dashboard-template.html
```

`SKILL.md` 只保存入口、输入解释、总体流程、质量门槛和参考资料路由。详细数据契约和领域规则放入 `references/`，Agent 只在执行相应阶段时读取。重复、确定性且容易出错的工作放入 `scripts/`。

## 十七、黄金测试案例

### 17.1 测试任务

> 调研北京市近两年制造业信号，识别具有中小企业基础、可复制 IT 需求和销售可覆盖性的重点赛道。

标准化输入：

```json
{
  "research_topic": "制造业",
  "target_regions": ["北京市"],
  "time_range": {
    "type": "relative",
    "value": "最近两年"
  },
  "specified_sources": [],
  "excluded_directions": [],
  "expected_track_count": null,
  "incremental_update": false,
  "existing_result_path": null
}
```

### 17.2 验收要求

- 用户只提供必填输入即可开始调研；
- 不要求用户安装或配置数据库；
- 不要求用户提供额外模型 API Key；
- Agent 能够形成研究计划并根据覆盖缺口补充调研；
- 搜索发现、页面正文、Event、Topic 和 Track 全部写入约定目录；
- 每条正式 Event 具有可定位的原文证据；
- 每条候选 Track 对应清晰的企业集合和经营活动；
- 每条候选 Track 形成可解释的 SMB IT 需求因果链；
- 证据不足的方向进入观察项或不确定性，不凑足数量；
- `validation.json.valid` 为 `true`；
- `report.md` 内容完整、可读并可追溯到来源；
- `dashboard.html` 可离线直接打开；
- 中断后能够根据 `checkpoint.json` 继续运行，而不重复处理已完成材料。

## 十八、完成标准

赛道洞察 Skill 的第一版在满足以下条件时完成：

1. 用户只需提供研究主题、目标区域和时间范围即可运行；
2. 指定来源、排除方向、期望赛道数量和增量更新均可选；
3. 整个流程不依赖外部数据库和项目专用模型 API；
4. Agent 能够自主完成研究、判断、补充调研和报告生成；
5. 抓取、解析、证据校验、引用校验和报告渲染具有可重复脚本；
6. 领域数据契约与现有 Event、Topic、Track 语义保持一致；
7. 一次运行产生完整、自包含且可继续执行的结果目录；
8. 黄金测试案例通过全部质量和交付验收要求。
