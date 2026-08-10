import unittest

from capyreview.agents import MultiAgentCoordinator
from capyreview.diff_parser import parse_unified_diff
from capyreview.models import Finding, Severity
from capyreview.reviewer import OpenAICompatibleReviewer
from tests.fakes import InMemoryTaskStore


DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n-old\n+eval(data)\n+print(data)\n"


class ApprovingJudge:
    name = "llm-review-judge"

    def judge(self, _diff, _parsed, _findings, evidence):
        return {
            key: {
                "approved": report.grounded,
                "reasons": [] if report.grounded else ["ungrounded evidence"],
                "confidence": 0.9,
            }
            for key, report in evidence.items()
        }


class CannedLLMReviewer(OpenAICompatibleReviewer):
    def __init__(self, name, domains, raw_finding):
        super().__init__("https://example.invalid", "key", "model")
        self.name = name
        self.domains = domains
        self.raw_finding = raw_finding

    def _request_json(self, _payload):
        return {"findings": [self.raw_finding]}


class MultiAgentCollaborationTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryTaskStore()

    def test_security_and_reliability_are_independent_specialists(self):
        parsed = parse_unified_diff(DIFF)

        security_reviewer = CannedLLMReviewer(
            "llm-security-specialist", ("security",), {
                "rule_id": "SEC-EVAL", "severity": "critical",
                "title": "Dynamic execution", "explanation": "Input is executed.",
                "path": "app.py", "line": 1, "evidence": "eval(data)",
                "fix": "Use a parser.", "test": "Test malicious input.",
                "confidence": 0.9,
            },
        )
        reliability_reviewer = CannedLLMReviewer(
            "llm-correctness-specialist", ("correctness", "reliability"), {
                "rule_id": "REL-DEBUG-PRINT", "severity": "low",
                "title": "Debug output", "explanation": "Output leaks runtime data.",
                "path": "app.py", "line": 2, "evidence": "print(data)",
                "fix": "Use structured logging.", "test": "Assert output is absent.",
                "confidence": 0.8,
            },
        )

        security = security_reviewer.review(DIFF, parsed)
        reliability = reliability_reviewer.review(DIFF, parsed)

        self.assertEqual({"SEC-EVAL"}, {item.rule_id for item in security})
        self.assertEqual({"REL-DEBUG-PRINT"}, {item.rule_id for item in reliability})
        self.assertNotEqual(security_reviewer.name, reliability_reviewer.name)
        self.assertNotEqual(security_reviewer.domains, reliability_reviewer.domains)

    def test_evidence_validator_rejects_ungrounded_candidate(self):
        class UngroundedSpecialist:
            name = "ungrounded-specialist"
            domains = ("correctness",)

            def __init__(self):
                self.calls = 0

            def review(self, diff, parsed):
                return self.review_assignment(diff, parsed, {}, [], [])

            def review_assignment(self, _diff, parsed, _assignment, feedback, _inbox):
                self.calls += 1
                line = parsed.added_lines[0]
                return [Finding(
                    "LLM-CORRECTNESS", Severity.MEDIUM, "Unsafe dynamic execution",
                    "Untrusted input reaches a dynamic execution operation on the changed line.",
                    line.path, line.line, "evidence not present in the diff",
                    "Replace dynamic execution with an explicit parser and allow-listed dispatch.",
                    "Add a regression test proving untrusted expressions are treated as data.",
                    0.8,
                )]

        specialist = UngroundedSpecialist()
        self.store.create("task", "org/repo", 1, {})
        coordinator = MultiAgentCoordinator(
            [specialist], store=self.store, judge=ApprovingJudge()
        )

        findings = coordinator.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF)
        )
        task = self.store.get("task")
        kinds = {item["kind"] for item in task["collaboration"]}
        summary = coordinator.collaboration_summary("task")

        self.assertEqual([], findings)
        self.assertEqual(1, specialist.calls)
        self.assertTrue({
            "assignment", "reviewer_candidates", "evidence_validation",
            "judge_decision", "final_decision",
        }.issubset(kinds))
        self.assertEqual(0, summary["dialogue_rounds"])
        self.assertEqual(0, summary["approved_findings"])

    def test_failed_agent_is_retried_then_replanned_to_substitute(self):
        class BrokenSpecialist:
            name = "broken-security-agent"
            domains = ("reliability",)

            def review(self, _diff, _parsed):
                raise RuntimeError("provider unavailable")

        self.store.create("task", "org/repo", 1, {})
        backup = CannedLLMReviewer(
            "llm-correctness-specialist", ("reliability", "correctness"), {
                "rule_id": "REL-DEBUG-PRINT", "severity": "low",
                "title": "Debug output", "explanation": "Output leaks runtime data.",
                "path": "app.py", "line": 2, "evidence": "print(data)",
                "fix": "Use structured logging.", "test": "Assert output is absent.",
                "confidence": 0.8,
            },
        )
        coordinator = MultiAgentCoordinator(
            [BrokenSpecialist(), backup], store=self.store, agent_retries=1,
            judge=ApprovingJudge(),
        )

        findings = coordinator.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF)
        )
        summary = coordinator.collaboration_summary("task")
        kinds = [item["kind"] for item in self.store.get("task")["collaboration"]]

        self.assertEqual({"REL-DEBUG-PRINT"}, {item.rule_id for item in findings})
        self.assertEqual(1, summary["retries"])
        self.assertEqual(1, summary["handoffs"])
        self.assertIn("assignment_handoff", kinds)
        self.assertTrue(any(
            item["substituted_for"] == "broken-security-agent"
            for item in summary["agents"]
        ))

    def test_fix_wording_does_not_control_finding_validity(self):
        class UnsafeFixSpecialist:
            name = "unsafe-fix-specialist"
            domains = ("correctness",)

            def review(self, _diff, parsed):
                line = parsed.added_lines[0]
                return [Finding(
                    "LLM-UNSAFE-FIX", Severity.MEDIUM, "Dynamic execution risk",
                    "The changed line dynamically executes input without a trust boundary.",
                    line.path, line.line, line.content,
                    "Disable validation and keep the dynamic execution behavior unchanged.",
                    "Add a focused regression test for malicious expression input.", 0.9,
                )]

        coordinator = MultiAgentCoordinator(
            [UnsafeFixSpecialist()], judge=ApprovingJudge()
        )

        findings = coordinator.review(DIFF, parse_unified_diff(DIFF))

        self.assertEqual(1, len(findings))


if __name__ == "__main__":
    unittest.main()
