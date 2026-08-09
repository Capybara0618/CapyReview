# 历史受控规则基准

## 目的与边界

该基准用于复现 CapyReview 历史材料中的以下数字：

- F1：71.4% → 82.5%；
- 高风险召回率：84.2% → 94.7%；
- 干净 PR 准确率：91.7%。

它是一个完全离线、确定性的规则覆盖实验，不调用 DeepSeek，不进入生产 `ReviewService`，
也不证明当前 LLM 或 Multi-Agent 链路带来了上述提升。生产系统仍然保持 LLM-only。

规则与数据生成逻辑还原自历史上游 commit
`e26148cb0af84b1803177fd2eb8ce968cd3c831a`（上游所有者：
[God1007](https://github.com/God1007)）。

## 实验对象

### 基线：6 条核心规则

- `SEC-EVAL`：`eval/exec` 动态执行；
- `SEC-SUBPROCESS-SHELL`：`shell=True`；
- `SEC-HARDCODED-SECRET`：硬编码凭据；
- `SEC-SQL-CONCAT`：动态拼接 SQL；
- `REL-EMPTY-EXCEPT`：宽泛异常捕获；
- `REL-DEBUG-PRINT`：调试输出。

### 候选：核心规则 + 8 条上下文规则

- `SEC-PATH-TRAVERSAL`：路径穿越；
- `SEC-YAML-LOAD`：不安全 YAML 反序列化；
- `SEC-WEAK-HASH`：MD5 弱哈希；
- `SEC-INSECURE-TEMPFILE`：不安全临时文件；
- `SEC-WEAK-RANDOM`：弱随机数；
- `REL-UNBOUNDED-RETRY`：无限重试；
- `SEC-ASSERT-AUTH`：使用 `assert` 做鉴权；
- `SEC-INSECURE-COOKIE`：不安全 Cookie。

候选只是两个规则集合的确定性合并，不是当前生产 `MultiAgentCoordinator`。

## 数据与匹配

- 100 条 `synthetic-controlled` PR Diff；
- 40 条风险、60 条干净；
- 10 个受控仓库；
- 预测与标注按路径、行号区间和 CWE 一对一匹配；
- 重复预测只能命中一次；
- 数据集规范化 SHA-256：
  `aea871d1319177c603d2cc261c452b092c07e66e3c8210c84ee8c8b6612ef8e9`。

运行器会冻结上述样本数、构成、仓库数和指纹；删减案例或修改标签会直接失败。

## 结果是如何得到的

| 指标 | 6 条核心规则 | 核心 + 上下文规则 |
|---|---:|---:|
| TP / FP / FN | 25 / 5 / 15 | 33 / 7 / 7 |
| Precision | 83.3% | 82.5% |
| Recall | 62.5% | 82.5% |
| F1 | 71.4% | 82.5% |
| 高风险召回率 | 84.2% | 94.7% |
| 干净 PR 准确率 | 91.7% | 91.7% |

加入 8 条规则后多命中 8 个风险案例，同时新增 2 个 FP，因此 Recall 明显提高，Precision
从 83.3% 略降至 82.5%，最终 F1 从 71.4% 提高到 82.5%。

高风险样本共 19 条：基线命中 16 条，候选命中 18 条，因此高风险召回率分别为
`16/19 = 84.2%` 与 `18/19 = 94.7%`。候选仍漏掉 `SEC-PICKLE-LOAD`。

60 条干净样本中有 5 条被报告，因此干净 PR 准确率为 `55/60 = 91.7%`。上下文规则新增的
2 个 MD5 FP 位于已经被核心规则误报的同一批 PR 中，所以 FP 数增加，但干净 PR 准确率不变。

## 运行与验证

```powershell
python scripts/run_controlled_rule_benchmark.py
```

默认输出：

```text
output/controlled-rule-benchmark/
├── controlled-rule-benchmark-report.json
└── controlled-rule-benchmark-report.md
```

运行聚焦测试：

```powershell
python -m unittest tests.test_controlled_rule_benchmark -v
```

面试时应将其称为“受控离线规则覆盖实验”或“历史确定性基准”，不能称为 DeepSeek
Multi-Agent 的对比实验。
