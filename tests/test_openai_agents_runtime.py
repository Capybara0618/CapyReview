import json
import unittest
from types import SimpleNamespace

from agents.testing import ScriptedModel, assistant_message, function_call

from capyreview.agents import MultiAgentCoordinator, finding_key
from capyreview.diff_parser import parse_unified_diff
from capyreview.reviewer import OpenAIAgentsSDKReviewer
from capyreview.review_skills import ReviewSkillRegistry, ReviewSkillSelector
from capyreview.runtime import (
    AgentTool, RuntimeBudgetExceeded, ToolRegistry,
)
from capyreview.sdk_runtime import OpenAIAgentsSDKLoop
from capyreview.service import ReviewService


class OpenAIAgentsSDKLoopTests(unittest.TestCase):
    def test_native_tool_call_is_checkpointed_and_returned_to_the_model(self):
        model = ScriptedModel([
            [function_call(
                "lookup", {"query": "eval"}, call_id="call-1",
            )],
            [assistant_message('{"findings":[]}')],
        ])
        tools = ToolRegistry([AgentTool(
            "lookup", "Look up code context.",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            lambda query: {"content": "found " + query},
        )])
        checkpoints = []

        result = OpenAIAgentsSDKLoop(model).run(
            agent_name="security-reviewer",
            instructions="Return JSON with a findings array.",
            input_text="Review the diff.",
            tools=tools,
            output_parser=json.loads,
            max_turns=2,
            checkpoint_sink=checkpoints.append,
        )

        self.assertEqual({"findings": []}, result.output)
        self.assertEqual("final", result.stop_reason)
        self.assertEqual(1, len(result.observations))
        self.assertEqual("lookup", result.observations[0]["tool"])
        self.assertTrue(result.observations[0]["ok"])
        self.assertEqual(2, checkpoints[-1]["next_step"])
        self.assertEqual(1, len(model.calls[0].tools))

    def test_tool_failure_becomes_a_model_visible_observation(self):
        model = ScriptedModel([
            [function_call("lookup", {}, call_id="call-1")],
            [assistant_message('{"findings":[]}')],
        ])
        tools = ToolRegistry([AgentTool(
            "lookup", "Look up code context.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda: (_ for _ in ()).throw(RuntimeError("repository unavailable")),
        )])

        result = OpenAIAgentsSDKLoop(model).run(
            agent_name="security-reviewer",
            instructions="Return JSON with a findings array.",
            input_text="Review the diff.",
            tools=tools,
            output_parser=json.loads,
            max_turns=2,
        )

        self.assertFalse(result.observations[0]["ok"])
        self.assertIn("repository unavailable", result.observations[0]["error"])

    def test_sdk_max_turns_maps_to_the_project_budget_error(self):
        model = ScriptedModel([[
            function_call("lookup", {}, call_id="call-1"),
        ]])
        tools = ToolRegistry([AgentTool(
            "lookup", "Look up code context.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda: {"content": "found"},
        )])

        with self.assertRaisesRegex(
            RuntimeBudgetExceeded, "step budget exceeded",
        ):
            OpenAIAgentsSDKLoop(model).run(
                agent_name="security-reviewer",
                instructions="Return JSON with a findings array.",
                input_text="Review the diff.",
                tools=tools,
                output_parser=json.loads,
                max_turns=1,
            )


class OpenAIAgentsSDKReviewerTests(unittest.TestCase):
    def test_service_builds_sdk_reviewers_for_production_assignments(self):
        service = object.__new__(ReviewService)
        service.settings = SimpleNamespace(timeout_seconds=3)
        service._llm_config = lambda: {
            "base_url": "https://api.deepseek.com",
            "api_key": "key",
            "model": "deepseek-chat",
        }

        reviewer = service._build_llm_reviewer(
            "Review added lines.", "security-reviewer", ("security",),
        )

        self.assertIsInstance(reviewer, OpenAIAgentsSDKReviewer)

    def test_reviewer_returns_typed_findings_from_sdk_final_output(self):
        model = ScriptedModel([[
            assistant_message(json.dumps({"findings": [{
                "rule_id": "CWE-95",
                "severity": "critical",
                "title": "Dynamic execution",
                "explanation": "Input reaches eval.",
                "path": "app.py",
                "line": 1,
                "evidence": "eval(user_input)",
                "fix": "Use a safe parser.",
                "test": "Exercise malicious input.",
                "confidence": 0.9,
                "evidence_refs": [],
            }]})),
        ]])
        diff = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+eval(user_input)\n"
        reviewer = OpenAIAgentsSDKReviewer(
            "https://api.deepseek.com", "key", "deepseek-chat",
            sdk_model=model,
        )

        result = reviewer.run_agent_loop(
            {
                "parsed": parse_unified_diff(diff),
                "managed_system_prompt": "Review added lines only.",
                "managed_context": "DIFF_CONTEXT:\n" + diff,
            },
            ToolRegistry(),
            max_turns=2,
        )

        self.assertEqual(1, len(result.output))
        self.assertEqual("CWE-95", result.output[0].rule_id)
        self.assertEqual("critical", result.output[0].severity.value)

    def test_coordinator_delegates_the_production_reviewer_loop_to_the_sdk(self):
        model = ScriptedModel([[
            assistant_message(json.dumps({"findings": [{
                "rule_id": "CWE-95",
                "severity": "critical",
                "title": "Dynamic execution",
                "explanation": "Input reaches eval.",
                "path": "app.py",
                "line": 1,
                "evidence": "eval(user_input)",
                "fix": "Use a safe parser.",
                "test": "Exercise malicious input.",
                "confidence": 0.9,
                "evidence_refs": [],
            }]})),
        ]])
        diff = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+eval(user_input)\n"
        parsed = parse_unified_diff(diff)
        reviewer = OpenAIAgentsSDKReviewer(
            "https://api.deepseek.com", "key", "deepseek-chat",
            sdk_model=model,
        )
        reviewer.name = "sdk-security-reviewer"
        reviewer.domains = ("security",)

        class ApprovingJudge:
            name = "approving-judge"

            def judge(self, _diff, _parsed, findings, _evidence):
                return {
                    finding_key(item): {
                        "approved": True, "reasons": [], "confidence": 0.9,
                    }
                    for item in findings
                }

        coordinator = MultiAgentCoordinator(
            [reviewer], judge=ApprovingJudge(), agent_retries=0,
            agent_loop_max_steps=2,
        )

        findings = coordinator.review(diff, parsed)

        self.assertEqual(1, len(findings))
        self.assertEqual("CWE-95", findings[0].rule_id)
        self.assertEqual(1, len(model.calls))

    def test_sdk_reviewer_loads_skill_and_updates_native_tool_catalog(self):
        model = ScriptedModel([
            [function_call(
                "load_review_skill",
                {"skill": "review-auth-security"},
                call_id="call-skill",
            )],
            [assistant_message(json.dumps({"findings": [{
                "rule_id": "SEC-AUTH",
                "severity": "critical",
                "title": "Signature bypass",
                "explanation": "A fixed value bypasses signature verification.",
                "path": "webhook.py",
                "line": 1,
                "evidence": "if signature == 'bypass':",
                "fix": "Remove the bypass.",
                "test": "Add a forged-signature test.",
                "confidence": 0.99,
                "evidence_refs": [],
            }]}))],
        ])
        diff = (
            "--- a/webhook.py\n+++ b/webhook.py\n@@ -0,0 +1,2 @@\n"
            "+if signature == 'bypass':\n+    return True\n"
        )
        parsed = parse_unified_diff(diff)
        reviewer = OpenAIAgentsSDKReviewer(
            "https://api.deepseek.com", "key", "deepseek-chat",
            sdk_model=model,
        )
        reviewer.name = "sdk-security-reviewer"
        reviewer.domains = ("security",)

        class ApprovingJudge:
            name = "approving-judge"

            def judge(self, _diff, _parsed, findings, _evidence):
                return {
                    finding_key(item): {
                        "approved": True, "reasons": [], "confidence": 0.9,
                    }
                    for item in findings
                }

        coordinator = MultiAgentCoordinator(
            [reviewer], judge=ApprovingJudge(), agent_retries=0,
            agent_loop_max_steps=3,
            skill_registry=ReviewSkillRegistry("skills"),
            skill_selector=ReviewSkillSelector(),
        )

        findings = coordinator.review(diff, parsed)

        first_tools = {tool.name for tool in model.calls[0].tools}
        second_tools = {tool.name for tool in model.calls[1].tools}
        self.assertEqual(1, len(findings))
        self.assertIn("load_review_skill", first_tools)
        self.assertNotIn("read_skill_reference", first_tools)
        self.assertNotIn("load_review_skill", second_tools)
        self.assertIn("read_skill_reference", second_tools)
        self.assertIn(
            "# Authentication Security Review",
            str(model.calls[1].input),
        )
        self.assertEqual(
            ["review-auth-security@1"],
            coordinator.collaboration_summary("")["activated_skills"],
        )


if __name__ == "__main__":
    unittest.main()
