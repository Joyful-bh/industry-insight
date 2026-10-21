# 百炼自动化全流程

## 初始化与诊断

```powershell
python scripts/run_pipeline.py init --workspace <工作目录>
python scripts/run_pipeline.py doctor --workspace <工作目录>
```

诊断必须确认 Python 依赖、SQLite 建表、配置文件和百炼 Key 均可用。

`doctor` 会同时报告百炼端点和公共网页端点的网络可达性。正式运行前再执行一次显式预检：

```powershell
python scripts/run_pipeline.py network-check --workspace <工作目录>
```

若返回码为 `3`，说明当前进程没有所需的外网访问能力。Codex 或 WorkBuddy 处于受限沙箱时，应申请网络权限后重新执行原命令；不要在同一受限环境中反复恢复流水线，否则网页抓取会被误记为来源失败。

## 新研究

```powershell
python scripts/run_pipeline.py run --workspace <工作目录> --region <区域> --industry <行业> --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD>
```

该入口依次完成研究规划、百炼联网搜索、页面抓取与解析、事件抽取、Topic 构建、候选赛道生成、SMB 分析和看板输出。默认看板为 `<工作目录>/reports/track_dashboard_latest.html`。

缩小实验范围时可以降低工作包和候选 URL 数量，但 `budgets.max_searches_per_work_package` 必须不小于 `4`，因为正式覆盖规划需要至少四个互补搜索任务。使用 `demo` 模板即可满足该约束。

小范围 API 实验直接使用内置预算模板，无需手工改 YAML：

```powershell
python scripts/run_pipeline.py run --profile demo --workspace <工作目录> --region <区域> --industry <行业> --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD>
```

`demo` 模板限制为 1 个工作包、每包 4 个搜索任务和 12 个候选 URL。正式研究使用默认的 `standard` 模板。入口会在工作目录记录当前模板，后续 `resume` 默认自动沿用。

真实调用会产生费用。运行前向用户说明将消耗其百炼额度，但不要求重复确认已经明确要求执行的任务。

## 恢复

失败后先运行：

```powershell
python scripts/run_pipeline.py status --workspace <工作目录>
```

若存在可恢复计划：

```powershell
python scripts/run_pipeline.py resume --workspace <工作目录> [--profile demo]
```

不得通过删除 SQLite 数据库来解决普通调用失败。只有 Schema 损坏且用户明确允许放弃历史状态时，才可创建新工作目录。

## 交付

确认 HTML 看板存在且非空，并报告：研究范围、候选来源/事件/Topic/赛道数量、未完成阶段、证据覆盖缺口以及看板路径。API 与 Agent 原生模式使用同一套品牌、筛选、详情和证据模板。不要在最终答复中输出 Key、原始模型响应或冗长运行日志。

## 原程序与正式看板

`runtime/src`、正式配置、提示词、阶段脚本与 `runtime/scripts/templates/smb_dashboard.html` 都来自正式工作区，发布时按字节校验。API 入口先完整调用原程序 `pipeline-run`，随后将已完成的数据库结果交给原版 `scripts.build_smb_dashboard` 的数据组织和渲染函数，输出正式全量看板；这一导出不调用模型，也不修改研究结果。

全部正式 CLI 参数可直接透传：

```powershell
python scripts/run_pipeline.py cli --workspace <工作目录> -- pipeline-run --config-path config/national.yaml
python scripts/run_pipeline.py dashboard --workspace <工作目录>
```

第一条命令忠实执行原 CLI，包括其内置看板；第二条将完成结果输出为正式全量看板。正式阶段脚本也完整包含于 `runtime/scripts`，可使用原脚本参数调用。运行配置、输入状态和模型响应一致时，程序采用相同处理逻辑；独立联网模型运行具有响应随机性，不保证每次文本逐字相同。
