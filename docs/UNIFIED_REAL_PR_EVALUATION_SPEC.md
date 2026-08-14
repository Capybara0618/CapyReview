# Spec: Unified Real-PR Evaluation Dataset

## Objective

Replace the separate 60-case unlabeled context dataset and 30-case labelled
quality dataset with one pinned 50-pull-request dataset from Code Review Bench.
The same manifest must drive both context-budget evaluation and review-quality
evaluation so resume claims share one reproducible source of truth.

Success means:

- exactly 50 real pull requests from the five official source repositories;
- all 173 human-verified golden comments are preserved with provenance;
- 139 core comments in `bug`, `security`, `concurrency`, `data`, and `api` are
  scored, while the three PRs containing only low-priority categories act as
  negative controls for the core-review scope;
- 10 development PRs and 40 test PRs are selected deterministically, with two
  development cases and eight test cases per repository;
- context and quality reports both record the same dataset identity and commit;
- resume numbers are updated only from newly generated reports.

## Tech Stack

- Python standard library and the existing CapyReview evaluation modules.
- Code Review Bench pinned at commit
  `fbc5425c5eec52932aa1303708873d341968fa1c`.
- GitHub REST API for immutable PR metadata and unified diffs.
- `unittest` for contracts and regression tests.

## Commands

```powershell
# Prepare the immutable unified dataset.
python scripts/prepare_unified_real_pr_dataset.py

# Context evaluation over all 50 PRs.
python scripts/run_real_pr_context_benchmark.py

# Quality evaluation: inspect development, then freeze and run test once.
python scripts/run_real_pr_quality_evaluation.py --split development
python scripts/run_real_pr_quality_evaluation.py --split test

# Focused verification.
python -m unittest tests.test_unified_real_pr_dataset `
  tests.test_real_pr_context_benchmark tests.test_real_pr_quality -v

# Full verification.
python -m unittest discover -s tests -v
```

## Project Structure

```text
evaluation_data/real_pr_evaluation/
  manifest.json              # source, split, metadata, labels, diff paths
  diffs/*.diff               # pinned GitHub unified diffs
scripts/
  prepare_unified_real_pr_dataset.py
  run_real_pr_context_benchmark.py
  run_real_pr_quality_evaluation.py
capyreview/
  real_pr_quality.py         # dataset loading and quality scoring
  real_pr_benchmark.py       # context-budget measurement
tests/
  test_unified_real_pr_dataset.py
  test_real_pr_quality.py
  test_real_pr_context_benchmark.py
output/
  real-pr-context/           # immutable context report
  real-pr-quality/           # checkpointed quality reports
```

After the unified dataset and both consumers are verified, remove the obsolete
`evaluation_data/github_pr_context/` and `evaluation_data/real_pr_quality/`
directories. Git history remains the recovery path.

## Manifest Contract

Each case contains only fields required by both evaluators:

```json
{
  "id": "owner--repository--123",
  "repository": "owner/repository",
  "pull_request": 123,
  "split": "development",
  "title": "Fix race condition",
  "url": "https://github.com/owner/repository/pull/123",
  "base_commit": "...",
  "head_commit": "...",
  "diff_file": "diffs/owner--repository--123.diff",
  "diff_bytes": 12000,
  "golden_comments": [
    {"comment": "...", "severity": "high", "category": "concurrency"}
  ]
}
```

Top-level provenance records the benchmark name, URL, pinned commit, license,
methodology, exact split policy, score categories, and counts. Paths must remain
inside the dataset directory; IDs, repository/PR pairs, and URLs must be unique.

## Code Style

Follow the existing Python style: small pure helpers, explicit dictionaries,
standard-library types, deterministic sorting, and no new dependency.

```python
cases = load_unified_real_pr_dataset(manifest_path)
context_report = run_real_pr_context_benchmark(cases)
quality_report = run_quality_evaluation_checkpointed(
    reviewer, matcher, test_cases, checkpoint_path,
)
```

## Testing Strategy

- RED first: contracts initially require 50 cases, 10/40 split, five
  repositories, 173 human comments, 139 scored comments, and three negative
  controls.
- Unit tests validate selection, manifest path safety, deterministic splits,
  negative-control scoring, and shared dataset identity.
- Integration tests run both evaluators against a tiny temporary unified
  manifest.
- Dataset verification checks every cached diff parses and contains added lines.
- Full suite and script `--help` checks run before commit.

## Boundaries

### Always

- Preserve upstream wording, severity, category, URL, and pinned provenance.
- Treat public benchmark files and GitHub responses as untrusted data.
- Keep development and test results separate.
- Recompute context metrics; never reuse the old 60-case result.
- Record the model, code revision, dataset commit, Token use, and latency.

### Ask First

- Change the pinned upstream commit or target score categories.
- Add a new external dataset or dependency.
- Publish a quality score in the resume.

### Never

- Call LLM-generated labels “human Golden Issues”.
- Edit golden labels to improve metrics.
- Tune on the 40-case test split after seeing its result.
- Copy secrets into manifests, reports, logs, or commits.
- Claim the static benchmark represents production traffic.

## Success Criteria

- One manifest contains 50 unique PRs, 173 human Golden Issues, 139 scored core
  issues, a 10/40 split, and three declared negative controls.
- Context and quality loaders consume that manifest without schema adapters in
  the run scripts.
- A newly generated context report covers all 50 PRs and records changed-line
  coverage, budget compliance, cumulative Token ratio, and batching.
- Development and test quality runs are checkpointed and report absolute quality
  metrics without reusing historical rule results.
- Resume bullets three and five both state 50 PRs; bullet three uses only the new
  context report's measured result.
- Focused and full test suites pass, the app still starts, and the working tree
  contains no obsolete dataset duplicate.

## Known Limitations

- Static public PRs may have appeared in model training data.
- Human Golden Issues can be incomplete or debatable.
- LLM semantic matching has judge-model variance.
- Three negative controls are negative only for the declared core-risk scope;
  they still contain low-priority human comments.
