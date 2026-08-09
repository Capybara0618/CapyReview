import unittest

from capyreview.diff_parser import parse_unified_diff
from capyreview.reviewer import OpenAICompatibleReviewer


class CannedLLMReviewer(OpenAICompatibleReviewer):
    def __init__(self):
        super().__init__("https://example.invalid", "key", "model")

    def _request_json(self, _payload):
        return {"findings": [
            {
                "rule_id": "CWE-95", "severity": "critical",
                "title": "Dynamic execution", "explanation": "Input is executed.",
                "path": "app.py", "line": 2, "evidence": "eval(user_input)",
                "fix": "Use a constrained parser.", "test": "Test malicious input.",
                "confidence": 0.9,
            },
            {
                "rule_id": "CWE-95", "severity": "critical",
                "title": "Removed line", "explanation": "This is not added code.",
                "path": "app.py", "line": 0, "evidence": "eval(old_input)",
                "fix": "No action.", "test": "No test.", "confidence": 0.9,
            },
        ]}


class OpenAICompatibleReviewerTests(unittest.TestCase):
    def test_keeps_only_llm_findings_on_added_lines(self):
        diff = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
-eval(old_input)
+safe = parse(old_input)
+eval(user_input)
 safe = True
"""

        findings = CannedLLMReviewer().review(diff, parse_unified_diff(diff))

        self.assertEqual(1, len(findings))
        self.assertEqual("CWE-95", findings[0].rule_id)
        self.assertEqual(2, findings[0].line)


if __name__ == "__main__":
    unittest.main()
