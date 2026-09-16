# 赛道洞察 POC

当前实现 Agent 驱动方案的前两个阶段：

- 根据地区、时间和产业范围生成结构化研究计划；
- 使用阿里云百炼 Responses API 联网搜索发现候选 URL，并进行结构化筛选；
- 保存搜索原始结果、模型判断、URL 发现记录和覆盖审计；
- 支持任务幂等、失败状态和断点恢复。

## 环境要求

- Python 3.12 或 3.13；
- PostgreSQL 17；
- `uv`，或其他能够安装 `pyproject.toml` 的 Python 包管理工具。

## 初始化

```powershell
Copy-Item .env.example .env
uv sync --extra dev
docker compose up -d postgres
uv run alembic upgrade head
```

## 常用命令

```powershell
uv run track-insight db-check
uv run track-insight config-show
uv run track-insight research-plan-build
uv run track-insight research-plan-validate <plan-id>
uv run track-insight search-run <plan-id> --max-work-packages 1 --max-searches 3
uv run track-insight url-status <plan-id>
```

## 阿里云百炼 API

研究规划和搜索使用真实百炼 API，不提供模拟调用模式。在 `.env` 中配置：

```env
DASHSCOPE_API_KEY=your-api-key
BAILIAN_MODEL=qwen3.7-flash
BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

运行日志同时写入终端、`logs/track-insight.jsonl` 和数据库 `run_event` 表。查看批次及事件：

```powershell
uv run track-insight run-list
uv run track-insight run-events <pipeline-run-id>
```

`BAILIAN_MODEL` 会覆盖 `config/poc.yaml` 中的默认模型。联网搜索使用 Responses API，模型必须属于百炼文档列出的 Responses 联网搜索支持范围。若业务空间使用专属接入地址，通过 `BAILIAN_BASE_URL` 配置完整的 OpenAI 兼容基础地址。`config-show` 会显示实际生效的模型，只显示密钥是否已经配置，不显示密钥内容。首次验证应限制为一个
WorkPackage 和三个 SearchTask。搜索返回的全部去重来源都会保留，并按 `review_batch_size` 分批复核；该配置只控制单批大小，不限制来源总数。
