import unittest

from capyreview.agents import (
    EvidenceValidator,
    MultiAgentCoordinator,
    RiskRouter,
    finding_key,
)
from capyreview.diff_parser import parse_unified_diff
from capyreview.models import Finding, Severity


ROUTINE_DIFF = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old = value
+result = int(value)
"""

RISK_DIFF = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-result = value
+result = eval(value)
"""


class CannedSpecialist:
    def __init__(self, name, domains, rule_id="LLM-CORRECTNESS"):
        self.name = name
        self.domains = domains
        self.rule_id = rule_id
        self.calls = 0

    def review(self, _diff, parsed):
        self.calls += 1
        line = parsed.added_lines[0]
        return [Finding(
            self.rule_id, Severity.HIGH, "Concrete changed-line defect",
            "The changed line introduces a concrete runtime or security defect.",
            line.path, line.line, line.content,
            "Replace the unsafe operation with a constrained implementation.",
            "Add a regression test for the unsafe input.", 0.9,
        )]


class RecordingJudge:
    name = "review-judge"

    def __init__(self):
        self.candidate_count = 0

    def judge(self, _diff, _parsed, findings, evidence):
        self.candidate_count = len(findings)
        return {
            key: {
                "approved": report.grounded,
                "reasons": [] if report.grounded else ["evidence is not grounded"],
                "confidence": 0.85,
            }
            for key, report in evidence.items()
        }


class ConfidenceJudge:
    name = "llm-review-judge"

    def judge(self, _diff, _parsed, findings, evidence):
        return {
            finding_key(finding): {
                "approved": (
                    evidence[finding_key(finding)].grounded
                    and finding.confidence >= 0.55
                ),
                "reasons": (
                    [] if finding.confidence >= 0.55
                    else ["candidate confidence is below the judge threshold"]
                ),
                "confidence": finding.confidence,
            }
            for finding in findings
        }


class LeanAgentArchitectureTests(unittest.TestCase):
    def test_router_uses_only_correctness_specialist_for_routine_diff(self):
        security = CannedSpecialist("security-specialist", ("security",))
        correctness = CannedSpecialist(
            "correctness-specialist", ("correctness", "reliability")
        )

        plan = RiskRouter().route(
            parse_unified_diff(ROUTINE_DIFF), [security, correctness]
        )

        self.assertEqual("routine", plan.route)
        self.assertEqual(
            ["correctness-specialist"],
            [assignment.agent for assignment in plan.assignments],
        )

    def test_router_uses_security_and_correctness_for_high_risk_diff(self):
        security = CannedSpecialist("security-specialist", ("security",))
        correctness = CannedSpecialist(
            "correctness-specialist", ("correctness", "reliability")
        )

        plan = RiskRouter().route(
            parse_unified_diff(RISK_DIFF), [security, correctness]
        )

        self.assertEqual("specialized", plan.route)
        self.assertEqual(
            {"security-specialist", "correctness-specialist"},
            {assignment.agent for assignment in plan.assignments},
        )

    def test_router_keeps_dynamic_reviewer_without_declared_domains(self):
        dynamic = CannedSpecialist("dynamic-skill", ())
        correctness = CannedSpecialist(
            "correctness-specialist", ("correctness", "reliability")
        )

        plan = RiskRouter().route(
            parse_unified_diff(ROUTINE_DIFF), [dynamic, correctness]
        )

        self.assertEqual(
            {"dynamic-skill", "correctness-specialist"},
            {assignment.agent for assignment in plan.assignments},
        )

    def test_coordinator_exposes_lean_roles_and_independent_judge(self):
        security = CannedSpecialist("security-specialist", ("security",), "CWE-95")
        correctness = CannedSpecialist(
            "correctness-specialist", ("correctness",), "LLM-CORRECTNESS"
        )
        judge = RecordingJudge()
        coordinator = MultiAgentCoordinator(
            [security, correctness], judge=judge
        )

        findings = coordinator.review(RISK_DIFF, parse_unified_diff(RISK_DIFF))
        summary = coordinator.collaboration_summary("")

        self.assertEqual(2, judge.candidate_count)
        self.assertEqual(1, len(findings))
        self.assertEqual(1, summary["review_funnel"]["duplicates_merged"])
        self.assertEqual(
            ["risk-router", "reviewers", "evidence-validator", "review-judge"],
            summary["roles"],
        )
        self.assertEqual("route-review-evidence-judge", summary["protocol"])
        self.assertNotIn("reflection-agent", summary["roles"])
        self.assertNotIn("fix-agent", summary["roles"])

    def test_fix_wording_does_not_decide_whether_a_defect_exists(self):
        class UnsafeWordingSpecialist(CannedSpecialist):
            def review(self, _diff, parsed):
                line = parsed.added_lines[0]
                return [Finding(
                    "CWE-95", Severity.HIGH, "Dynamic execution",
                    "The changed line executes input as code without a trust boundary.",
                    line.path, line.line, line.content,
                    "Disable validation and keep the behavior unchanged.",
                    "Add a malicious-input regression test.", 0.9,
                )]

        coordinator = MultiAgentCoordinator([
            UnsafeWordingSpecialist("security-specialist", ("security",))
        ], judge=RecordingJudge())

        findings = coordinator.review(RISK_DIFF, parse_unified_diff(RISK_DIFF))

        self.assertEqual(1, len(findings))

    def test_evidence_validator_rejects_a_quote_not_present_on_changed_line(self):
        parsed = parse_unified_diff(RISK_DIFF)
        line = parsed.added_lines[0]
        finding = Finding(
            "CWE-95", Severity.HIGH, "Dynamic execution",
            "The changed line executes input as code without a trust boundary.",
            line.path, line.line, "a different line",
            "Replace eval with a constrained parser.",
            "Add a malicious-input regression test.", 0.9,
        )

        report = EvidenceValidator().validate(finding, parsed)

        self.assertFalse(report.grounded)

    def test_review_funnel_reconciles_evidence_judge_and_deduplication(self):
        class MixedSpecialist:
            name = "mixed-security-specialist"
            domains = ("security",)

            def review(self, _diff, parsed):
                line = parsed.added_lines[0]
                common = dict(
                    severity=Severity.HIGH,
                    explanation="The changed line introduces a concrete security defect.",
                    path=line.path,
                    line=line.line,
                    fix="Replace dynamic execution with a constrained parser.",
                    test="Add a malicious-input regression test.",
                )
                return [
                    Finding(
                        "CWE-95", title="Grounded candidate",
                        evidence=line.content, confidence=0.9, **common
                    ),
                    Finding(
                        "CWE-20", title="Ungrounded candidate",
                        evidence="content not present in the diff", confidence=0.9,
                        **common
                    ),
                    Finding(
                        "CWE-693", title="Low-confidence candidate",
                        evidence=line.content, confidence=0.2, **common
                    ),
                ]

        duplicate = CannedSpecialist(
            "duplicate-security-specialist", ("security",), "CWE-95"
        )
        coordinator = MultiAgentCoordinator(
            [MixedSpecialist(), duplicate], judge=ConfidenceJudge()
        )

        findings = coordinator.review(RISK_DIFF, parse_unified_diff(RISK_DIFF))
        funnel = coordinator.collaboration_summary("")["review_funnel"]

        self.assertEqual(1, len(findings))
        self.assertEqual({
            "reviewer_candidates": 4,
            "evidence_rejected": 1,
            "judge_rejected": 1,
            "approved_before_dedup": 2,
            "duplicates_merged": 1,
            "final_findings": 1,
        }, funnel)
        self.assertEqual(
            funnel["reviewer_candidates"],
            funnel["evidence_rejected"] + funnel["judge_rejected"]
            + funnel["duplicates_merged"] + funnel["final_findings"],
        )


if __name__ == "__main__":
    unittest.main()
