# CapyReview 工程能力基准报告

## Runtime 故障注入

- 样本：50 条（可恢复 40 条，预期终止并隔离 10 条）
- 可恢复故障恢复率：100.0%
- 预期终止故障隔离率：100.0%
- 状态一致率：100.0%
- Trace 完整率：100.0%
- 重复副作用：0 次

故障类型覆盖瞬时节点失败、工具参数错误、Checkpoint 断点恢复、重复投递以及执行预算耗尽。恢复率仅以可恢复的前四类 40 条为分母；预算耗尽的 10 条按是否正确停止并保存已完成状态计算隔离率。

## 细粒度 Agent 恢复

- 样本：30 条（Agent Loop Observation、Reviewer Final、Judge Decision 各 10 条）
- 恢复成功率：100.0%
- 状态一致率：100.0%
- Trace 完整率：100.0%
- 重复 LLM 调用：0 次

该组测试验证工具 Observation 后续跑、Reviewer 最终结果复用和 Judge 决策复用；它不宣称生成中 Token 级恢复。

## 大 Diff 上下文压力

- 样本：30 条（medium/large/xlarge 各 10 条）
- 压缩触发率：100.0%
- Token 预算满足率：100.0%
- 规则层完整保留率：100.0%
- 风险证据保留率：100.0%
- 平均 Token 缩减：95.0%

所有结果均由 `scripts/run_engineering_benchmarks.py` 本地可复现生成。
