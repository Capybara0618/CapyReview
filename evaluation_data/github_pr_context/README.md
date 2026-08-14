# GitHub PR Context Dataset

This is an unlabeled context-engineering dataset collected from public, merged
pull requests in repositories with MIT, Apache-2.0, or BSD-3-Clause licenses.
It is not ground truth for review precision, recall, or F1.

- Natural samples: 30
- Size-stratified stress samples: 30
- Stress tiers: 8K, 16K, and 32K estimated Diff tokens
- Token estimate: UTF-8 bytes divided by four

`manifest.jsonl` pins repository, pull request, base/head commits, source URL,
language, license, subset, and the corresponding file under `diffs/`.
