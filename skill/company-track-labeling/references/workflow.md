# 运行与恢复

先按安装说明诊断依赖、Key 和网络，并确认企业和赛道输入字段。真实运行将企业分类材料发送到用户配置的百炼模型服务；运行前让用户知道使用范围与费用。默认只有初级搜索，高级搜索仅在用户明确选择时开启。

```powershell
python "<Skill目录>/scripts/run_pipeline.py" run --workspace "<工作目录>" --companies "<企业.xlsx>" --tracks "<赛道.jsonl>"
```

保留原程序全部七个生产阶段：

1. 企业分类准备。
2. 赛道召回准备。
3. 企业、赛道语义向量生成或缓存复用。
4. BM25 与 E0_WEIGHTED 固定混合召回。
5. 每个赛道下的一批企业做本地证据核验。
6. 对未决企业做初级联网补档及复判。
7. 在原工作簿末尾追加结果列并保存新文件。

默认 E0_WEIGHTED 权重 `0.40/0.30/0.15/0.15`，Embedding/BM25 融合 `0.75/0.25`，Robust Sigmoid 归一化；候选预算 `2000`、核心预算 `1600`、并发 `8`。入口仅适配路径、凭证和状态，不变更算法、提示词或原程序的缓存行为。

可选参数：`--output`、`--sheet`、`--candidate-budget`、`--core-budget`、`--concurrency`。缩小预算时要满足 `1 <= core_budget <= candidate_budget`。默认输出 `<工作目录>/outputs/<企业文件名>_赛道判断.xlsx`；中间数据保存在 `<工作目录>/work`。

用户明确需要高级搜索时追加 `--advanced-search`。该阶段使用百炼的 Responses API，只升级初级搜索后仍然信息不足的企业；如果没有此类企业，原程序自动跳过。不得为了补标签默认扩大调用范围。

```powershell
python "<Skill目录>/scripts/run_pipeline.py" status --workspace "<工作目录>"
python "<Skill目录>/scripts/run_pipeline.py" resume --workspace "<工作目录>"
```

`resume` 使用已保存的输入与参数重新进入原生产入口，复用符合原规则的向量、核验和检索缓存。已经完成则交付现有产物；普通失败不删除缓存，不默认强制模型调用。不要让两个 Agent 同时运行同一工作目录。

新输入或不同运行参数使用新的工作目录，防止混用状态。交付前检查 Excel 存在、企业行数及原工作表保留、判断列已生成，并读取 `work/pipeline_manifest.json`、检索报告及最终复判报告，报告标签和未决数量。错误退出不能表述为完整成功。
