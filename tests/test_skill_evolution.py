import unittest

from capyreview.reviewer import Reviewer
from capyreview.skill_evolution import (
    ReviewPolicy,
    compose_system_prompt,
    validate_artifact,
)


def security_artifact():
    return {
        "name": "evolved-security-review",
        "description": "Confirmed security review guidance",
        "instructions": [{
            "rule_id": "SEC-DANGEROUS-CALL",
            "severity": "high",
            "domains": ["security"],
            "instruction": (
                "Report dangerous calls only when exact added-line evidence shows "
                "attacker-controlled data reaching the call."
            ),
        }],
    }


class ReviewPolicyTests(unittest.TestCase):
    def test_policy_is_versioned_prompt_data_not_executable_review_logic(self):
        normalized = validate_artifact(
            security_artifact(), "evolved-security-review"
        )
        policy = ReviewPolicy(normalized, 2)

        self.assertEqual(2, normalized["schema_version"])
        self.assertEqual([], normalized["permissions"])
        self.assertEqual("evolved-security-review@2", policy.name)
        self.assertFalse(isinstance(policy, Reviewer))
        self.assertFalse(hasattr(policy, "review"))
        self.assertEqual(64, len(policy.artifact_sha256))

    def test_policy_instructions_are_applied_only_to_matching_reviewer_domains(self):
        policy = ReviewPolicy(security_artifact(), 3)

        security_prompt = policy.compose_system_prompt(
            "Base security prompt.", ("security",)
        )
        correctness_prompt = policy.compose_system_prompt(
            "Base correctness prompt.", ("correctness", "reliability")
        )

        self.assertIn("evolved-security-review@3", security_prompt)
        self.assertIn("SEC-DANGEROUS-CALL", security_prompt)
        self.assertEqual("Base correctness prompt.", correctness_prompt)

    def test_multiple_policies_compose_in_version_order_supplied_by_the_caller(self):
        security = ReviewPolicy(security_artifact(), 1)
        reliability = ReviewPolicy({
            "name": "evolved-reliability-review",
            "instructions": [{
                "rule_id": "REL-RESOURCE-LEAK",
                "severity": "medium",
                "domains": ["reliability", "correctness"],
                "instruction": "Check added resource lifecycles for concrete leaks.",
            }],
        }, 4)

        prompt = compose_system_prompt(
            "Base prompt.", [security, reliability],
            ("security", "correctness"),
        )

        self.assertLess(
            prompt.index("evolved-security-review@1"),
            prompt.index("evolved-reliability-review@4"),
        )
        self.assertIn("SEC-DANGEROUS-CALL", prompt)
        self.assertIn("REL-RESOURCE-LEAK", prompt)

    def test_legacy_match_rules_and_invalid_policy_domains_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "deterministic rule artifacts"):
            validate_artifact({
                "name": "evolved-review",
                "rules": [{"rule_id": "SEC-OLD", "match": "eval("}],
            })
        with self.assertRaisesRegex(ValueError, "invalid review policy domain"):
            validate_artifact({
                "name": "evolved-review",
                "instructions": [{
                    "rule_id": "SEC-INVALID-DOMAIN",
                    "severity": "high",
                    "domains": ["local-rules"],
                    "instruction": "Invalid domain instruction.",
                }],
            })


if __name__ == "__main__":
    unittest.main()
