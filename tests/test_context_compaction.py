import unittest

from capyreview.agents import MultiAgentCoordinator, finding_key
from capyreview.context_manager import ContextManager
from capyreview.diff_parser import parse_unified_diff
from capyreview.models import Finding, Severity
from tests.fakes import InMemoryTaskStore


class ContextCompactionTests(unittest.TestCase):
    def test_full_diff_is_unchanged_when_it_fits_the_budget(self):
        diff = (
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,3 +1,3 @@ def run():\n"
            " unchanged_before\n"
            "-old_value = 1\n"
            "+new_value = 2\n"
            " unchanged_after\n"
        )

        bundles = ContextManager(
            max_tokens=512, reserved_tokens=0
        ).build_batches(diff)

        self.assertEqual(1, len(bundles))
        self.assertFalse(bundles[0].compressed)
        self.assertEqual("full-diff", bundles[0].strategy)
        self.assertEqual(diff, bundles[0].text)

    def test_overflow_uses_compact_change_view_without_losing_changed_lines(self):
        context = "".join(
            " unchanged_%03d = '%s'\n" % (index, "x" * 20)
            for index in range(90)
        )
        diff = (
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,92 +1,92 @@ def run():\n"
            + context[: len(context) // 2]
            + "-old_value = dangerous(source)\n"
            + "+new_value = safe(source)\n"
            + context[len(context) // 2 :]
        )

        bundles = ContextManager(
            max_tokens=512, reserved_tokens=0
        ).build_batches(diff)

        self.assertEqual(1, len(bundles))
        bundle = bundles[0]
        self.assertTrue(bundle.compressed)
        self.assertEqual("compact-change-view", bundle.strategy)
        self.assertIn("--- a/app.py", bundle.text)
        self.assertIn("+++ b/app.py", bundle.text)
        self.assertIn("@@ -1,92 +1,92 @@ def run():", bundle.text)
        self.assertIn("-old_value = dangerous(source)", bundle.text)
        self.assertIn("+new_value = safe(source)", bundle.text)
        self.assertNotIn("unchanged_000", bundle.text)
        self.assertIn("unchanged lines omitted", bundle.text)
        self.assertLess(bundle.final_tokens, bundle.original_tokens)

    def test_complete_material_is_measured_before_compression_is_activated(self):
        context = "".join(
            " unchanged_%03d = '%s'\n" % (index, "x" * 12)
            for index in range(45)
        )
        diff = (
            "--- a/app.py\n+++ b/app.py\n@@ -1,47 +1,47 @@\n"
            + context
            + "-old_value = source\n+new_value = source\n"
        )
        manager = ContextManager(max_tokens=512, reserved_tokens=0)
        base_fixed = 80

        without_memory = manager.build_batches(
            diff, fixed_tokens=base_fixed
        )
        memory_tokens = manager.runtime_tokens(memories=[{
            "scope": "semantic", "kind": "review_feedback",
            "content": "m" * 500,
        }])
        with_memory = manager.build_batches(
            diff, fixed_tokens=base_fixed + memory_tokens
        )

        self.assertFalse(without_memory[0].compressed)
        self.assertTrue(with_memory[0].compressed)
        self.assertEqual("compact-change-view", with_memory[0].strategy)

    def test_compact_view_keeps_new_and_old_line_position_after_omission(self):
        context = "".join(" unchanged_%02d\n" % index for index in range(80))
        diff = (
            "--- a/app.py\n+++ b/app.py\n"
            "@@ -10,82 +20,82 @@ def run():\n"
            " first_context\n-old_value\n+new_value\n" + context
        )

        bundle = ContextManager(
            max_tokens=512, reserved_tokens=0
        ).build_batches(diff, fixed_tokens=250)[0]

        self.assertIn(
            "[1 unchanged lines omitted; next old=11, new=21]",
            bundle.text,
        )
        self.assertIn("-old_value", bundle.text)
        self.assertIn("+new_value", bundle.text)

    def test_compact_diff_that_still_overflows_is_batched_without_losing_changes(self):
        hunks = []
        expected = []
        for group in range(8):
            changed = "+value_%02d = '%s'\n" % (group, str(group) * 180)
            expected.append(changed.strip())
            hunks.append(
                "@@ -{0} +{0} @@\n{1}".format(group * 10 + 1, changed)
            )
        diff = "--- a/app.py\n+++ b/app.py\n" + "".join(hunks)
        manager = ContextManager(max_tokens=512, reserved_tokens=0)

        bundles = manager.build_batches(diff, fixed_tokens=180)

        self.assertGreater(len(bundles), 1)
        self.assertTrue(all(not bundle.compressed for bundle in bundles))
        self.assertTrue(all(bundle.strategy == "hunk-batch" for bundle in bundles))
        self.assertEqual(list(range(1, len(bundles) + 1)), [
            bundle.batch_index for bundle in bundles
        ])
        self.assertTrue(all(bundle.batch_count == len(bundles) for bundle in bundles))
        self.assertTrue(all(
            bundle.final_tokens + 180 <= manager.max_tokens
            for bundle in bundles
        ))
        rendered = "\n".join(bundle.text for bundle in bundles)
        for line in expected:
            self.assertEqual(1, rendered.count(line))

    def test_coordinator_reviews_every_compact_batch_and_merges_findings(self):
        hunks = []
        for group in range(16):
            hunks.append(
                "@@ -{0} +{0} @@\n+risk_{1} = '{2}'\n".format(
                    group * 10 + 1, group, str(group) * 160
                )
            )
        diff = "--- a/app.py\n+++ b/app.py\n" + "".join(hunks)
        parsed = parse_unified_diff(diff)

        class BatchReviewer:
            name = "correctness-reviewer"
            domains = ("correctness",)

            def __init__(self):
                self.contexts = []

            def agent_step(self, state):
                context = state["managed_context"]
                self.contexts.append(context)
                findings = []
                for line in state["parsed"].added_lines:
                    if line.content in context:
                        findings.append(Finding(
                            "BATCH-%d" % line.line, Severity.HIGH,
                            "Batch-visible change", "Review this changed line.",
                            line.path, line.line, line.content,
                            "Apply the repository invariant.",
                            "Add a regression test.", 0.9,
                        ))
                return {"action": "final", "findings": findings}

        class ApprovingJudge:
            name = "judge"

            def judge(self, _diff, _parsed, findings, _evidence):
                return {
                    finding_key(item): {
                        "approved": True, "reasons": [], "confidence": 0.9,
                    }
                    for item in findings
                }

        reviewer = BatchReviewer()
        coordinator = MultiAgentCoordinator(
            [reviewer], judge=ApprovingJudge(),
            context_manager=ContextManager(max_tokens=512, reserved_tokens=0),
            agent_loop_max_steps=2,
        )

        findings = coordinator.review(diff, parsed)

        self.assertGreater(len(reviewer.contexts), 1)
        self.assertEqual(16, len(findings))
        self.assertEqual(
            {line.content for line in parsed.added_lines},
            {finding.evidence for finding in findings},
        )

    def test_retry_restores_completed_batches_without_repeating_their_llm_call(self):
        hunks = []
        for group in range(16):
            hunks.append(
                "@@ -{0} +{0} @@\n+risk_{1} = '{2}'\n".format(
                    group * 10 + 1, group, str(group) * 160
                )
            )
        diff = "--- a/app.py\n+++ b/app.py\n" + "".join(hunks)
        parsed = parse_unified_diff(diff)
        store = InMemoryTaskStore()
        store.create("batch-retry", "org/repo", 1, {})

        class FlakyBatchReviewer:
            name = "correctness-reviewer"
            domains = ("correctness",)

            def __init__(self):
                self.contexts = []
                self.batch_calls = []
                self.failed = False

            def agent_step(self, state):
                context = state["managed_context"]
                self.contexts.append(context)
                self.batch_calls.append(state["batch_index"])
                if state["batch_index"] == 2 and not self.failed:
                    self.failed = True
                    raise RuntimeError("injected later-batch failure")
                findings = []
                for line in state["parsed"].added_lines:
                    if line.content in context:
                        findings.append(Finding(
                            "BATCH-%d" % line.line, Severity.HIGH,
                            "Batch-visible change", "Review this changed line.",
                            line.path, line.line, line.content,
                            "Apply the repository invariant.",
                            "Add a regression test.", 0.9,
                        ))
                return {"action": "final", "findings": findings}

        class ApprovingJudge:
            name = "judge"

            def judge(self, _diff, _parsed, findings, _evidence):
                return {
                    finding_key(item): {
                        "approved": True, "reasons": [], "confidence": 0.9,
                    }
                    for item in findings
                }

        reviewer = FlakyBatchReviewer()
        coordinator = MultiAgentCoordinator(
            [reviewer], judge=ApprovingJudge(), store=store,
            agent_retries=1,
            context_manager=ContextManager(max_tokens=512, reserved_tokens=0),
            agent_loop_max_steps=2,
        )

        findings = coordinator.review_with_context(
            "batch-retry", diff, parsed, repository="org/repo"
        )

        self.assertEqual(16, len(findings))
        self.assertEqual(1, reviewer.batch_calls.count(1))


if __name__ == "__main__":
    unittest.main()
