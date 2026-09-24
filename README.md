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

程序负责数据持久化、任务队列、幂等、失败记录、断点恢复和结果渲染；阿里云百炼模型负责研究规划、联网搜索和复杂语义判断。该系统需要持续、批量运行，不以 Skill 作为主要执行方式。

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
migrations/             PostgreSQL 数据库迁移
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
```

文档入口见 [docs/README.md](docs/README.md)。

## 环境要求

- Python 3.12 或 3.13；
- PostgreSQL 17；
- `uv`，或其他能够安装 `pyproject.toml` 的 Python 包管理工具；
- 阿里云百炼 API Key。

## 初始化

```powershell
Copy-Item .env.example .env
uv sync --extra dev
docker compose up -d postgres
uv run alembic upgrade head
uv run track-insight db-check
```

在 `.env` 中配置：

```env
DASHSCOPE_API_KEY=your-api-key
BAILIAN_MODEL=qwen3.7-flash
BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

运行参数位于 `config/poc.yaml`。`BAILIAN_MODEL` 和 `BAILIAN_BASE_URL` 环境变量会覆盖配置文件中的对应值。

## 一键运行与恢复

使用配置文件中的默认研究范围执行完整流程：

```powershell
uv run track-insight pipeline-run
```

也可以直接指定研究范围：

```powershell
uv run track-insight pipeline-run `
  --region 北京市 `
  --industry 制造业 `
  --start-date 2024-09-16 `
  --end-date 2026-09-16
```

命令会依次完成研究规划、联网搜索、页面获取、事件提取、Topic 构建、赛道生成、赛道分析和看板生成。默认看板写入 `reports/track_dashboard_latest.html`。

运行失败或中断后，直接恢复最近一次未完成流程：

```powershell
uv run track-insight pipeline-run --resume
```

也可以指定研究计划恢复：

```powershell
uv run track-insight pipeline-run --plan-id <plan-id> --resume
```

查看统一流程状态：

```powershell
uv run track-insight pipeline-status
uv run track-insight pipeline-status --plan-id <plan-id>
```

统一入口根据数据库中的任务状态复用已经完成的结果，不会重新执行已经成功且输入未变化的模型任务。页面或模型任务发生可重试错误时，程序会在设定的等待时间内自动继续；超过等待时间后保留恢复状态并退出。

## 分阶段运行

以下命令保留用于单阶段调试、质量检查和问题定位；正常运行优先使用 `pipeline-run`。

### 1. 生成并校验研究计划

```powershell
uv run track-insight research-plan-build
uv run track-insight research-plan-validate <plan-id>
```

也可以在生成计划时指定范围：

```powershell
uv run track-insight research-plan-build `
  --region 北京市 `
  --industry 制造业 `
  --start-date 2024-09-16 `
  --end-date 2026-09-16
```

### 2. 搜索和覆盖检查

```powershell
uv run track-insight search-run <plan-id>
uv run track-insight url-status <plan-id>
```

中断后可使用 `search-run <plan-id> --resume` 继续。

### 3. 获取页面正文并提取事件

```powershell
uv run track-insight page-enqueue <plan-id> --limit 1000
uv run track-insight page-fetch-work --limit 1000
uv run track-insight event-extract-work --limit 1000 --workers 4
uv run track-insight stage2-status <plan-id>
```

页面获取和事件提取使用任务队列。若状态中仍有待处理任务，需要重复执行对应的 `*-work` 命令。

### 4. 生成 Topic 和候选赛道

```powershell
uv run track-insight topic-build <plan-id>
uv run track-insight topic-status <plan-id>
uv run track-insight track-build <plan-id>
uv run track-insight track-analyze <plan-id>
uv run track-insight track-status <plan-id>
```

Topic 构建中断后可使用 `topic-build <plan-id> --resume` 继续。

### 5. 生成看板

```powershell
uv run track-insight dashboard-build <plan-id> `
  --output reports/track_dashboard_latest.html
```

## 运行检查

```powershell
uv run track-insight config-show
uv run track-insight run-list
uv run track-insight run-events <pipeline-run-id>
uv run track-insight event-list <plan-id>
uv run track-insight topic-list <plan-id>
uv run track-insight track-list <plan-id>
```

日志同时写入终端、`logs/track-insight.jsonl` 和数据库 `run_event` 表。

## 测试

```powershell
uv run pytest
uv run ruff check src tests
```

`tests/live` 会调用真实 API 并产生费用，需单独按测试标记执行。
