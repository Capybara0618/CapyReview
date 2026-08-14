# CapyReview Real PR Quality Evaluation

- Split: `test`
- Model and semantic matcher: `deepseek-v4-flash`
- Cases: 20 real pull requests
- Human golden issues: 65
- Code revision: `aaf8c69aff9c74a8c7822ea4566bff03b1bcb195`

| Metric | Result |
|---|---:|
| Precision | 33.3% |
| Recall | 1.5% |
| F1 | 2.9% |
| High-severity recall | 3.2% |
| False positives / PR | 0.10 |
| Execution success | 100.0% |
| Median latency | 72.078s |
| P95 latency | 106.688s |

- Review LLM calls: 103; prompt tokens: 683631; completion tokens: 56479
- Semantic matcher calls: 2; prompt tokens: 1485; completion tokens: 146

Static real-PR benchmark with human-verified golden issues; not production traffic.
