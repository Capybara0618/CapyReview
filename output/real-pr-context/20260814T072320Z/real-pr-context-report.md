# CapyReview Real GitHub PR Context Benchmark

- Cases: 60
- Context budget: 12000 tokens
- Reserved runtime budget: 2500 tokens
- Changed-line coverage: 100.0%
- Budget compliance: 100.0%
- Duplicate changed lines: 0

| Subset | Cases | Compression | Batching | Median batches | Max batches | Cumulative token ratio |
|---|---:|---:|---:|---:|---:|---:|
| Natural | 30 | 6.7% | 6.7% | 1.0 | 2 | 98.3% |
| Stress | 30 | 70.0% | 60.0% | 2.0 | 4 | 90.4% |

The dataset contains public merged PRs and has no defect labels. These metrics
validate context budgeting and changed-line transport, not review accuracy.
