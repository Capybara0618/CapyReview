import unittest
import json
from unittest.mock import patch

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

    def test_provider_usage_and_latency_are_available_once_per_llm_call(self):
        body = json.dumps({
            "choices": [{"message": {"content": '{"findings":[]}'}}],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
            },
        }).encode("utf-8")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return body

        reviewer = OpenAICompatibleReviewer(
            "https://example.invalid", "key", "model"
        )
        with patch("urllib.request.urlopen", return_value=Response()):
            reviewer._request_json({"model": "model", "messages": []})

        usage = reviewer.consume_usage()
        self.assertEqual(1, usage["llm_calls"])
        self.assertEqual(120, usage["prompt_tokens"])
        self.assertEqual(30, usage["completion_tokens"])
        self.assertEqual(150, usage["total_tokens"])
        self.assertGreaterEqual(usage["latency_ms"], 0)
        self.assertEqual({}, reviewer.consume_usage())

    def test_agent_output_is_bounded_for_structured_review(self):
        class CapturingReviewer(OpenAICompatibleReviewer):
            def __init__(self):
                super().__init__("https://example.invalid", "key", "model")
                self.payload = {}

            def _request_json(self, payload):
                self.payload = payload
                return {"action": "final", "findings": []}

        diff = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+safe = True\n"
        parsed = parse_unified_diff(diff)
        reviewer = CapturingReviewer()

        reviewer.agent_step({
            "parsed": parsed,
            "managed_context": "DIFF_CONTEXT:\n" + diff,
            "available_tools": [],
        })

        self.assertEqual(2048, reviewer.payload["max_tokens"])
        self.assertEqual({"type": "disabled"}, reviewer.payload["thinking"])


if __name__ == "__main__":
    unittest.main()
