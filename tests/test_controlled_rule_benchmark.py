from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from benchmarks.controlled_rule_benchmark import (
    run_controlled_rule_benchmark,
    write_controlled_rule_report,
)
from capyreview.evaluation_harness import load_jsonl


ROOT = Path(__file__).resolve().parents[1]


class ControlledRuleBenchmarkTests(unittest.TestCase):
    def test_reproduces_the_historical_detection_metrics(self):
        cases = load_jsonl(str(ROOT / "evaluation_data" / "pr_diff_100.jsonl"))

        report = run_controlled_rule_benchmark(cases)

        self.assertEqual(100, report["dataset"]["cases"])
        self.assertEqual(40, report["dataset"]["risk_cases"])
        self.assertEqual(60, report["dataset"]["clean_cases"])
        self.assertEqual(
            {"tp": 25, "fp": 5, "fn": 15},
            {
                key: report["baseline"]["metrics"][key]
                for key in ("tp", "fp", "fn")
            },
        )
        self.assertEqual(
            {"tp": 33, "fp": 7, "fn": 7},
            {
                key: report["candidate"]["metrics"][key]
                for key in ("tp", "fp", "fn")
            },
        )
        self.assertEqual(0.7143, report["baseline"]["metrics"]["f1"])
        self.assertEqual(0.8250, report["candidate"]["metrics"]["f1"])
        self.assertEqual(
            0.8421, report["baseline"]["metrics"]["high_risk_recall"]
        )
        self.assertEqual(
            0.9474, report["candidate"]["metrics"]["high_risk_recall"]
        )
        self.assertEqual(0.9167, report["candidate"]["metrics"]["clean_accuracy"])

    def test_offline_rules_are_not_imported_by_the_production_package(self):
        for path in (ROOT / "capyreview").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("benchmarks.controlled_rule_benchmark", source, path.name)
            self.assertNotIn("LocalRuleReviewer", source, path.name)
            self.assertNotIn("ContextRuleReviewer", source, path.name)

    def test_writes_auditable_json_and_markdown_reports(self):
        cases = load_jsonl(str(ROOT / "evaluation_data" / "pr_diff_100.jsonl"))
        report = run_controlled_rule_benchmark(cases)

        with TemporaryDirectory() as directory:
            json_path, markdown_path = write_controlled_rule_report(directory, report)
            markdown = markdown_path.read_text(encoding="utf-8")

            self.assertTrue(json_path.is_file())
            self.assertTrue(markdown_path.is_file())
            self.assertIn("F1 | 71.4% | 82.5%", markdown)
            self.assertIn("High-risk recall | 84.2% | 94.7%", markdown)
            self.assertIn("not evidence of LLM or Multi-Agent improvement", markdown)

    def test_rejects_an_incomplete_or_rebalanced_corpus(self):
        cases = load_jsonl(str(ROOT / "evaluation_data" / "pr_diff_100.jsonl"))

        with self.assertRaisesRegex(ValueError, "100 cases"):
            run_controlled_rule_benchmark(cases[:-1])


if __name__ == "__main__":
    unittest.main()
