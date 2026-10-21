# Agent 原生演示

Agent 原生模式只用于小范围效果演示。除非用户特别要求更大范围，主动选择单一区域、单一细分行业、较短时间窗口和少量高质量来源。它没有固定硬上限，但不得暗示具备正式模式的大规模覆盖、历史基线或无人值守能力。

## 1. 创建任务包

```powershell
python scripts/agent_demo.py prepare --workspace <工作目录> --region <区域> --industry <行业> --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD>
```

读取生成的 `agent-demo/task.json`。使用 Agent 当前可用的联网搜索和页面读取能力，优先政府、行业协会、园区、企业公告和权威研究来源。若不能联网，请用户提供材料。

## 2. 生成结果

根据 `task.json` 中的 Schema 写出 `agent-demo/result.json`。每个赛道至少包含一个可访问来源和一段来自该来源的证据摘录。不得把推测写成来源事实。

按正式结构提供 `smb_value_analysis`：每项包含 `workload`、`product_category`、`reason`，最多三项；没有可靠关联时使用空数组。证据中的 `regions` 仅填写材料明确支持的地域，禁止根据研究目标推测。

## 3. 校验并生成看板

```powershell
python scripts/agent_demo.py complete --workspace <工作目录> --result <工作目录>/agent-demo/result.json
```

脚本会验证结果、写入 `state.sqlite3`，并使用与百炼 API 模式相同的正式模板生成 `reports/agent_demo_dashboard.html`。看板保留品牌样式、筛选、详情弹窗和证据原文链接。交付时明确说明这是 Agent 原生演示，范围有限、不代表完整监测或趋势判断。

模板与正式成果原样一致；演示身份、范围和覆盖说明保存在同目录 `agent_demo_scope.json`，交付时必须明确说明。演示结果默认作为候选方向展示，可通过 `status: watchlist` 标记观察项。不要为了适配演示修改正式模板。
