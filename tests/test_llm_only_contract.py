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

    def test_evolution_artifact_is_a_formal_skill_package_not_a_reviewer(self):
        package = skill_evolution.validate_skill_package({
            "name": "review-dangerous-calls",
            "skill_md": """---
name: review-dangerous-calls
description: Review confirmed dangerous-call regressions in changed code.
metadata:
  capyreview-domains: security
  capyreview-signals: eval exec shell
---

# Dangerous Calls

Require exact changed-line evidence and a concrete untrusted-data path.
""",
            "references": {},
        })

        self.assertIn("skill_md", package)
        self.assertNotIn("prompt", package)
        self.assertNotIn("instructions", package)
        self.assertFalse(isinstance(package, Reviewer))
        self.assertFalse(hasattr(skill_evolution, "ReviewPolicy"))

    def test_formal_skill_module_has_no_executable_reviewer_or_second_engine(self):
        self.assertFalse(hasattr(skill_evolution, "SkillEvolutionEngine"))
        self.assertFalse(hasattr(skill_evolution, "ReviewPolicy"))
        self.assertFalse(hasattr(skill_evolution, "compose_system_prompt"))


if __name__ == "__main__":
    unittest.main()
