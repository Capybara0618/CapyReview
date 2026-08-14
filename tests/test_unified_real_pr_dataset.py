import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from capyreview.real_pr_quality import load_quality_dataset


class UnifiedRealPRDatasetContractTests(unittest.TestCase):
    def test_checked_in_manifest_has_official_fifty_case_contract(self):
        manifest_path = Path("evaluation_data/real_pr_evaluation/manifest.json")
        if not manifest_path.exists():
            self.fail("unified 50-PR manifest has not been prepared")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        cases = manifest["cases"]

        self.assertEqual(2, manifest["schema_version"])
        self.assertEqual(50, len(cases))
        self.assertEqual(5, len({case["source_repository"] for case in cases}))
        self.assertEqual(10, sum(case["split"] == "development" for case in cases))
        self.assertEqual(40, sum(case["split"] == "test" for case in cases))
        self.assertEqual(173, sum(len(case["golden_comments"]) for case in cases))

        loaded, source = load_quality_dataset(str(manifest_path))
        self.assertEqual(50, len(loaded))
        self.assertEqual(139, sum(len(case["golden_comments"]) for case in loaded))
        self.assertEqual(3, sum(case["negative_control"] for case in loaded))
        self.assertEqual(
            "fbc5425c5eec52932aa1303708873d341968fa1c",
            source["commit"],
        )


if __name__ == "__main__":
    unittest.main()
