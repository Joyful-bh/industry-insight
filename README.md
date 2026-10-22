# 赛道发现 POC

本项目面向联想 SMB 业务：根据指定的地区、时间范围和行业方向，从政策、产业、项目、企业及市场信息中提取可追溯的产业事件，归纳产业主题并形成候选赛道，为后续企业—赛道归类系统提供赛道数据。

核心处理链路已经实现并完成过全流程运行。统一入口会自动衔接各阶段、等待任务队列完成，并将进度和失败位置保存到数据库；运行中断后可以从同一研究计划继续。

## 处理流程

```text
研究范围
→ 生成研究计划
→ 联网搜索并筛选候选网页
→ 抓取和解析正文
→ 提取带原文证据的产业事件
→ 聚合产业 Topic
→ 生成并合并候选赛道
→ 完成赛道边界、企业特征和 SMB 价值分析
→ 生成业务看板
```

程序负责数据持久化、任务队列、幂等、失败记录、断点恢复和结果渲染；阿里云百炼模型负责研究规划、联网搜索和复杂语义判断。`skill/track-insight/` 提供面向 Codex 和 WorkBuddy 的完整 Agent 调用入口。

## 当前验证结果

最近一次完整测试范围为北京市制造业，时间窗口为 2024-09-16 至 2026-09-16：

| 结果 | 数量 |
| --- | ---: |
| 产业事件 Event | 400 |
| 产业主题 Topic | 259 |
| 候选赛道 | 53 |
| 观察中赛道 | 40 |
| 赛道总数 | 93 |
| 已完成分析的赛道 | 93 |

最新结果见 [reports/track_dashboard_latest.html](reports/track_dashboard_latest.html)。

## 目录结构

```text
config/                 运行参数与各阶段提示词
data/                   抓取的原始内容和本地运行数据（不提交 Git）
docs/
  current/              当前实施方案
  reference/            业务参考资料
  archive/              早期方案和历史验证记录
logs/                   当前运行日志及历史日志归档
migrations/             早期数据库版本迁移记录
reports/                最新看板及历史报告归档
src/track_insight/
  planning/             研究计划
  discovery/            联网搜索、URL 管理与覆盖检查
  content/              页面获取、正文解析与质量判断
  events/               产业事件提取
  topics/               Topic 聚合
  tracks/               赛道生成、合并与分析
  reporting/            看板生成
  infrastructure/       数据库、任务、日志和百炼客户端
tests/                   单元、集成和真实 API 测试
skill/track-insight/     可独立分发的 Agent Skill 与运行程序
```

文档入口见 [docs/README.md](docs/README.md)。

## 环境要求

- Python 3.12 或 3.13；
- 支持 `pip` 的 Python 环境；
- 阿里云百炼 API Key。

## 初始化

```powershell
Copy-Item .env.example .env
python -m pip install -e ".[dev]"
python -m track_insight db-init
python -m track_insight db-check
```

在 `.env` 中配置：

```env
DASHSCOPE_API_KEY=your-api-key
BAILIAN_MODEL=qwen3.7-flash
BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

运行参数位于 `config/poc.yaml`。`BAILIAN_MODEL` 和 `BAILIAN_BASE_URL` 环境变量会覆盖配置文件中的对应值。

`stage4.target_track_boundary` 定义正式赛道所对应的目标企业范围。Track 生成、降级归并和最终定义都会收到当前研究计划的 `industry_scopes` 及这条边界；修改边界会改变构建指纹，不会复用其他范围下的赛道结果。

正式赛道构建在候选生成、辅助信号补关联和定义之后，执行动态粗粒度归并：生成本轮平面市场目录、分批归属、审核合并组，再由程序合并。目录随本轮输入生成，不依赖固定赛道清单；不同供给角色不合并，拒绝的组保留原候选，核心和辅助 Topic 均完整保留。

全国采集配置为 `config/national.yaml`，时间窗口为 2025-10-09 至 2026-10-09。
“全国”展开为国家级和中国大陆31个省级地区，基础规划按每批4个地区并发生成，每地区4至6条查询。
程序统一查询地区与时间，并校验独立地区任务、政策来源任务和预算；规划不合格时最多修复一次。
搜索结束后统计各地区任务状态、保留正文链接及已知发布日期分布，最多补查8个薄弱地区、每地区2条新查询、仅一轮。
查询地区标签表示调查目标，不证明网页事实发生在该地区；未知日期也不视为时间覆盖达标。失败或预算停止的任务会单独统计，不以补查掩盖。

赛道质量实验的保留结论与回归入口位于 `experiments/track_quality/`。回归脚本不接入正式 `pipeline-run`，动态归并使用正式 `tracks.markets` 模块。

## 一键运行与恢复

使用配置文件中的默认研究范围执行完整流程：

```powershell
python -m track_insight pipeline-run
```

也可以直接指定研究范围：

```powershell
python -m track_insight pipeline-run `
  --region 北京市 `
  --industry 制造业 `
  --start-date 2024-09-16 `
  --end-date 2026-09-16
```

命令会依次完成研究规划、联网搜索、页面获取、事件提取、Topic 构建、赛道生成、赛道分析和看板生成。默认看板写入 `reports/track_dashboard_latest.html`。

运行失败或中断后，直接恢复最近一次未完成流程：

```powershell
python -m track_insight pipeline-run --resume
```

也可以指定研究计划恢复：

```powershell
python -m track_insight pipeline-run --plan-id <plan-id> --resume
```

查看统一流程状态：

```powershell
python -m track_insight pipeline-status
python -m track_insight pipeline-status --plan-id <plan-id>
```

统一入口根据数据库中的任务状态复用已经完成的结果，不会重新执行已经成功且输入未变化的模型任务。页面或模型任务发生可重试错误时，程序会在设定的等待时间内自动继续；超过等待时间后保留恢复状态并退出。

## 分阶段运行

以下命令保留用于单阶段调试、质量检查和问题定位；正常运行优先使用 `pipeline-run`。

### 1. 生成并校验研究计划

```powershell
python -m track_insight research-plan-build
python -m track_insight research-plan-validate <plan-id>
```

也可以在生成计划时指定范围：

```powershell
python -m track_insight research-plan-build `
  --region 北京市 `
  --industry 制造业 `
  --start-date 2024-09-16 `
  --end-date 2026-09-16
```

### 2. 搜索和覆盖检查

```powershell
python -m track_insight search-run <plan-id>
python -m track_insight url-status <plan-id>
```

中断后可使用 `search-run <plan-id> --resume` 继续。

搜索使用 `bailian.max_concurrency` 控制工作包并发数（全国配置为4）。每个工作包内的查询仍串行执行，独立数据库会话保存结果；设为1可串行运行。修改配置后需重新启动命令才会生效。

默认使用百炼联网搜索，`search.selection: citations` 由程序收集综述引用的 URL，
直接作为待核验候选交给页面下载、清洗及事件抽取，不调用独立 URL 审核模型。
引用只表示候选选择，不表示网页相关性或日期已核验。所有召回 URL 和原始综述仍保存到数据库。
`search.selection: model_review` 可启用逐网页审核。搜索和模型调用统一使用阿里云百炼。

只运行全国搜索阶段的小批任务：

```powershell
python -m track_insight search-run <plan-id> --config-path config/national.yaml --max-work-packages 6 --max-searches 1
```

`max-searches` 限制每个工作包检查的任务数，已完成任务会跳过。此命令仅召回并选择候选网页，
正文获取由后续 `page-enqueue` 和 `page-fetch-work` 执行。

### 3. 获取页面正文并提取事件

```powershell
python -m track_insight page-enqueue <plan-id> --limit 1000
python -m track_insight page-fetch-work --limit 1000
python -m track_insight event-extract-work --limit 1000 --workers 4
python -m track_insight stage2-status <plan-id>
```

页面获取和事件提取使用任务队列。若状态中仍有待处理任务，需要重复执行对应的 `*-work` 命令。

### 4. 生成 Topic 和候选赛道

```powershell
python -m track_insight topic-build <plan-id>
python -m track_insight topic-status <plan-id>
python -m track_insight direction-build <plan-id>
python -m track_insight direction-status <plan-id>
python -m track_insight track-build <plan-id>
python -m track_insight track-analyze <plan-id>
python -m track_insight track-status <plan-id>
```

`direction-build` 是新版 Topic→细分方向阶段：使用带重叠的语义分组，分组内由模型归并 Topic，最后只对跨组的明显重复方向做一次收口。该阶段当前独立运行，尚未替换旧 `track-build`。

Topic 构建中断后可使用 `topic-build <plan-id> --resume` 继续。

### 5. 生成看板

```powershell
python -m track_insight dashboard-build <plan-id> `
  --output reports/track_dashboard_latest.html
```

## 运行检查

```powershell
python -m track_insight config-show
python -m track_insight run-list
python -m track_insight run-events <pipeline-run-id>
python -m track_insight event-list <plan-id>
python -m track_insight topic-list <plan-id>
python -m track_insight track-list <plan-id>
```

日志同时写入终端、`logs/track-insight.jsonl` 和数据库 `run_event` 表。

## 测试

```powershell
python -m pytest
python -m ruff check src tests
```

`tests/live` 会调用真实 API 并产生费用，需单独按测试标记执行。
