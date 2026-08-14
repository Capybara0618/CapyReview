import unittest

from capyreview.agents import MultiAgentCoordinator, finding_key
from capyreview.context_manager import ContextManager
from capyreview.diff_parser import parse_unified_diff
from capyreview.memory import MemoryManager
from capyreview.mcp import GitHubMcpToolProvider
from capyreview.models import Finding, Severity
from capyreview.reviewer import OpenAICompatibleReviewer
from capyreview.review_skills import ReviewSkillRegistry, ReviewSkillSelector
from capyreview.runtime import AgentLoop, AgentRuntime, AgentTool, RuntimeNode, ToolRegistry
from capyreview.store import utc_now
from tests.fakes import InMemoryTaskStore


class FakeGitHubMcpClient:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "get_file_contents":
            return {"content": "old\neval(data)\nresult = helper(data)\n"}
        return {}


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
                return {
                    "action": "tool", "tool": "lookup", "arguments": {"key": "x"},
                    "usage": {
                        "llm_calls": 1, "prompt_tokens": 10,
                        "completion_tokens": 4, "total_tokens": 14,
                        "latency_ms": 20,
                    },
                }
            return {
                "action": "final", "output": state["observations"][0]["result"],
                "usage": {
                    "llm_calls": 1, "prompt_tokens": 12,
                    "completion_tokens": 3, "total_tokens": 15,
                    "latency_ms": 15,
                },
            }

        result = AgentLoop(max_steps=3, timeout_seconds=5).run(
            stepper, {"lookup": lambda key: "value:%s" % key}, {}
        )

        self.assertEqual("value:x", result.output)
        self.assertEqual(2, result.steps)
        self.assertEqual("lookup", result.observations[0]["tool"])
        self.assertEqual({
            "llm_calls": 2, "prompt_tokens": 22,
            "completion_tokens": 7, "total_tokens": 29,
            "latency_ms": 35,
        }, result.usage)

    def test_agent_loop_resumes_from_persisted_observation_without_repeating_tool(self):
        tool_calls = []
        checkpoints = []

        def interrupted_stepper(state):
            if not state.get("observations"):
                return {"action": "tool", "tool": "lookup", "arguments": {"key": "x"}}
            raise RuntimeError("provider interrupted after tool observation")

        loop = AgentLoop(max_steps=3, timeout_seconds=5)
        with self.assertRaisesRegex(RuntimeError, "provider interrupted"):
            loop.run(
                interrupted_stepper,
                {"lookup": lambda key: tool_calls.append(key) or "value:%s" % key},
                {}, checkpoint_sink=checkpoints.append,
            )

        def resumed_stepper(state):
            return {"action": "final", "output": state["observations"][0]["result"]}

        result = loop.run(
            resumed_stepper,
            {"lookup": lambda key: tool_calls.append(key) or "value:%s" % key},
            {}, resume_state=checkpoints[-1],
        )

        self.assertEqual("value:x", result.output)
        self.assertEqual(2, result.steps)
        self.assertEqual(["x"], tool_calls)
        self.assertEqual(2, checkpoints[-1]["next_step"])
        self.assertEqual("lookup", checkpoints[-1]["observations"][0]["tool"])

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

    def test_context_manager_batches_large_all_added_diff_without_losing_evidence(self):
        added = ["+value_%03d = %d\n" % (index, index) for index in range(300)]
        added[250] = "+result = eval(user_input)\n"
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1,300 @@\n" + "".join(added)

        bundles = ContextManager(max_tokens=512, reserved_tokens=64).build_batches(
            diff, {
                "risk_domains": ["security"],
                "objective": "find injection",
                "focus_lines": [{"path": "app.py", "line": 251}],
            }
        )

        rendered = "".join(bundle.text for bundle in bundles)
        self.assertGreater(len(bundles), 1)
        self.assertIn("eval(user_input)", rendered)
        self.assertEqual(300, sum(
            bundle.text.count("+value_") for bundle in bundles
        ) + rendered.count("+result = eval(user_input)"))
        self.assertTrue(all(
            bundle.strategy == "hunk-batch" for bundle in bundles
        ))

    def test_router_focus_does_not_change_diff_batch_content(self):
        ordinary = "x" * 90
        first = "".join(
            "+first_%02d = '%s'\n" % (index, ordinary) for index in range(8)
        )
        focused = "".join(
            "+focus_%02d = '%s'\n" % (index, ordinary) for index in range(8)
        )
        diff = (
            "--- a/app.py\n+++ b/app.py\n"
            "@@ -1 +1,8 @@\n" + first
            + "@@ -20 +20,8 @@\n" + focused
        )

        manager = ContextManager(max_tokens=512, reserved_tokens=256)
        focused_bundles = manager.build_batches(
            diff,
            {
                "agent": "security-reviewer",
                "focus_lines": [{"path": "app.py", "line": 24}],
            },
        )

        plain_bundles = manager.build_batches(
            diff, {"agent": "security-reviewer"}
        )

        self.assertEqual(
            [bundle.text for bundle in plain_bundles],
            [bundle.text for bundle in focused_bundles],
        )
        rendered = "".join(bundle.text for bundle in focused_bundles)
        self.assertIn("focus_04", rendered)
        self.assertIn("first_04", rendered)

    def test_context_manager_covers_every_changed_file_across_batches(self):
        dense = "q" * 45
        file_a = "--- a/a.py\n+++ b/a.py\n" + "".join(
            "@@ -{0} +{0},4 @@\n".format(1 + group * 10)
            + "".join(
                "+a_%d_%d = '%s'\n" % (group, line, dense)
                for line in range(4)
            )
            for group in range(4)
        )
        file_b = (
            "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n"
            "+b_value = '%s'\n" % ("b" * 180)
        )
        file_c = (
            "--- a/c.py\n+++ b/c.py\n@@ -1 +1 @@\n"
            "+c_value = '%s'\n" % ("c" * 180)
        )

        bundles = ContextManager(max_tokens=512, reserved_tokens=192).build_batches(
            file_a + file_b + file_c,
            {"agent": "correctness-reviewer"},
        )

        rendered = "".join(bundle.text for bundle in bundles)
        self.assertIn("+++ b/a.py", rendered)
        self.assertIn("b_value", rendered)
        self.assertIn("c_value", rendered)
        for group in range(4):
            for line in range(4):
                self.assertIn("+a_%d_%d" % (group, line), rendered)

    def test_memory_does_not_change_which_diff_hunk_is_selected(self):
        ordinary = "v" * 90
        first = "".join(
            "+first_%02d = '%s'\n" % (index, ordinary) for index in range(8)
        )
        second = "".join(
            "+remember_target_%02d = '%s'\n" % (index, ordinary)
            for index in range(8)
        )
        diff = (
            "--- a/app.py\n+++ b/app.py\n"
            "@@ -1 +1,8 @@\n" + first
            + "@@ -20 +20,8 @@\n" + second
        )
        manager = ContextManager(max_tokens=512, reserved_tokens=256)

        without_memory = manager.build(diff, {"agent": "correctness-reviewer"})
        with_memory = manager.build(
            diff,
            {"agent": "correctness-reviewer"},
            memories=[{
                "scope": "semantic",
                "kind": "review_feedback",
                "content": "remember_target requires attention",
            }],
        )

        self.assertEqual(without_memory.text, with_memory.text)

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
            "step": i, "tool": "search_repository", "ok": True,
            "result": "observation-%02d %s" % (i, "z" * 200),
        } for i in range(12)]

        managed = manager.compose(
            bundle, {"agent": "security", "objective": "review risky additions"},
            feedback=feedback, memories=memories, observations=observations,
            tools=[{
                "name": "search_repository", "description": "Search repository",
                "parameters": {},
            }],
        )

        self.assertLessEqual(managed.estimated_tokens, 512)
        self.assertTrue(managed.compressed)
        self.assertGreater(managed.dropped_observations, 0)
        self.assertGreater(managed.dropped_feedback + managed.dropped_memories, 0)
        self.assertIn("DIFF_CONTEXT", managed.text)

    def test_context_window_counts_system_and_never_drops_the_contract(self):
        manager = ContextManager(max_tokens=512, reserved_tokens=256)
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        system_prompt = "SYSTEM CONTRACT " + ("s" * 240)
        assignment = {"agent": "security", "objective": "review risky additions"}
        skills = [{
            "name": "review-auth-security", "version": 1,
            "body": "Inspect authentication trust boundaries.",
        }]
        tools = [{
            "name": "read_code_context", "description": "Read source context.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }]
        fixed_tokens = manager.contract_tokens(
            system_prompt, assignment, skills, tools
        )
        bundle = manager.build(diff, assignment, fixed_tokens=fixed_tokens)
        memories = [{
            "scope": "semantic", "kind": "review_feedback",
            "content": "memory-%02d %s" % (index, "m" * 240),
        } for index in range(10)]

        managed = manager.compose(
            bundle, assignment,
            system_prompt=system_prompt,
            skills=skills, tools=tools,
            memories=memories,
        )

        self.assertEqual(system_prompt, managed.system_prompt)
        self.assertIn("SKILL", managed.text)
        self.assertIn("TOOL", managed.text)
        self.assertLessEqual(
            manager.estimate_tokens(managed.system_prompt + "\n" + managed.text),
            512,
        )
        self.assertEqual(512, managed.manifest["budget_tokens"])
        self.assertGreater(managed.manifest["sections"]["system"], 0)
        self.assertEqual(1, managed.manifest["included"]["skills"])
        self.assertEqual(1, managed.manifest["included"]["tools"])
        self.assertGreater(managed.manifest["dropped"]["memories"], 0)

    def test_context_window_rejects_a_contract_that_cannot_fit(self):
        manager = ContextManager(max_tokens=512, reserved_tokens=64)
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
        assignment = {"agent": "correctness", "objective": "review"}
        skills = [{
            "name": "oversized-skill", "version": 1,
            "body": "x" * 2000,
        }]
        fixed_tokens = manager.contract_tokens(
            "fixed system", assignment, skills, ()
        )

        with self.assertRaisesRegex(ValueError, "fixed context"):
            manager.build(diff, assignment, fixed_tokens=fixed_tokens)

    def test_reviewer_uses_the_context_managed_system_prompt(self):
        parsed = parse_unified_diff(
            "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
        )

        class CapturingReviewer(OpenAICompatibleReviewer):
            def __init__(self):
                super().__init__("https://example.invalid", "key", "model")
                self.payload = {}

            def _request_json(self, payload):
                self.payload = payload
                return {"action": "final", "findings": []}

        reviewer = CapturingReviewer()
        reviewer.agent_step({
            "parsed": parsed,
            "managed_context": "bounded user context",
            "managed_system_prompt": "bounded system contract",
            "available_tools": [],
        })

        self.assertEqual(
            "bounded system contract",
            reviewer.payload["messages"][0]["content"],
        )

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

    def test_judge_receives_only_explicitly_referenced_mcp_evidence(self):
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        parsed = parse_unified_diff(diff)

        class EvidenceToolProvider:
            def registry(self, _context):
                return ToolRegistry([AgentTool(
                    "read_code_context", "Read related source context.",
                    {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "line": {"type": "integer"},
                        },
                        "required": ["path", "line"],
                        "additionalProperties": False,
                    },
                    lambda path, line: {
                        "path": path, "start_line": line, "end_line": line,
                        "content": "def execute(value): return eval(value)",
                    },
                )])

        class EvidenceAwareSpecialist:
            name = "security-specialist"
            domains = ("security",)

            def agent_step(self, state):
                if not state.get("observations"):
                    return {
                        "action": "tool", "tool": "read_code_context",
                        "arguments": {"path": "app.py", "line": 1},
                    }
                if state["observations"][0]["id"] != "O1":
                    raise AssertionError("tool observations require stable sequential ids")
                line = state["parsed"].added_lines[0]
                return {"action": "final", "findings": [Finding(
                    "CWE-95", Severity.CRITICAL, "Dynamic execution",
                    "The changed line executes untrusted input.",
                    line.path, line.line, line.content,
                    "Replace eval.", "Add a regression test.", 0.95,
                    evidence_refs=["O1"],
                )]}

        class EvidenceCapturingJudge(ApprovingJudge):
            def __init__(self):
                self.supporting = []

            def judge(self, diff_context, parsed_diff, findings, evidence):
                self.supporting = evidence[finding_key(findings[0])].supporting_evidence
                return super().judge(diff_context, parsed_diff, findings, evidence)

        judge = EvidenceCapturingJudge()
        coordinator = MultiAgentCoordinator(
            [EvidenceAwareSpecialist()], judge=judge,
            tool_provider=EvidenceToolProvider(),
        )

        findings = coordinator.review_with_context(
            "evidence-packet", diff, parsed, repository="org/repo",
            head_commit="commit-1",
        )

        self.assertEqual(1, len(findings))
        self.assertEqual([{
            "source": "read_code_context", "path": "app.py", "line": 1,
            "content": "def execute(value): return eval(value)",
        }], judge.supporting)

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

    def test_memory_rejects_transient_working_state(self):
        memory = MemoryManager(self.store)

        with self.assertRaisesRegex(ValueError, "unsupported memory scope"):
            memory.remember(
                "org/repo", "working", "agent_loop_observation",
                "Tool output belongs in the loop checkpoint, not long-term memory.",
            )

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
            managed_calls = []

            def review(self, _diff, _parsed):
                return []

            def agent_step(self, state):
                self.managed_calls.append({
                    "system": state.get("managed_system_prompt", ""),
                    "metadata": dict(state.get("context_metadata") or {}),
                })
                if not state.get("observations"):
                    return {
                        "action": "tool", "tool": "read_code_context",
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
            tool_provider=GitHubMcpToolProvider(FakeGitHubMcpClient()),
            agent_loop_max_steps=2,
        )
        findings = coordinator.review_with_context(
            "loop-task", diff, parsed, repository="org/repo", head_commit="abc123"
        )
        summary = coordinator.collaboration_summary("loop-task")
        kinds = {item["kind"] for item in self.store.get("loop-task")["collaboration"]}

        self.assertEqual({"SEC-EVAL"}, {item.rule_id for item in findings})
        self.assertEqual(2, summary["agent_loop_steps"])
        self.assertEqual(1, summary["memories_recalled"])
        self.assertTrue(all(item["system"] for item in LoopSpecialist.managed_calls))
        manifests = [item["metadata"]["manifest"] for item in LoopSpecialist.managed_calls]
        self.assertGreater(manifests[0]["included"]["tools"], 0)
        self.assertEqual(0, manifests[-1]["included"]["tools"])
        self.assertTrue(all(
            item["total_input_tokens"] <= item["budget_tokens"]
            for item in manifests
        ))
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

    def test_agent_loop_exposes_commit_pinned_repository_context_tool(self):
        diff = "--- a/app.py\n+++ b/app.py\n@@ -3 +3 @@\n-old\n+result = helper(data)\n"
        parsed = parse_unified_diff(diff)
        client = FakeGitHubMcpClient()

        class RepositoryAwareSpecialist:
            name = "correctness-specialist"
            domains = ("correctness",)

            def agent_step(self, state):
                if not state.get("observations"):
                    self.assert_tool(state)
                    return {
                        "action": "tool", "tool": "read_code_context",
                        "arguments": {"path": "app.py", "line": 3},
                    }
                line = state["parsed"].added_lines[0]
                return {"action": "final", "findings": [Finding(
                    "COR-HELPER", Severity.HIGH, "Unsafe helper call",
                    "The called helper dynamically executes its input.",
                    line.path, line.line, line.content,
                    "Replace the helper with a typed parser.",
                    "Add an untrusted-input regression test.", 0.9,
                )]}

            @staticmethod
            def assert_tool(state):
                names = {item["name"] for item in state["available_tools"]}
                if "read_code_context" not in names:
                    raise AssertionError("repository context tool is unavailable")

        coordinator = MultiAgentCoordinator(
            [RepositoryAwareSpecialist()], judge=ApprovingJudge(),
            tool_provider=GitHubMcpToolProvider(client),
        )
        findings = coordinator.review_with_context(
            "", diff, parsed, repository="org/repo", head_commit="abc123",
            pull_request=7,
        )

        self.assertEqual({"COR-HELPER"}, {item.rule_id for item in findings})
        self.assertEqual([(
            "get_file_contents",
            {"owner": "org", "repo": "repo", "path": "app.py", "sha": "abc123"},
        )], client.calls)

    def test_failed_tool_observation_is_archived_for_skill_evolution(self):
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        parsed = parse_unified_diff(diff)
        self.store.create("tool-learning", "org/repo", 13, {})
        client = FakeGitHubMcpClient()

        class CorrectingToolSpecialist:
            name = "security-specialist"
            domains = ("security",)

            def agent_step(self, state):
                if not state.get("observations"):
                    return {
                        "action": "tool", "tool": "read_code_context",
                        "arguments": {"path": "app.py", "line": "first"},
                    }
                line = state["parsed"].added_lines[0]
                return {"action": "final", "findings": [Finding(
                    "SEC-EVAL", Severity.CRITICAL, "Dynamic execution",
                    "The changed line executes untrusted input as code.",
                    line.path, line.line, line.content,
                    "Replace eval with a parser.", "Add an input test.", 0.9,
                )]}

        coordinator = MultiAgentCoordinator(
            [CorrectingToolSpecialist()], store=self.store,
            judge=ApprovingJudge(),
            tool_provider=GitHubMcpToolProvider(client),
        )
        findings = coordinator.review_with_context(
            "tool-learning", diff, parsed, repository="org/repo",
            head_commit="abc123",
        )
        cases = self.store.list_task_failure_cases("tool-learning")

        self.assertEqual(1, len(findings))
        self.assertEqual([], client.calls)
        self.assertEqual("tool_error", cases[0]["category"])
        self.assertEqual("read_code_context", cases[0]["payload"]["tool"])
        self.assertFalse(cases[0]["payload"]["evolution_eligible"])

    def test_security_reviewer_receives_selected_skill_before_agent_loop(self):
        diff = "--- a/capyreview/github.py\n+++ b/capyreview/github.py\n@@ -10 +10,3 @@\n-old\n+if signature == 'bypass':\n+    return True\n"
        parsed = parse_unified_diff(diff)

        class SkillAwareSpecialist:
            name = "security-specialist"
            domains = ("security",)

            def agent_step(self, state):
                if not state.get("observations"):
                    if "# Authentication Security Review" not in state["managed_context"]:
                        raise AssertionError("selected SKILL.md was not loaded before the loop")
                    names = {item["name"] for item in state["available_tools"]}
                    if "load_review_skill" in names:
                        raise AssertionError("Skill must not be selected twice")
                    if "read_skill_reference" not in names:
                        raise AssertionError("selected Skill reference tool is unavailable")
                    return {
                        "action": "tool", "tool": "read_skill_reference",
                        "arguments": {
                            "skill": "review-auth-security",
                            "path": "references/threat-patterns.md",
                        },
                    }
                if "fixed development bypass" not in str(state["observations"]):
                    raise AssertionError("Skill reference content was not observed")
                line = state["parsed"].added_lines[0]
                return {"action": "final", "findings": [Finding(
                    "SEC-AUTH", Severity.CRITICAL, "Signature bypass",
                    "A fixed value bypasses signature verification.",
                    line.path, line.line, line.content,
                    "Remove the bypass.", "Add a forged-signature test.", 0.99,
                )]}

        coordinator = MultiAgentCoordinator(
            [SkillAwareSpecialist()], judge=ApprovingJudge(),
            skill_registry=ReviewSkillRegistry("skills"),
            skill_selector=ReviewSkillSelector(),
        )

        findings = coordinator.review(diff, parsed)
        summary = coordinator.collaboration_summary("")

        self.assertEqual(1, len(findings))
        self.assertEqual(1, summary["tool_calls"])
        self.assertEqual(
            ["review-auth-security@1"], summary["activated_skills"]
        )

    def test_unrelated_diff_does_not_load_a_review_skill(self):
        diff = "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+Clarify installation.\n"
        parsed = parse_unified_diff(diff)

        class DocumentationSpecialist:
            name = "correctness-specialist"
            domains = ("correctness",)

            def agent_step(self, state):
                if "SKILL.md" in state["managed_context"]:
                    raise AssertionError("unrelated Skill was loaded")
                names = {item["name"] for item in state["available_tools"]}
                if "read_skill_reference" in names:
                    raise AssertionError("reference tool should require an active Skill")
                return {"action": "final", "findings": []}

        coordinator = MultiAgentCoordinator(
            [DocumentationSpecialist()], judge=ApprovingJudge(),
            skill_registry=ReviewSkillRegistry("skills"),
            skill_selector=ReviewSkillSelector(),
        )

        coordinator.review(diff, parsed)

        self.assertEqual([], coordinator.collaboration_summary("")["activated_skills"])

    def test_coordinator_reserves_the_last_loop_step_for_final_output(self):
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        parsed = parse_unified_diff(diff)
        client = FakeGitHubMcpClient()

        class EvidenceHungrySpecialist:
            name = "security-specialist"
            domains = ("security",)

            def agent_step(self, state):
                if state.get("available_tools"):
                    return {
                        "action": "tool", "tool": "read_code_context",
                        "arguments": {"path": "app.py", "line": 1},
                    }
                line = state["parsed"].added_lines[0]
                return {"action": "final", "findings": [Finding(
                    "SEC-EVAL", Severity.CRITICAL, "Dynamic execution",
                    "The changed line executes data as code.",
                    line.path, line.line, line.content,
                    "Replace eval with a parser.", "Add an input test.", 0.9,
                )]}

        coordinator = MultiAgentCoordinator(
            [EvidenceHungrySpecialist()], judge=ApprovingJudge(),
            agent_loop_max_steps=3,
            tool_provider=GitHubMcpToolProvider(client),
        )

        findings = coordinator.review_with_context(
            "", diff, parsed, repository="org/repo", head_commit="abc123"
        )

        self.assertEqual({"SEC-EVAL"}, {item.rule_id for item in findings})
        self.assertEqual(2, len(client.calls))
        self.assertEqual(2, coordinator.collaboration_summary("")["tool_calls"])

    def test_coordinator_restores_completed_reviewer_result_without_calling_llm(self):
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        parsed = parse_unified_diff(diff)
        self.store.create("reviewer-resume", "org/repo", 8, {})
        finding = Finding(
            "SEC-EVAL", Severity.CRITICAL, "Dynamic execution",
            "The changed line executes data as code.",
            "app.py", 1, "eval(data)",
            "Replace eval with an allow-listed parser.",
            "Add an untrusted-input regression test.", 0.9,
        )
        self.store.save_checkpoint(
            "reviewer-resume", "reviewer:A01",
            {
                "agent": "security-specialist",
                "findings": [finding.to_dict()],
                "attempts": 1,
                "execution": {"steps": 1, "tool_calls": 0},
                "substituted_for": "",
            },
        )

        class MustNotRunSpecialist:
            name = "security-specialist"
            domains = ("security",)

            def review(self, _diff, _parsed):
                raise AssertionError("completed reviewer checkpoint was ignored")

        coordinator = MultiAgentCoordinator(
            [MustNotRunSpecialist()], store=self.store, judge=ApprovingJudge()
        )
        findings = coordinator.review_with_context(
            "reviewer-resume", diff, parsed, repository="org/repo"
        )
        events = self.store.get("reviewer-resume")["collaboration"]

        self.assertEqual([finding.to_dict()], [item.to_dict() for item in findings])
        self.assertTrue(any(
            item["kind"] == "checkpoint_restored"
            and item["content"].get("node") == "reviewer:A01"
            for item in events
        ))

    def test_coordinator_resumes_agent_loop_observation_and_clears_it_after_final(self):
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        parsed = parse_unified_diff(diff)
        self.store.create("loop-resume", "org/repo", 10, {})

        class InterruptedSpecialist:
            name = "security-specialist"
            domains = ("security",)

            def agent_step(self, state):
                if not state.get("observations"):
                    return {
                        "action": "tool", "tool": "read_code_context",
                        "arguments": {"path": "app.py", "line": 1},
                    }
                raise RuntimeError("provider interrupted after observation")

        mcp_client = FakeGitHubMcpClient()
        first = MultiAgentCoordinator(
            [InterruptedSpecialist()], store=self.store,
            agent_retries=0, judge=ApprovingJudge(),
            tool_provider=GitHubMcpToolProvider(mcp_client),
        )
        with self.assertRaisesRegex(RuntimeError, "provider interrupted"):
            first.review_with_context(
                "loop-resume", diff, parsed, repository="org/repo",
                head_commit="abc123",
            )

        loop_checkpoint = self.store.load_checkpoints("loop-resume")["loop:A01"]
        self.assertEqual("running", loop_checkpoint["status"])
        self.assertEqual(2, loop_checkpoint["state"]["next_step"])

        class ResumedSpecialist:
            name = "security-specialist"
            domains = ("security",)

            def agent_step(self, state):
                if not state.get("observations"):
                    raise AssertionError("the completed tool call was repeated")
                line = state["parsed"].added_lines[0]
                return {"action": "final", "findings": [Finding(
                    "SEC-EVAL", Severity.CRITICAL, "Dynamic execution",
                    "The changed line executes data as code.",
                    line.path, line.line, line.content,
                    "Replace eval with an allow-listed parser.",
                    "Add an untrusted-input regression test.", 0.9,
                )]}

        second = MultiAgentCoordinator(
            [ResumedSpecialist()], store=self.store,
            agent_retries=0, judge=ApprovingJudge(),
            tool_provider=GitHubMcpToolProvider(mcp_client),
        )
        findings = second.review_with_context(
            "loop-resume", diff, parsed, repository="org/repo",
            head_commit="abc123",
        )
        recovery = second.collaboration_summary("loop-resume")["recovery"]
        checkpoints = self.store.load_checkpoints("loop-resume")
        kinds = [
            item["kind"] for item in self.store.get("loop-resume")["collaboration"]
        ]

        self.assertEqual({"SEC-EVAL"}, {item.rule_id for item in findings})
        self.assertNotIn("loop:A01", checkpoints)
        self.assertIn("reviewer:A01", checkpoints)
        self.assertIn("agent_loop_checkpoint_restored", kinds)
        self.assertGreaterEqual(recovery["checkpoints_saved"], 2)
        self.assertEqual(1, recovery["checkpoints_restored"])
        self.assertEqual(1, recovery["checkpoints_cleared"])
        self.assertEqual(1, len(mcp_client.calls))

    def test_coordinator_restores_matching_judge_decision_without_calling_llm(self):
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        parsed = parse_unified_diff(diff)
        self.store.create("judge-resume", "org/repo", 9, {})
        finding = Finding(
            "SEC-EVAL", Severity.CRITICAL, "Dynamic execution",
            "The changed line executes data as code.",
            "app.py", 1, "eval(data)",
            "Replace eval with an allow-listed parser.",
            "Add an untrusted-input regression test.", 0.9,
        )
        key = finding_key(finding)
        self.store.save_checkpoint(
            "judge-resume", "judge",
            {
                "candidate_keys": [key],
                "decisions": {
                    key: {
                        "finding_key": key, "approved": True,
                        "reasons": [], "confidence": 0.95,
                    },
                },
                "judge_context": {"restored": True},
            },
        )

        class SecuritySpecialist:
            name = "security-specialist"
            domains = ("security",)

            def review(self, _diff, _parsed):
                return [finding]

        class MustNotRunJudge:
            name = "llm-review-judge"

            def judge(self, _diff, _parsed, _findings, _evidence):
                raise AssertionError("matching judge checkpoint was ignored")

        coordinator = MultiAgentCoordinator(
            [SecuritySpecialist()], store=self.store, judge=MustNotRunJudge()
        )
        findings = coordinator.review_with_context(
            "judge-resume", diff, parsed, repository="org/repo"
        )
        events = self.store.get("judge-resume")["collaboration"]

        self.assertEqual([finding.to_dict()], [item.to_dict() for item in findings])
        self.assertTrue(any(
            item["kind"] == "checkpoint_restored"
            and item["content"].get("node") == "judge"
            for item in events
        ))

    def test_coordinator_restores_completed_reflection_without_repeating_reviewer(self):
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        parsed = parse_unified_diff(diff)
        self.store.create("reflection-resume", "org/repo", 11, {})

        class CorrectingSpecialist:
            name = "security-specialist"
            domains = ("security",)

            def agent_step(self, state):
                line = state["parsed"].added_lines[0]
                evidence = line.content if state.get("feedback") else "eval(other)"
                return {"action": "final", "findings": [Finding(
                    "SEC-EVAL", Severity.CRITICAL, "Dynamic execution",
                    "The added line executes untrusted input as code.",
                    line.path, line.line, evidence,
                    "Replace eval with an allow-listed parser.",
                    "Add an untrusted-input regression test.", 0.9,
                )]}

        first = MultiAgentCoordinator(
            [CorrectingSpecialist()], store=self.store,
            agent_retries=0, judge=ApprovingJudge(),
        )
        first.review_with_context(
            "reflection-resume", diff, parsed, repository="org/repo",
        )
        archived_before_resume = self.store.list_task_failure_cases(
            "reflection-resume"
        )
        self.store.delete_checkpoint("reflection-resume", "judge")

        class MustNotRunSpecialist:
            name = "security-specialist"
            domains = ("security",)

            def review(self, _diff, _parsed):
                raise AssertionError("completed reviewer or reflection was repeated")

        second = MultiAgentCoordinator(
            [MustNotRunSpecialist()], store=self.store,
            agent_retries=0, judge=ApprovingJudge(),
        )
        findings = second.review_with_context(
            "reflection-resume", diff, parsed, repository="org/repo",
        )
        events = self.store.get("reflection-resume")["collaboration"]
        archived_after_resume = self.store.list_task_failure_cases(
            "reflection-resume"
        )

        self.assertEqual("eval(data)", findings[0].evidence)
        self.assertEqual([], archived_before_resume)
        self.assertEqual([], archived_after_resume)
        self.assertTrue(any(
            item["kind"] == "checkpoint_restored"
            and item["content"].get("node") == "reflection:evidence:A01"
            for item in events
        ))

    def test_judge_checkpoint_restores_the_corrected_finding_content(self):
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        parsed = parse_unified_diff(diff)
        self.store.create("judge-reflection-resume", "org/repo", 12, {})

        class CorrectingSpecialist:
            name = "security-specialist"
            domains = ("security",)

            def agent_step(self, state):
                line = state["parsed"].added_lines[0]
                explanation = (
                    "Attacker-controlled data reaches eval and executes arbitrary code."
                    if state.get("feedback") else "The call may be unsafe."
                )
                return {"action": "final", "findings": [Finding(
                    "SEC-EVAL", Severity.CRITICAL, "Dynamic execution",
                    explanation, line.path, line.line, line.content,
                    "Replace eval with a parser.", "Add an input test.", 0.9,
                )]}

        class ReflectingJudge:
            name = "llm-review-judge"

            def __init__(self):
                self.calls = 0

            def judge(self, _diff, _parsed, findings, _evidence):
                self.calls += 1
                return {
                    finding_key(finding): {
                        "approved": self.calls > 1,
                        "reasons": [] if self.calls > 1 else ["missing attacker path"],
                        "confidence": 0.9,
                    }
                    for finding in findings
                }

        first_judge = ReflectingJudge()
        first = MultiAgentCoordinator(
            [CorrectingSpecialist()], store=self.store,
            agent_retries=0, judge=first_judge,
        )
        first_result = first.review_with_context(
            "judge-reflection-resume", diff, parsed, repository="org/repo",
        )

        class MustNotRunJudge:
            name = "llm-review-judge"

            def judge(self, *_args):
                raise AssertionError("completed judge checkpoint was ignored")

        second = MultiAgentCoordinator(
            [CorrectingSpecialist()], store=self.store,
            agent_retries=0, judge=MustNotRunJudge(),
        )
        restored = second.review_with_context(
            "judge-reflection-resume", diff, parsed, repository="org/repo",
        )

        self.assertIn("Attacker-controlled", first_result[0].explanation)
        self.assertEqual(first_result[0].to_dict(), restored[0].to_dict())

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
                        "action": "tool", "tool": "read_code_context",
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
                self._last_usage = {
                    "llm_calls": 1, "prompt_tokens": 20,
                    "completion_tokens": 5, "total_tokens": 25,
                    "latency_ms": 10,
                }
                return self.responses.pop(0)

        class UsageJudge(ApprovingJudge):
            def __init__(self):
                self.usage = {
                    "llm_calls": 1, "prompt_tokens": 30,
                    "completion_tokens": 6, "total_tokens": 36,
                    "latency_ms": 12,
                }

            def consume_usage(self):
                value = dict(self.usage)
                self.usage = {}
                return value

        coordinator = MultiAgentCoordinator(
            [CannedReviewer()], judge=UsageJudge(),
            tool_provider=GitHubMcpToolProvider(FakeGitHubMcpClient()),
        )
        findings = coordinator.review_with_context(
            "", diff, parsed, repository="org/repo", head_commit="abc123"
        )
        usage = coordinator.collaboration_summary("")["usage"]

        self.assertEqual({"SEC-EVAL"}, {item.rule_id for item in findings})
        self.assertEqual({
            "llm_calls": 3, "prompt_tokens": 70,
            "completion_tokens": 16, "total_tokens": 86,
            "latency_ms": 32,
        }, usage)


if __name__ == "__main__":
    unittest.main()
