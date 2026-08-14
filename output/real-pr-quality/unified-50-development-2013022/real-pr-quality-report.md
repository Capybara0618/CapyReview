# CapyReview Real PR Quality Evaluation

- Split: `development`
- Model and semantic matcher: `deepseek-v4-flash`
- Cases: 10 real pull requests
- Human golden issues: 27
- Code revision: `2013022f006c5069baafe5b0841dcafa44647a37`

| Metric | Result |
|---|---:|
| Precision | 16.7% |
| Recall | 3.7% |
| F1 | 6.1% |
| High-severity recall | 6.7% |
| False positives / PR | 0.50 |
| Execution success | 100.0% |
| Median latency | 60.430s |
| P95 latency | 85.438s |

- Review LLM calls: 40; prompt tokens: 201183; completion tokens: 24230
- Semantic matcher calls: 3; prompt tokens: 1831; completion tokens: 191

Static real-PR benchmark with human-verified golden issues; not production traffic.
