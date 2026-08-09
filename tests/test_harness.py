import os
import tempfile
import unittest

from capyreview.harness import ReviewHarness
from capyreview.models import Finding, Severity
from capyreview.store import TaskStore


class FakeLlmReviewer:
    name = "fake-llm-reviewer"

    def review(self, _diff, parsed):
        line = parsed.added_lines[0]
        return [Finding(
            "SEC-EVAL", Severity.HIGH, "Dynamic execution",
            "The changed line executes data as code.", line.path, line.line,
            line.content, "Use a typed parser.", "Add an input regression test.",
        )]


class HarnessTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = TaskStore(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_successful_state_flow_is_persisted(self):
        task_id = "test-task"
        self.store.create(task_id, "demo/repo", 7, {"source": "test"})
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+eval(value)\n"
        report = ReviewHarness(self.store, FakeLlmReviewer()).run(
            task_id, "demo/repo", 7, diff
        )
        task = self.store.get(task_id)
        self.assertEqual("SUCCESS", task["state"])
        self.assertEqual(["PLANNING", "EXECUTING", "REVIEWING", "SUCCESS"], [x["state"] for x in task["trace"]])
        self.assertEqual("high", report.risk)

    def test_invalid_diff_is_recorded_as_failure(self):
        task_id = "bad-task"
        self.store.create(task_id, "demo/repo", None, {"source": "test"})
        with self.assertRaises(ValueError):
            ReviewHarness(self.store, FakeLlmReviewer()).run(
                task_id, "demo/repo", None, "not a diff"
            )
        self.assertEqual("FAILED", self.store.get(task_id)["state"])


if __name__ == "__main__":
    unittest.main()
