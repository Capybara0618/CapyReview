# CapyReview Real GitHub PR Context Benchmark

- Cases: 50
- Context budget: 12000 tokens
- Reserved runtime budget: 2500 tokens
- Changed-line coverage: 100.0%
- Budget compliance: 100.0%
- Duplicate changed lines: 0

| Subset | Cases | Compression | Batching | Median batches | Max batches | Cumulative token ratio |
|---|---:|---:|---:|---:|---:|---:|
| Overall | 50 | 18.0% | 12.0% | 1.0 | 5 | 97.4% |
| Natural | 41 | 0.0% | 0.0% | 1 | 1 | 99.9% |
| Stress | 9 | 100.0% | 66.7% | 2 | 5 | 85.5% |

The shared dataset contains real pull requests and human Golden Issues. This
report validates context budgeting and changed-line transport; review quality
is reported separately by the quality Evaluation Harness.
