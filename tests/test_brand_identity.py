from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".json", ".jsonl", ".yml", ".yaml", ".txt", ".toml", ".example"}


class CapyReviewIdentityTests(unittest.TestCase):
    def test_legacy_product_name_is_absent_from_source_tree(self) -> None:
        legacy_name = "".join(("evo", "agent"))
        offenders: list[str] = []

        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if any(part in {"tmp", "output", "__pycache__", ".git"} for part in relative.parts):
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"Dockerfile"}:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if legacy_name in content.lower():
                offenders.append(str(relative))

        self.assertEqual([], offenders, f"legacy product name remains in: {offenders}")

    def test_python_package_and_environment_prefix_use_capyreview(self) -> None:
        legacy_package = ROOT / "".join(("evo", "agent"))
        self.assertFalse(legacy_package.exists())
        self.assertTrue((ROOT / "capyreview" / "__main__.py").is_file())

        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("CAPYREVIEW_HOST=", env_example)


if __name__ == "__main__":
    unittest.main()
