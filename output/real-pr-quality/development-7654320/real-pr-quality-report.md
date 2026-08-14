# CapyReview Real PR Quality Evaluation

- Split: `development`
- Model and semantic matcher: `deepseek-v4-flash`
- Cases: 10 real pull requests
- Human golden issues: 27
- Code revision: `76543208607dc613130b5f0d67ed43d64f14395d`

| Metric | Result |
|---|---:|
| Precision | 33.3% |
| Recall | 14.8% |
| F1 | 20.5% |
| High-severity recall | 13.3% |
| False positives / PR | 0.80 |
| Execution success | 100.0% |
| Median latency | 89.688s |
| P95 latency | 98.657s |

- Review LLM calls: 54; prompt tokens: 255095; completion tokens: 32979
- Semantic matcher calls: 7; prompt tokens: 4381; completion tokens: 576

Static real-PR benchmark with human-verified golden issues; not production traffic.
