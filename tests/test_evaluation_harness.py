from pathlib import Path
import unittest

from capyreview.evaluation_harness import (
    EndToEndEvaluationHarness,
    dataset_fingerprint,
    load_jsonl,
    one_to_one_match,
)
from capyreview.models import Finding, Severity


DATASET = Path(__file__).resolve().parents[1] / "evaluation_data" / "pr_diff_100.jsonl"


class EndToEndEvaluationTests(unittest.TestCase):
    def test_evaluation_aggregates_review_funnel_metrics_without_repair(self):
        class FunnelReviewer:
            name = "funnel-reviewer"

            def review(self, _diff, _parsed):
                return []

            def collaboration_summary(self, _task_id):
                return {"review_funnel": {
                    "reviewer_candidates": 5,
                    "evidence_rejected": 1,
                    "judge_rejected": 1,
                    "approved_before_dedup": 3,
                    "duplicates_merged": 1,
                    "final_findings": 2,
                }}

        case = {
            "id": "clean-1", "repository": "org/repo", "pull_request": 1,
            "split": "validation", "expected_findings": [],
            "diff": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
            "source": {"kind": "unit-test"},
        }

        metrics = EndToEndEvaluationHarness().run(FunnelReviewer(), [case])["metrics"]

        self.assertEqual(5, metrics["reviewer_candidates"])
        self.assertEqual(1, metrics["evidence_rejected"])
        self.assertEqual(1, metrics["judge_rejected"])
        self.assertEqual(1, metrics["duplicates_merged"])
        self.assertEqual(2, metrics["final_findings"])
        self.assertEqual(0.6, metrics["candidate_filter_rate"])
        self.assertNotIn("safe_fix_rate", metrics)
        self.assertNotIn("e2e_security_fix_rate", metrics)

    def test_frozen_dataset_has_expected_shape_and_repository_split(self):
        cases = load_jsonl(str(DATASET))
        self.assertEqual(100, len(cases))
        self.assertEqual(40, sum(bool(item["expected_findings"]) for item in cases))
        self.assertEqual(60, sum(not item["expected_findings"] for item in cases))
        validation_repos = {
            item["repository"] for item in cases if item["split"] == "validation"
        }
        holdout_repos = {
            item["repository"] for item in cases if item["split"] == "holdout"
        }
        self.assertEqual(8, len(validation_repos))
        self.assertEqual(2, len(holdout_repos))
        self.assertFalse(validation_repos & holdout_repos)
        self.assertEqual(
            {"synthetic-controlled"},
            {item["source"]["kind"] for item in cases},
        )

    def test_one_to_one_matching_counts_duplicate_prediction_once(self):
        expected = [{
            "path": "src/a.py", "start_line": 10, "end_line": 12,
            "cwe": "CWE-95", "severity": "critical",
        }]
        predicted = [
            Finding(
                "SEC-EVAL", Severity.CRITICAL, "a", "long enough explanation",
                "src/a.py", line, "eval(x)", "replace eval safely",
                "add malicious input test", 0.9,
            )
            for line in (10, 11)
        ]
        self.assertEqual(1, len(one_to_one_match(expected, predicted)))

    def test_frozen_dataset_fingerprint_is_stable(self):
        first = load_jsonl(str(DATASET))
        second = load_jsonl(str(DATASET))
        self.assertEqual(dataset_fingerprint(first), dataset_fingerprint(second))


if __name__ == "__main__":
    unittest.main()
