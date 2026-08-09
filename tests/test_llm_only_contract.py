import inspect
import unittest

import capyreview.agents as agents_module
import capyreview.reviewer as reviewer_module
import capyreview.skill_evolution as skill_evolution
from capyreview.agents import MultiAgentCoordinator
from capyreview.diff_parser import parse_unified_diff
from capyreview.reviewer import Reviewer


RISK_DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"


class RejectingJudge:
    name = "llm-review-judge"

    def judge(self, _diff, _parsed, findings, evidence):
        return {
            key: {
                "approved": False,
                "reasons": ["rejected by the configured judge"],
                "confidence": 0.0,
            }
            for key in evidence
        }


class BrokenLLMReviewer(Reviewer):
    name = "llm-security-specialist"
    domains = ("security",)

    def review(self, _diff, _parsed):
        raise RuntimeError("provider unavailable")


class LLMOnlyContractTests(unittest.TestCase):
    def test_local_reviewer_types_and_policy_judge_are_not_exported(self):
        for name in (
            "LocalRuleReviewer",
            "DomainRuleReviewer",
            "SecurityRuleReviewer",
            "ReliabilityRuleReviewer",
            "CompositeReviewer",
        ):
            self.assertFalse(hasattr(reviewer_module, name), name)
        self.assertFalse(hasattr(agents_module, "FilteredReviewer"))
        self.assertFalse(hasattr(agents_module, "PolicyJudge"))

    def test_coordinator_requires_an_explicit_judge_and_has_no_fallback_parameter(self):
        parameters = inspect.signature(MultiAgentCoordinator).parameters
        self.assertNotIn("fallback_agent", parameters)
        with self.assertRaisesRegex(ValueError, "judge"):
            MultiAgentCoordinator([BrokenLLMReviewer()])

    def test_failed_only_reviewer_is_not_replaced_by_a_local_implementation(self):
        coordinator = MultiAgentCoordinator(
            [BrokenLLMReviewer()], agent_retries=0, judge=RejectingJudge()
        )

        with self.assertRaisesRegex(RuntimeError, "all review assignments failed"):
            coordinator.review(RISK_DIFF, parse_unified_diff(RISK_DIFF))

    def test_review_policy_is_versioned_prompt_data_not_a_reviewer(self):
        self.assertTrue(hasattr(skill_evolution, "ReviewPolicy"))
        normalized = skill_evolution.validate_artifact({
            "name": "evolved-review",
            "description": "Confirmed project review guidance",
            "instructions": [{
                "rule_id": "SEC-DANGEROUS-CALL",
                "severity": "high",
                "domains": ["security"],
                "instruction": (
                    "Report dangerous dynamic calls only when the added line "
                    "shows a concrete untrusted-data path."
                ),
            }],
        }, "evolved-review")

        policy = skill_evolution.ReviewPolicy(normalized, version=3)

        self.assertEqual(2, normalized["schema_version"])
        self.assertNotIn("rules", normalized)
        self.assertEqual("evolved-review@3", policy.name)
        self.assertFalse(isinstance(policy, Reviewer))
        self.assertFalse(hasattr(policy, "review"))
        security_prompt = policy.compose_system_prompt(
            "Base security prompt.", ("security",)
        )
        correctness_prompt = policy.compose_system_prompt(
            "Base correctness prompt.", ("correctness",)
        )
        self.assertIn("SEC-DANGEROUS-CALL", security_prompt)
        self.assertIn("Base security prompt.", security_prompt)
        self.assertEqual("Base correctness prompt.", correctness_prompt)

    def test_review_policy_module_has_no_second_evolution_engine(self):
        artifact = skill_evolution.validate_artifact({
            "name": "evolved-review",
            "instructions": [{
                "rule_id": "SEC-DANGEROUS-CALL",
                "severity": "high",
                "domains": ["security"],
                "instruction": "Review confirmed dangerous-call regressions.",
            }],
        }, "evolved-review")
        policy = skill_evolution.ReviewPolicy(artifact, version=1)

        self.assertFalse(hasattr(skill_evolution, "SkillEvolutionEngine"))
        prompt = skill_evolution.compose_system_prompt(
            "Base security prompt.", [policy], ("security",)
        )
        self.assertIn("SEC-DANGEROUS-CALL", prompt)


if __name__ == "__main__":
    unittest.main()
