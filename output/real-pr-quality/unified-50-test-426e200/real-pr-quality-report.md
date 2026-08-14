# CapyReview Real PR Quality Evaluation

- Split: `test`
- Model and semantic matcher: `deepseek-v4-flash`
- Cases: 40 real pull requests
- Human golden issues: 112
- Code revision: `426e20038ad75a0ae96aaf23a5fc28304fa92b26`

| Metric | Result |
|---|---:|
| Precision | 42.9% |
| Recall | 2.7% |
| F1 | 5.0% |
| High-severity recall | 5.9% |
| False positives / PR | 0.10 |
| Execution success | 100.0% |
| Median latency | 68.500s |
| P95 latency | 113.282s |

- Review LLM calls: 187; prompt tokens: 1231693; completion tokens: 112395
- Semantic matcher calls: 6; prompt tokens: 3658; completion tokens: 276

Static real-PR benchmark with human-verified golden issues; not production traffic.
