import json
import os
import tempfile
import unittest

from capyreview.agents import EvidenceReport, MultiAgentCoordinator, finding_key
from capyreview.diff_parser import parse_unified_diff
from capyreview.evaluation_harness import EndToEndEvaluationHarness
from capyreview.llm_evaluation import (
    build_evaluation_metadata,
    build_llm_evaluation_reviewer,
    run_evaluation_checkpointed,
    write_evaluation_report,
)
from capyreview.models import Finding, Severity
from capyreview.reviewer import OpenAICompatibleJudge, Reviewer


RISK_DIFF = """--- a/app.py
+++ b/app.py
@@ -1 +1,2 @@
 value = input()
+result = eval(value)
"""


def risk_case():
    return {
        "id": "risk-1",
        "repository": "demo/repo",
        "pull_request": 1,
        "split": "holdout",
        "source": {"kind": "synthetic-controlled"},
        "diff": RISK_DIFF,
        "expected_findings": [{
            "path": "app.py",
            "start_line": 2,
            "end_line": 2,
            "cwe": "CWE-95",
            "rule_id": "SEC-EVAL",
            "severity": "critical",
        }],
    }


class FixedReviewer(Reviewer):
    name = "fixed"

    def __init__(self, evidence):
        self.evidence = evidence

    def review(self, _diff, _parsed):
        return [Finding(
            "SEC-EVAL", Severity.CRITICAL, "Dynamic execution",
            "User-controlled data reaches eval.", "app.py", 2,
            self.evidence, "Replace eval.", "Add a malicious-input test.", 0.95,
        )]


class CaseAwareReviewer(Reviewer):
    name = "capyreview-test-double"

    def review(self, diff, parsed):
        if "eval(value)" not in diff:
            return []
        return FixedReviewer("result = eval(value)").review(diff, parsed)


class LlmEvaluationContractTests(unittest.TestCase):
    def test_judge_prompt_rejects_environment_only_and_secondary_claims(self):
        prompt = OpenAICompatibleJudge.SYSTEM_PROMPT.lower()
        self.assertIn("environment", prompt)
        self.assertIn("secondary", prompt)
        self.assertIn("one primary root cause", prompt)

    def test_independent_llm_judge_returns_semantic_decisions_by_candidate(self):
        finding = FixedReviewer("result = eval(value)").review(
            RISK_DIFF, parse_unified_diff(RISK_DIFF)
        )[0]
        candidate_id = finding_key(finding)

        class CannedJudge(OpenAICompatibleJudge):
            def _request_json(self, _payload):
                return {"decisions": [{
                    "candidate_id": candidate_id,
                    "approved": False,
                    "reason": "The diff does not establish attacker control.",
                    "confidence": 0.2,
                }]}

        judge = CannedJudge("https://example.invalid", "key", "model")
        decisions = judge.judge(
            RISK_DIFF, None, [finding], {
                candidate_id: EvidenceReport(
                    candidate_id, True, "changed-line check", "result = eval(value)"
                )
            },
        )
        self.assertFalse(decisions[candidate_id]["approved"])
        self.assertEqual(0.2, decisions[candidate_id]["confidence"])

    def test_evidence_validity_is_scored_from_added_line_content(self):
        valid = EndToEndEvaluationHarness().run(
            FixedReviewer("result = eval(value)"), [risk_case()]
        )
        invalid = EndToEndEvaluationHarness().run(
            FixedReviewer("eval is dangerous"), [risk_case()]
        )
        self.assertEqual(1.0, valid["metrics"]["evidence_valid_rate"])
        self.assertEqual(0.0, invalid["metrics"]["evidence_valid_rate"])

    def test_evaluation_builds_only_the_production_multi_agent_chain(self):
        config = {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "api_key": "secret-value",
            "model": "test-model",
            "headers": {},
        }
        reviewer = build_llm_evaluation_reviewer(config, timeout=12)

        self.assertIsInstance(reviewer, MultiAgentCoordinator)
        self.assertEqual(
            ["llm-security-specialist", "llm-correctness-specialist"],
            [item.name for item in reviewer.agents],
        )
        self.assertTrue(all(item.api_key == "secret-value" for item in reviewer.agents))
        self.assertIsInstance(reviewer.judge, OpenAICompatibleJudge)
        self.assertFalse(hasattr(reviewer, "fallback_agent"))

    def test_checkpointed_run_and_single_system_report_are_secret_free(self):
        clean = dict(risk_case())
        clean.update({
            "id": "clean-1",
            "pull_request": 2,
            "diff": """--- a/app.py
+++ b/app.py
@@ -1 +1,2 @@
 value = input()
+result = int(value)
""",
            "expected_findings": [],
        })
        cases = [risk_case(), clean]
        config = {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "api_key": "do-not-persist-this-secret",
            "model": "test-model",
            "headers": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            progress = os.path.join(directory, "case-results.jsonl")
            result = run_evaluation_checkpointed(
                CaseAwareReviewer(), cases, progress, resume=False
            )
            metadata = build_evaluation_metadata(config, cases, "source-hash")
            paths = write_evaluation_report(directory, metadata, result)

            self.assertEqual(2, result["metrics"]["cases"])
            self.assertEqual(1.0, result["metrics"]["f1"])
            with open(paths["json"], encoding="utf-8") as handle:
                report_text = handle.read()
            self.assertNotIn("do-not-persist-this-secret", report_text)
            report = json.loads(report_text)
            self.assertEqual("test-model", report["evaluation"]["model"])
            self.assertNotIn("baseline", report)
            self.assertNotIn("candidate", report)

            resumed = run_evaluation_checkpointed(
                CaseAwareReviewer(), cases, progress, resume=True
            )
            self.assertEqual(result["metrics"], resumed["metrics"])


if __name__ == "__main__":
    unittest.main()
