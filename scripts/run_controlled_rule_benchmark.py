"""Reproduce the historical deterministic rule benchmark without using an LLM."""

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.controlled_rule_benchmark import (  # noqa: E402
    run_controlled_rule_benchmark,
    write_controlled_rule_report,
)
from capyreview.evaluation_harness import load_jsonl  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the isolated historical controlled-rule benchmark."
    )
    parser.add_argument(
        "--dataset",
        default=str(ROOT / "evaluation_data" / "pr_diff_100.jsonl"),
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "output" / "controlled-rule-benchmark"),
    )
    args = parser.parse_args()

    report = run_controlled_rule_benchmark(load_jsonl(args.dataset))
    json_path, markdown_path = write_controlled_rule_report(args.output_dir, report)
    baseline = report["baseline"]["metrics"]
    candidate = report["candidate"]["metrics"]
    print(f"json_report={json_path}")
    print(f"markdown_report={markdown_path}")
    print(
        "baseline_f1={:.1f}% candidate_f1={:.1f}% "
        "baseline_high_risk_recall={:.1f}% candidate_high_risk_recall={:.1f}% "
        "clean_pr_accuracy={:.1f}%".format(
            baseline["f1"] * 100,
            candidate["f1"] * 100,
            baseline["high_risk_recall"] * 100,
            candidate["high_risk_recall"] * 100,
            candidate["clean_accuracy"] * 100,
        )
    )


if __name__ == "__main__":
    main()
