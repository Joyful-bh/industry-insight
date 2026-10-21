# 赛道质量实验归档

Topic 补关联回归入口：`python experiments/track_quality/recover_topics_regression.py`。
固定输入为历史 259 个 Topic，模型为 `deepseek-v4-flash-0731`，并发数为 4；输出在
`data/track_quality/topic_recovery_regression/run_2/`，不修改历史结果或正式数据库。
该回归只做精确身份合并，不包含动态市场粗粒度归并。输出是候选，不是已验证的全国热门赛道。

主流程归并回归入口：`python experiments/track_quality/main_market_regression.py`。
复用上述 259 个 Topic 的候选与辅助关联结果，调用正式动态归并模块，输出在
`data/track_quality/main_market_regression/run_1/`。

历史实验报告：

- `docs/core-supporting-batched-regression-results.md`：确认核心、辅助、排除三类 Topic 和完整输入核算有效；
- `docs/dynamic-market-normalization-experiment-results.md`：记录动态平面市场归并的历史效果及边界风险。

历史 API 输入、响应和结果保存在被 Git 忽略的 `data/track_quality/`，用于复核。

当前正式流程包含候选生成、辅助信号补关联、定义、动态市场目录、分批归属、合并组审核和确定性合并。正式入口为：

```powershell
python -m track_insight pipeline-run
```
