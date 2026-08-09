# CapyReview 单系统 LLM Evaluation 规范

## 目标

使用 DeepSeek 官方 API 对完整 CapyReview 工作流运行一次可审计的 100 条评测，回答：

- 当前冻结代码在受控 Diff 上能够发现多少已标注风险；
- 最终 Finding 的精确率、证据有效率和干净样本表现如何；
- API 或 Runtime 失败是否被如实计入结果；
- Risk Router、Evidence Validator、Judge 和去重漏斗是否留下完整记录。

该协议只评测 CapyReview 自身，不设置其他评测对象。

## 被评测系统

评测装配必须与应用核心拓扑一致：

```text
Risk Router
   ├─ routine: Correctness Reviewer
   └─ high risk: Security Reviewer + Correctness Reviewer
                         │
                         ▼
              Evidence Validator
                         │
                         ▼
              Independent LLM Judge
                         │
                         ▼
                deduplicated findings
```

固定要求：

- Provider：DeepSeek 官方 API；
- Base URL：`https://api.deepseek.com`；
- Model：由 `DEEPSEEK_MODEL` 指定，默认 `deepseek-chat`；
- Reviewer：Security 与 Correctness 两个 OpenAI-compatible Reviewer；
- Judge：独立 OpenAI-compatible LLM Judge；
- Temperature：`0`；
- 输出：JSON object；
- Evidence Validator、匹配器和指标计算保持确定性；
- 不允许在评测过程中改变 Prompt、路由规则、数据标签或计分逻辑。

## 数据集

正式数据文件：`evaluation_data/pr_diff_100.jsonl`

冻结构成：

- 100 条受控合成 PR Diff；
- 40 条风险样本；
- 60 条干净样本；
- 10 个仓库；
- Validation 80 条；
- Holdout 20 条；
- 当前 SHA-256：
  `aea871d1319177c603d2cc261c452b092c07e66e3c8210c84ee8c8b6612ef8e9`。

每条记录包含 repository、pull request、unified diff、expected findings、split 与来源信息。
正式运行前脚本会检查总数必须为 100、风险数必须为 40；不满足时直接终止。

数据来源标记为 `synthetic-controlled`。结果只能说明当前代码、Prompt、模型和冻结数据下的
表现，不能外推为真实生产 PR 的泛化能力。

## 指标

### 检出指标

- **Precision**：`TP / (TP + FP)`；
- **Recall**：`TP / (TP + FN)`；
- **F1**：Precision 与 Recall 的调和平均；
- **Severity accuracy**：匹配 Finding 中严重级别达到标注要求的比例；
- **High-risk recall**：高严重级别标注被正确命中的比例；
- **Clean-PR accuracy**：干净样本没有最终 Finding 的比例。

### 工程指标

- **Execution success rate**：没有 API、解析或 Runtime 错误的案例比例；
- **Evidence validity**：最终预测中 path、line 与 evidence 能对应新增行的比例；
- **Candidate filter rate**：Reviewer 候选经 Evidence、Judge 与根因去重后被过滤的比例；
- **Evidence/Judge rejection rate**：分别记录两个门禁的拒绝比例；
- **Duration**：完整运行耗时。

Candidate filter rate 只描述漏斗行为，不能单独解释为误报下降。最终效果必须结合 Precision、
Recall、干净样本准确率和逐案例错误一起判断。

## 匹配与失败计分

- 预测与标注采用一对一匹配，重复预测只能命中一次；
- path 与新增行位置必须匹配，存在 rule/CWE 标签时还必须匹配风险类别；
- 错误类别、重复 Finding 或未匹配 Finding 计为 FP；
- 未命中的标注计为 FN；
- 风险案例执行失败时，全部未命中风险继续计入 FN；
- 干净案例执行失败时，不能计为正确的空结果；
- 不允许手工删除失败案例或只重跑表现不佳的个别样本。

## 运行命令

项目根目录 `.env` 必须包含：

```env
DEEPSEEK_API_KEY=你的官方DeepSeekAPIKey
DEEPSEEK_MODEL=deepseek-chat
```

先执行两条样本预检：

```powershell
python scripts/run_llm_evaluation.py --smoke
```

预检应覆盖一条风险样本和一条干净样本，用来检查网络、模型名称、JSON 响应、Reviewer、Judge
和报告写入链路。预检结果不能作为正式指标。

正式运行：

```powershell
python scripts/run_llm_evaluation.py
```

若进程中断，必须指定原输出目录恢复：

```powershell
python scripts/run_llm_evaluation.py `
  --output-dir output/llm-evaluation/<run-id> `
  --resume
```

恢复时会校验 Provider、Model、数据指纹、源码指纹与配置指纹；任一字段变化都会拒绝续跑。

## 输出产物

正式运行使用新的不可覆盖目录：

```text
output/llm-evaluation/<UTC时间>-<model>/
├── experiment.json
├── case-results.jsonl
├── llm-evaluation-report.json
└── llm-evaluation-report.md
```

- `experiment.json`：Provider、Model、数据/源码/配置/Prompt 指纹；
- `case-results.jsonl`：每完成一条立即落盘，支持断点续跑；
- JSON 报告：完整机器可读结果、分区指标、逐案例结果和边界；
- Markdown 报告：面试与人工复核所需的核心指标摘要。

API 的 `/api/evaluation` 只展示案例数完整、数据指纹一致的报告。

## 测试

评测契约测试：

```powershell
python -m unittest tests.test_llm_evaluation -v
```

全量回归：

```powershell
python -m unittest discover -s tests -v
```

测试应覆盖：

- DeepSeek-only 装配；
- Risk Router、Reviewer 与 Judge 角色；
- 新增行 Evidence 校验；
- 一对一匹配与失败分母；
- Checkpoint 结果去重；
- Manifest 与数据指纹校验；
- schema v2 单系统报告；
- API 不读取不完整报告。

## 结果使用规则

可以使用：

- 由当前代码、当前 Prompt、当前数据指纹生成的完整 schema v2 正式报告；
- 100 条数据构成、工程故障注入和上下文压力测试结果；
- 报告中明确列出的 Precision、Recall、F1、高风险召回、干净样本准确率、证据有效率和
  执行成功率。

不可使用：

- Smoke 结果或未完成的部分结果；
- 修改标签、删除失败案例或选择性重跑后的数字；
- 旧协议生成的历史报告；
- 将受控合成数据表述为真实生产效果；
- 旧版规则对比协议产生的数字。它们不是当前 DeepSeek LLM 工作流的评测结果，不能用于
  CapyReview 简历或面试。

## 完成标准

- 预检的风险与干净样本均完成；
- 正式输出包含且仅包含 100 个唯一 Case ID；
- 所有失败保留在计分分母；
- Manifest、结果和数据集 SHA-256 一致；
- JSON 与 Markdown 报告成功生成且不包含 API Key；
- 全量自动化测试通过；
- 简历只引用当前完整报告中能够准确解释的数据。
