"""Run CapyReview's deterministic runtime and context engineering benchmarks."""
import argparse
from datetime import datetime, timezone
import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from capyreview.engineering_benchmark import (  # noqa: E402
    markdown_report,
    run_context_stress_benchmark,
    run_fault_injection_benchmark,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        default=os.path.join(ROOT, "output", "engineering-evaluation"),
    )
    args = parser.parse_args()

    faults = run_fault_injection_benchmark()
    context = run_context_stress_benchmark()
    report = {
        "schema_version": 1,
        "fault_injection": faults,
        "context_stress": context,
    }
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = os.path.join(args.output_dir, run_id)
    os.makedirs(run_dir, exist_ok=False)
    json_path = os.path.join(run_dir, "engineering-benchmark-report.json")
    markdown_path = os.path.join(run_dir, "engineering-benchmark-report.md")
    with open(json_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    with open(markdown_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(markdown_report(faults, context))

    print("report:", json_path)
    print(
        "fault recovery=%.1f%% containment=%.1f%% state consistency=%.1f%%"
        % (
            faults["fault_recovery_rate"] * 100,
            faults["fault_containment_rate"] * 100,
            faults["state_consistency_rate"] * 100,
        )
    )
    print(
        "risk retention=%.1f%% budget compliance=%.1f%% token reduction=%.1f%%"
        % (
            context["risk_evidence_retention_rate"] * 100,
            context["budget_compliance_rate"] * 100,
            context["average_token_reduction_rate"] * 100,
        )
    )


if __name__ == "__main__":
    main()
