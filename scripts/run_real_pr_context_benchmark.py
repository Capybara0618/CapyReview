#!/usr/bin/env python
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capyreview.real_pr_benchmark import run_real_pr_context_benchmark  # noqa: E402


def percent(value):
    return "%.1f%%" % (100 * value)


def markdown(report):
    natural = report["subsets"]["natural"]
    stress = report["subsets"]["stress"]
    lines = [
        "# CapyReview Real GitHub PR Context Benchmark",
        "",
        "- Cases: %d" % report["cases"],
        "- Context budget: %d tokens" % report["context_max_tokens"],
        "- Reserved runtime budget: %d tokens" % report["reserved_tokens"],
        "- Changed-line coverage: %s" % percent(
            report["changed_line_coverage_rate"]
        ),
        "- Budget compliance: %s" % percent(
            report["budget_compliance_rate"]
        ),
        "- Duplicate changed lines: %d" % report["duplicate_changed_lines"],
        "",
        "| Subset | Cases | Compression | Batching | Median batches | Max batches | Cumulative token ratio |",
        "|---|---:|---:|---:|---:|---:|---:|",
        "| Natural | {cases} | {compression} | {batching} | {median} | {maximum} | {ratio} |".format(
            cases=natural["cases"],
            compression=percent(natural["compression_rate"]),
            batching=percent(natural["batch_rate"]),
            median=natural["median_batch_count"],
            maximum=natural["max_batch_count"],
            ratio=percent(natural["average_cumulative_token_ratio"]),
        ),
        "| Stress | {cases} | {compression} | {batching} | {median} | {maximum} | {ratio} |".format(
            cases=stress["cases"],
            compression=percent(stress["compression_rate"]),
            batching=percent(stress["batch_rate"]),
            median=stress["median_batch_count"],
            maximum=stress["max_batch_count"],
            ratio=percent(stress["average_cumulative_token_ratio"]),
        ),
        "",
        "The dataset contains public merged PRs and has no defect labels. These metrics",
        "validate context budgeting and changed-line transport, not review accuracy.",
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        default=str(ROOT / "evaluation_data" / "github_pr_context"),
    )
    parser.add_argument("--max-tokens", type=int, default=12_000)
    parser.add_argument("--reserved-tokens", type=int, default=2_500)
    parser.add_argument("--output")
    args = parser.parse_args()
    output = Path(args.output) if args.output else (
        ROOT / "output" / "real-pr-context" /
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    output.mkdir(parents=True, exist_ok=True)
    report = run_real_pr_context_benchmark(
        Path(args.dataset), args.max_tokens, args.reserved_tokens
    )
    (output / "real-pr-context-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "real-pr-context-report.md").write_text(
        markdown(report), encoding="utf-8"
    )
    print(json.dumps({
        "output": str(output),
        "cases": report["cases"],
        "natural": report["subsets"]["natural"],
        "stress": report["subsets"]["stress"],
        "changed_line_coverage_rate": report["changed_line_coverage_rate"],
        "budget_compliance_rate": report["budget_compliance_rate"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
