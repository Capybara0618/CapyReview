import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from capyreview.real_pr_benchmark import run_real_pr_context_benchmark


def write_case(
    root: Path, name: str, subset: str, diff: str, size_tier: str = "natural"
):
    diff_path = root / "diffs" / (name + ".diff")
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(diff, encoding="utf-8")
    return {
        "id": name,
        "repository": "org/project",
        "pull_number": 1,
        "url": "https://github.com/org/project/pull/1",
        "base_sha": "base",
        "head_sha": "head",
        "merged_at": "2026-01-01T00:00:00Z",
        "language": "Python",
        "license": "MIT",
        "subset": subset,
        "size_tier": size_tier,
        "diff_path": "diffs/%s.diff" % name,
    }


class RealPullRequestContextBenchmarkTests(unittest.TestCase):
    def test_report_uses_formal_budget_and_preserves_changed_lines(self):
        small = (
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            "@@ -1 +1 @@\n-old = 1\n+new = 2\n"
        )
        body = []
        for index in range(180):
            body.extend([
                " unchanged_%03d = value\n" % index,
                "+changed_%03d = normalize(value)\n" % index,
            ])
        large = (
            "diff --git a/service.py b/service.py\n"
            "--- a/service.py\n+++ b/service.py\n"
            "@@ -1,180 +1,360 @@\n" + "".join(body)
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [
                write_case(root, "natural", "natural", small),
                write_case(root, "stress", "stress", large, "8k"),
            ]
            (root / "manifest.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            report = run_real_pr_context_benchmark(
                root, max_tokens=1_024, reserved_tokens=256
            )

        self.assertEqual(2, report["cases"])
        self.assertEqual(1.0, report["budget_compliance_rate"])
        self.assertEqual(1.0, report["changed_line_coverage_rate"])
        self.assertEqual(0, report["duplicate_changed_lines"])
        self.assertEqual(1, report["subsets"]["natural"]["cases"])
        self.assertEqual(1, report["subsets"]["stress"]["cases"])
        self.assertTrue(any(item["batch_count"] > 1 for item in report["case_results"]))


if __name__ == "__main__":
    unittest.main()
