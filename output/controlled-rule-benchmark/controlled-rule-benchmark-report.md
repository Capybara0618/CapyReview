# Historical Controlled Rule Benchmark

> Boundary: Offline deterministic rule-coverage benchmark only; not part of the DeepSeek runtime and not evidence of LLM or Multi-Agent improvement.

## Dataset

- Cases: 100 (40 risk, 60 clean)
- Repositories: 10
- Canonical SHA-256: `aea871d1319177c603d2cc261c452b092c07e66e3c8210c84ee8c8b6612ef8e9`
- Upstream commit: `e26148cb0af84b1803177fd2eb8ce968cd3c831a`
- Upstream owner: `God1007`

## Detection results

| Metric | Core rules | Core + context rules | Delta |
|---|---:|---:|---:|
| Precision | 83.3% | 82.5% | -0.8 pp |
| Recall | 62.5% | 82.5% | +20.0 pp |
| F1 | 71.4% | 82.5% | +11.1 pp |
| High-risk recall | 84.2% | 94.7% | +10.5 pp |
| Clean-PR accuracy | 91.7% | 91.7% | +0.0 pp |

Counts: baseline TP/FP/FN = 25/5/15; candidate TP/FP/FN = 33/7/7.

The candidate adds eight deterministic context-sensitive rules to the six core rules. The result measures rule coverage on a synthetic-controlled corpus; it does not measure the current DeepSeek review chain.
