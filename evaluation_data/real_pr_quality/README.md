# Real PR Quality Evaluation

This directory contains a small, fixed evaluation set for CapyReview. It uses
human-verified golden comments from
[Code Review Bench](https://github.com/withmartian/code-review-benchmark),
pinned at commit `fbc5425c5eec52932aa1303708873d341968fa1c`.

- 10 development PRs: prompt and pipeline inspection only
- 20 test PRs: one frozen final run
- Included categories: API, bug, concurrency, data, and security
- Scoring: semantic one-to-one matching between final Findings and golden issues

The benchmark data is MIT licensed by Martian. The PR diffs retain the licenses
of their source repositories. Results must identify the model and semantic judge
version and must not be presented as production traffic performance.

Regenerate the fixed dataset with:

```powershell
python scripts/prepare_real_pr_quality_dataset.py
```
