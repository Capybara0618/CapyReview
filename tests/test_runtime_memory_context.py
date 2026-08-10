import unittest

from capyreview.agents import MultiAgentCoordinator
from capyreview.context_manager import ContextManager
from capyreview.diff_parser import parse_unified_diff
from capyreview.memory import MemoryManager
from capyreview.models import Finding, Severity
from capyreview.reviewer import OpenAICompatibleReviewer
from capyreview.runtime import AgentLoop, AgentRuntime, AgentTool, RuntimeNode, ToolRegistry
from capyreview.store import utc_now
from tests.fakes import InMemoryTaskStore


class ApprovingJudge:
    name = "llm-review-judge"

    def judge(self, _diff, _parsed, _findings, evidence):
        return {
            key: {
                "approved": report.grounded,
                "reasons": [] if report.grounded else ["ungrounded evidence"],
                "confidence": 0.9,
            }
            for key, report in evidence.items()
        }


class RuntimeMemoryContextTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryTaskStore()

    def test_runtime_restores_completed_node_checkpoints(self):
        self.store.create("runtime-task", "org/repo", 1, {})
        runtime = AgentRuntime(max_steps=4, timeout_seconds=5)
        calls = []
        nodes = [
            RuntimeNode("plan", lambda _state: calls.append("plan") or {"value": 2}),
            RuntimeNode(
                "execute",
                lambda state: calls.append("execute") or {"result": state["value"] * 3},
            ),
        ]

        first = runtime.execute({}, nodes, "runtime-task", self.store)
        second = runtime.execute({}, nodes, "runtime-task", self.store)

        self.assertEqual(6, first["result"])
        self.assertEqual(6, second["result"])
        self.assertEqual(["plan", "execute"], calls)

    def test_agent_loop_executes_tool_then_returns_final_output(self):
        def stepper(state):
            if not state.get("observations"):
                return {"action": "tool", "tool": "lookup", "arguments": {"key": "x"}}
            return {"action": "final", "output": state["observations"][0]["result"]}

        result = AgentLoop(max_steps=3, timeout_seconds=5).run(
            stepper, {"lookup": lambda key: "value:%s" % key}, {}
        )

        self.assertEqual("value:x", result.output)
        self.assertEqual(2, result.steps)
        self.assertEqual("lookup", result.observations[0]["tool"])

    def test_tool_registry_validates_arguments_before_invocation(self):
        calls = []
        registry = ToolRegistry([AgentTool(
            "lookup", "Lookup one key.",
            {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"], "additionalProperties": False,
            },
            lambda key: calls.append(key) or "value:%s" % key,
        )])

        def stepper(state):
            if not state.get("observations"):
                return {
                    "action": "tool", "tool": "lookup",
                    "arguments": {"key": 7, "unexpected": True},
                }
            return {"action": "final", "output": state["observations"][0]}

        result = AgentLoop(max_steps=2, timeout_seconds=5).run(stepper, registry, {})

        self.assertFalse(result.output["ok"])
        self.assertIn("unknown tool arguments", result.output["error"])
        self.assertEqual([], calls)
        self.assertEqual("value:x", registry.invoke("lookup", {"key": "x"}))
        self.assertEqual(["x"], calls)

    def test_context_manager_compresses_large_diff_and_keeps_risk_evidence(self):
        added = ["+value_%03d = %d\n" % (index, index) for index in range(300)]
        added[250] = "+result = eval(user_input)\n"
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1,300 @@\n" + "".join(added)

        bundle = ContextManager(max_tokens=512, reserved_tokens=64).build(
            diff, {"risk_domains": ["security"], "objective": "find injection"}
        )

        self.assertTrue(bundle.compressed)
        self.assertLess(bundle.final_tokens, bundle.original_tokens)
        self.assertIn("eval(user_input)", bundle.text)
        self.assertEqual("risk-ranked-hunk-compression", bundle.strategy)

    def test_context_window_bounds_feedback_memory_and_observations(self):
        manager = ContextManager(max_tokens=512, reserved_tokens=128)
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        bundle = manager.build(diff, {"objective": "review"})
        feedback = ["feedback-%02d %s" % (i, "x" * 200) for i in range(12)]
        memories = [{
            "scope": "semantic", "kind": "review_feedback",
            "content": "memory-%02d %s" % (i, "y" * 200),
        } for i in range(12)]
        observations = [{
            "step": i, "tool": "search_diff", "ok": True,
            "result": "observation-%02d %s" % (i, "z" * 200),
        } for i in range(12)]

        managed = manager.compose(
            bundle, {"agent": "security", "objective": "review risky additions"},
            feedback=feedback, memories=memories, observations=observations,
            tools=[{"name": "search_diff", "description": "Search diff", "parameters": {}}],
        )

        self.assertLessEqual(managed.estimated_tokens, 512)
        self.assertTrue(managed.compressed)
        self.assertGreater(managed.dropped_observations, 0)
        self.assertGreater(managed.dropped_feedback + managed.dropped_memories, 0)
        self.assertIn("DIFF_CONTEXT", managed.text)

    def test_judge_receives_bounded_diff_context_with_candidate_evidence(self):
        added = ["+value_%04d = %d\n" % (index, index) for index in range(900)]
        added[820] = "+result = eval(user_input)\n"
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1,900 @@\n" + "".join(added)
        parsed = parse_unified_diff(diff)

        class SecuritySpecialist:
            name = "security-specialist"
            domains = ("security",)

            def review(self, _diff, parsed_diff):
                line = next(
                    item for item in parsed_diff.added_lines
                    if "eval(user_input)" in item.content
                )
                return [Finding(
                    "CWE-95", Severity.CRITICAL, "Dynamic execution",
                    "The changed line executes untrusted input as code.",
                    line.path, line.line, line.content,
                    "Replace eval with an allow-listed parser.",
                    "Add an untrusted-input regression test.", 0.95,
                )]

        class CapturingJudge(ApprovingJudge):
            def __init__(self):
                self.diff = ""

            def judge(self, bounded_diff, parsed_diff, findings, evidence):
                self.diff = bounded_diff
                return super().judge(bounded_diff, parsed_diff, findings, evidence)

        judge = CapturingJudge()
        coordinator = MultiAgentCoordinator(
            [SecuritySpecialist()], judge=judge,
            context_manager=ContextManager(max_tokens=512, reserved_tokens=64),
        )

        findings = coordinator.review(diff, parsed)
        summary = coordinator.collaboration_summary("")

        self.assertEqual(1, len(findings))
        self.assertLess(len(judge.diff), len(diff))
        self.assertIn("eval(user_input)", judge.diff)
        self.assertTrue(summary["judge_context"]["compressed"])
        self.assertGreaterEqual(summary["context_compressions"], 1)

    def test_memory_recall_is_repository_scoped(self):
        memory = MemoryManager(self.store, recall_limit=5)
        memory.remember(
            "org/repo", "semantic", "review_feedback",
            "SEC-EVAL was a confirmed missed issue in authentication code",
            importance=0.9,
        )
        memory.remember(
            "org/other", "semantic", "review_feedback",
            "REL-DEBUG-PRINT was accepted", importance=0.9,
        )

        recalled = memory.recall("org/repo", "authentication SEC-EVAL security")

        self.assertEqual(1, len(recalled))
        self.assertIn("SEC-EVAL", recalled[0]["content"])
        self.assertEqual([], memory.recall("org/other", "SEC-EVAL"))

    def test_coordinator_uses_agent_loop_context_and_memory(self):
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        parsed = parse_unified_diff(diff)
        self.store.create("loop-task", "org/repo", 7, {})
        memory = MemoryManager(self.store)
        memory.remember(
            "org/repo", "semantic", "review_feedback",
            "Dynamic execution in app.py requires exact changed-line evidence",
            importance=0.9,
        )

        class LoopSpecialist:
            name = "loop-specialist"
            domains = ("security",)

            def review(self, _diff, _parsed):
                return []

            def agent_step(self, state):
                if not state.get("observations"):
                    return {
                        "action": "tool", "tool": "changed_line",
                        "arguments": {"path": "app.py", "line": 1},
                    }
                line = state["parsed"].added_lines[0]
                return {"action": "final", "findings": [Finding(
                    "SEC-EVAL", Severity.CRITICAL, "Dynamic execution",
                    "The changed line executes data as code without a trust boundary.",
                    line.path, line.line, line.content,
                    "Replace eval with an explicit parser and allow-listed dispatch.",
                    "Add a regression test proving input is handled as data.", 0.9,
                )]}

        coordinator = MultiAgentCoordinator(
            [LoopSpecialist()], store=self.store, memory_manager=memory,
            context_manager=ContextManager(max_tokens=1024, reserved_tokens=128),
            judge=ApprovingJudge(),
        )
        findings = coordinator.review_with_context(
            "loop-task", diff, parsed, repository="org/repo"
        )
        summary = coordinator.collaboration_summary("loop-task")
        kinds = {item["kind"] for item in self.store.get("loop-task")["collaboration"]}

        self.assertEqual({"SEC-EVAL"}, {item.rule_id for item in findings})
        self.assertEqual(2, summary["agent_loop_steps"])
        self.assertEqual(1, summary["memories_recalled"])
        self.assertTrue({
            "memory_recalled", "context_prepared", "agent_loop_action",
            "agent_loop_observation",
        }.issubset(kinds))
        episodic = self.store.list_agent_memories(
            "org/repo", ("episodic",), 10
        )
        self.assertTrue(any(item["kind"] == "finding_approved" for item in episodic))
        self.assertTrue(any(item["kind"] == "task_summary" for item in episodic))
        working = self.store.list_agent_memories(
            "org/repo", ("working",), 10
        )
        self.assertEqual([], working)

    def test_memory_recall_purges_expired_records(self):
        self.store.save_agent_memory({
            "id": "expired-memory", "repository": "org/repo",
            "task_id": "old-task", "agent": "agent",
            "scope": "working", "kind": "observation", "content": "expired content",
            "keywords": ["expired"], "metadata": {}, "importance": 0.5,
            "created_at": utc_now(), "expires_at": "2000-01-01T00:00:00+00:00",
        })

        MemoryManager(self.store).recall("org/repo", "expired")

        self.assertEqual([], self.store.list_agent_memories(
            "org/repo", ("working",), 10
        ))

    def test_openai_reviewer_uses_runtime_tool_protocol(self):
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        parsed = parse_unified_diff(diff)

        class CannedReviewer(OpenAICompatibleReviewer):
            def __init__(self):
                super().__init__("https://example.invalid", "key", "model")
                self.responses = [
                    {
                        "action": "tool", "tool": "changed_line",
                        "arguments": {"path": "app.py", "line": 1},
                    },
                    {"action": "final", "findings": [{
                        "rule_id": "SEC-EVAL", "severity": "critical",
                        "title": "Dynamic execution",
                        "explanation": "The changed line executes input as code without validation.",
                        "path": "app.py", "line": 1, "evidence": "eval(data)",
                        "fix": "Replace eval with an explicit allow-listed parser.",
                        "test": "Add a regression test using an untrusted expression.",
                        "confidence": 0.9,
                    }]},
                ]

            def _request_json(self, _payload):
                return self.responses.pop(0)

        coordinator = MultiAgentCoordinator(
            [CannedReviewer()], judge=ApprovingJudge()
        )
        findings = coordinator.review(diff, parsed)

        self.assertEqual({"SEC-EVAL"}, {item.rule_id for item in findings})


if __name__ == "__main__":
    unittest.main()
