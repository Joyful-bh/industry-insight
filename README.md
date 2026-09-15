# 赛道洞察 POC

本仓库当前实现北京地区材料采集、解析和相关性筛选底座，包括：

- PostgreSQL 领域模型和 Alembic 初始迁移；
- 本地内容寻址 BlobStore；
- 基于 PostgreSQL 的持久任务表；
- 配置、结构化日志和 CLI；
- 可配置来源、通用采集框架和站点薄适配器；
- HTML/PDF 正文解析、标准化和模板噪声清理；
- 规则初筛与 LLM 结构化相关性复判；
- 数据模型、配置同步、BlobStore 与任务机制测试。

Event、Topic 和 Track 处理将在后续阶段实现。

## 环境要求

- Python 3.12 或 3.13；
- PostgreSQL 17，建议使用仓库中的 Compose 配置；
- `uv`，或其他能够安装 `pyproject.toml` 的 Python 包管理工具。

## 初始化

```powershell
Copy-Item .env.example .env
uv sync --extra dev
docker compose up -d postgres
uv run alembic upgrade head
uv run track-insight self-check
```

## 常用命令

```powershell
uv run track-insight db-check
uv run track-insight source-validate
uv run track-insight source-sync
uv run track-insight scope-validate
uv run track-insight collect stats_national "/sj/zxfb/20[0-9]{4}/t20[0-9]{6}_[0-9]+[.]html$" --max-items 1
uv run track-insight collect-source stats_national --max-pages 1 --max-items 1
uv run track-insight collect-source beijing_policy --max-pages 1 --max-items 1
uv run track-insight collect-source all --max-pages 10 --max-items 200 --mode backfill
uv run track-insight adapter-audit all --max-pages 3
uv run track-insight blob-put .\example.pdf
uv run track-insight job-enqueue document_parse document <document-id> <input-fingerprint> parser-v1
uv run track-insight job-list
uv run track-insight parse-enqueue
uv run track-insight parse-work --limit 20
uv run track-insight relevance-enqueue --limit 10000
uv run track-insight relevance-work --limit 500
uv run track-insight relevance-status
uv run pytest
uv run ruff check .
```

## 采集安全边界

`collect` 默认串行执行；未配置来源频率时每 5 秒最多请求一次，每个来源最多按其
`retry_policy` 进行有限重试。收到 HTTP 403 或 429 时立即停止当前来源，不绕过验证码，
也不切换代理继续请求。单个响应默认限制为 25 MiB，只接受 HTTP(S) 的 HTML 和 PDF。

`url_pattern` 用来限制列表页中哪些链接属于材料。首次接入网站时应先使用
`--max-items 1` 验证，确认列表规则和访问限制后再逐步扩大采集范围。

配置全部使用 `TRACK_INSIGHT_` 前缀的环境变量。BlobStore 默认写入工作区下的 `data/blob`。

## LLM 相关性复判

在 `.env` 中配置 OpenAI 兼容的 Chat Completions 服务：

```env
TRACK_INSIGHT_LLM_BASE_URL=https://your-llm-api.example.com/v1
TRACK_INSIGHT_LLM_API_KEY=your-api-key
TRACK_INSIGHT_LLM_MODEL=your-model-name
TRACK_INSIGHT_LLM_TIMEOUT_SECONDS=60
TRACK_INSIGHT_LLM_MAX_INPUT_CHARS=12000
```

可使用 `uv run track-insight config-show` 确认模型配置已读取，该命令不会显示 API Key。
