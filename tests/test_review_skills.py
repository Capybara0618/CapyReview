from pathlib import Path
import tempfile
import unittest

from capyreview.review_skills import ReviewSkillRegistry, ReviewSkillSelector


ROOT = Path(__file__).resolve().parents[1]


def write_skill(root: Path, name: str, description: str, domains: str, signals: str):
    skill = root / name
    (skill / "references").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        f"  capyreview-domains: {domains}\n"
        f"  capyreview-signals: {signals}\n"
        "---\n\n"
        f"# {name}\n\n"
        "Inspect the trust boundary and gather exact changed-line evidence.\n\n"
        "Read [patterns](references/patterns.md) only when detailed guidance is needed.\n",
        encoding="utf-8",
    )
    (skill / "references" / "patterns.md").write_text(
        "# Patterns\n\nReject fixed authentication bypasses.\n",
        encoding="utf-8",
    )


class ReviewSkillRegistryTests(unittest.TestCase):
    def test_discovery_loads_only_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(
                root, "review-auth-security",
                "Review authentication and HMAC changes. Use for signature code.",
                "security", "auth signature hmac",
            )

            discovered = ReviewSkillRegistry(root).discover()

            self.assertEqual(["review-auth-security"], [item.name for item in discovered])
            self.assertEqual(("security",), discovered[0].domains)
            self.assertEqual(("auth", "signature", "hmac"), discovered[0].signals)
            self.assertFalse(hasattr(discovered[0], "body"))

    def test_activation_and_reference_loading_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(
                root, "review-auth-security",
                "Review authentication and HMAC changes. Use for signature code.",
                "security", "auth signature hmac",
            )
            registry = ReviewSkillRegistry(root)

            activated = registry.activate("review-auth-security")

            self.assertIn("Inspect the trust boundary", activated.body)
            self.assertEqual(("references/patterns.md",), activated.references)
            self.assertNotIn("Reject fixed authentication bypasses", activated.body)
            reference = registry.read_reference(
                "review-auth-security", "references/patterns.md"
            )
            self.assertIn("Reject fixed authentication bypasses", reference)

    def test_rejects_directory_name_mismatch_and_unsafe_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(
                root, "review-auth-security",
                "Review authentication and HMAC changes. Use for signature code.",
                "security", "auth signature hmac",
            )
            path = root / "review-auth-security" / "SKILL.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "name: review-auth-security", "name: different-name"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "directory"):
                ReviewSkillRegistry(root).discover()

            write_skill(
                root, "review-auth-security",
                "Review authentication and HMAC changes. Use for signature code.",
                "security", "auth signature hmac",
            )
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "references/patterns.md", "../outside.md"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "reference"):
                ReviewSkillRegistry(root).activate("review-auth-security")

    def test_checked_in_skills_are_valid_formal_packages(self):
        registry = ReviewSkillRegistry(ROOT / "skills")

        discovered = registry.discover()

        self.assertEqual(
            {
                "review-async-reliability",
                "review-auth-security",
                "review-database-migration",
            },
            {item.name for item in discovered},
        )
        for item in discovered:
            self.assertTrue(registry.activate(item.name).body.strip())

    def test_activates_a_versioned_formal_package_from_persistent_content(self):
        package = {
            "name": "review-auth-security",
            "version": 3,
            "skill_md": """---
name: review-auth-security
description: Review authorization checks learned from confirmed failures.
metadata:
  capyreview-domains: security
  capyreview-signals: auth permission
---

# Authorization Review

Read [authorization cases](references/authorization.md) when permissions change.
""",
            "references": {
                "references/authorization.md": "Require deny-by-default checks."
            },
        }

        registry = ReviewSkillRegistry(ROOT / "skills", packages=[package])
        activated = registry.activate("review-auth-security")

        self.assertEqual(3, activated.version)
        self.assertEqual(
            "Require deny-by-default checks.",
            registry.read_reference(
                "review-auth-security", "references/authorization.md"
            ),
        )

    def test_rejects_persisted_instructions_without_real_skill_markdown(self):
        with self.assertRaisesRegex(ValueError, "skill_md"):
            ReviewSkillRegistry(ROOT / "skills", packages=[{
                "name": "review-auth-security", "version": 2,
                "instructions": ["check permissions"],
            }])


class ReviewSkillSelectorTests(unittest.TestCase):
    def test_selects_auth_skill_only_for_security_reviewer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(
                root, "review-auth-security",
                "Review authentication and HMAC changes. Use for signature code.",
                "security", "auth signature hmac webhook",
            )
            write_skill(
                root, "review-async-reliability",
                "Review asynchronous execution and retry changes.",
                "correctness reliability", "async retry lock queue",
            )
            metadata = ReviewSkillRegistry(root).discover()

            security = ReviewSkillSelector().select(
                metadata, ("security",), ("capyreview/github.py",),
                '+    if signature == "sha256=development-bypass":\n',
            )
            correctness = ReviewSkillSelector().select(
                metadata, ("correctness", "reliability"),
                ("capyreview/github.py",),
                '+    if signature == "sha256=development-bypass":\n',
            )

            self.assertEqual(["review-auth-security"], [item.name for item in security])
            self.assertEqual([], correctness)

    def test_does_not_activate_a_skill_for_unrelated_documentation(self):
        registry = ReviewSkillRegistry(ROOT / "skills")

        selected = ReviewSkillSelector().select(
            registry.discover(), ("correctness",), ("README.md",),
            "+Clarify the installation instructions.\n",
        )

        self.assertEqual([], selected)


if __name__ == "__main__":
    unittest.main()
