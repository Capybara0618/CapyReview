# Unified Real-PR Evaluation Dataset

This directory is the single source of truth for CapyReview context and quality
evaluation. It contains the fixed 50-PR offline corpus from
[Code Review Bench](https://github.com/withmartian/code-review-benchmark), pinned
at commit `fbc5425c5eec52932aa1303708873d341968fa1c`.

- 50 real pull requests from five open-source projects;
- 173 human-verified Golden Issues;
- 139 core issues scored as bug, security, concurrency, data, or API defects;
- 3 PRs with only non-core comments used as negative controls;
- 10 development cases and 40 frozen test cases;
- one manifest and one cached unified Diff per PR.

Regenerate the dataset with:

```powershell
python scripts/prepare_unified_real_pr_dataset.py
```

The static public corpus may have appeared in model training data. Human Golden
Issues can also be incomplete or debatable. Results must record the dataset
commit and must not be presented as production traffic performance.
