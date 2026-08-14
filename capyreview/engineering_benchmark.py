"""Reproducible engineering benchmarks for runtime recovery and context bounds."""
from datetime import datetime, timezone
from typing import Any, Dict, List

from .agents import MultiAgentCoordinator, finding_key
from .context_manager import ContextManager
from .diff_parser import parse_unified_diff
from .models import Finding, Severity
from .runtime import (
    AgentLoop,
    AgentRuntime,
    AgentTool,
    RuntimeBudgetExceeded,
    RuntimeNode,
    ToolRegistry,
)


class _MemoryCheckpointStore:
    def __init__(self):
        self.values: Dict[str, Dict[str, dict]] = {}
        self.messages: Dict[str, List[dict]] = {}

    def load_checkpoints(self, task_id: str) -> Dict[str, dict]:
        return {
            key: dict(value)
            for key, value in self.values.get(task_id, {}).items()
        }

    def save_checkpoint(
        self, task_id: str, node: str, state: Dict[str, Any],
        status: str = "completed", attempt: int = 1, error: str = "",
    ) -> None:
        self.values.setdefault(task_id, {})[node] = {
            "status": status, "attempt": attempt, "state": dict(state),
            "error": error,
        }

    def delete_checkpoint(self, task_id: str, node: str) -> bool:
        values = self.values.get(task_id, {})
        if node not in values:
            return False
        del values[node]
        return True

    def record_agent_message(self, task_id: str, message: Dict[str, Any]) -> None:
        self.messages.setdefault(task_id, []).append(dict(message))


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _runtime_events():
    values = []

    def collect(event):
        values.append({
            "kind": event.kind, "node": event.node, "step": event.step,
            "attempt": event.attempt, "detail": event.detail,
        })

    return values, collect


def _transient_retry_case(index: int) -> dict:
    attempts = {"count": 0}
    events, sink = _runtime_events()

    def flaky(_state):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ConnectionError("injected transient dependency failure")
        return {"result": "recovered-%02d" % index}

    output = AgentRuntime(max_steps=3, timeout_seconds=5, node_retries=1).execute(
        {}, [RuntimeNode("review", flaky)], event_sink=sink
    )
    kinds = [item["kind"] for item in events]
    return {
        "id": "transient-%02d" % index,
        "scenario": "transient_node_retry", "recoverable": True,
        "recovered": output.get("result") == "recovered-%02d" % index,
        "contained": True, "state_consistent": attempts["count"] == 2,
        "trace_complete": "node_failed" in kinds and "node_completed" in kinds,
        "duplicate_side_effects": 0, "events": events,
    }


def _tool_recovery_case(index: int) -> dict:
    calls = []
    events: List[dict] = []
    registry = ToolRegistry([AgentTool(
        "lookup", "Look up one value.",
        {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"], "additionalProperties": False,
        },
        lambda key: calls.append(key) or "value:%s" % key,
    )])

    def stepper(state):
        observations = state.get("observations") or []
        if not observations:
            return {"action": "tool", "tool": "lookup", "arguments": {"key": 7}}
        if len(observations) == 1:
            return {
                "action": "tool", "tool": "lookup",
                "arguments": {"key": "item-%02d" % index},
            }
        return {"action": "final", "output": observations[-1]["result"]}

    result = AgentLoop(max_steps=3, timeout_seconds=5).run(
        stepper, registry, {},
        event_sink=lambda kind, detail: events.append({"kind": kind, **detail}),
    )
    observations = result.observations
    return {
        "id": "tool-%02d" % index,
        "scenario": "tool_argument_recovery", "recoverable": True,
        "recovered": result.output == "value:item-%02d" % index,
        "contained": True,
        "state_consistent": calls == ["item-%02d" % index],
        "trace_complete": (
            len(observations) == 2 and not observations[0]["ok"]
            and observations[1]["ok"] and result.stop_reason == "final"
        ),
        "duplicate_side_effects": 0, "events": events,
    }


def _checkpoint_resume_case(index: int) -> dict:
    store = _MemoryCheckpointStore()
    task_id = "resume-%02d" % index
    calls = {"prepare": 0, "review": 0}
    events, sink = _runtime_events()

    def prepare(_state):
        calls["prepare"] += 1
        return {"prepared": index}

    def review(state):
        calls["review"] += 1
        if calls["review"] == 1:
            raise OSError("injected worker interruption")
        return {"result": state["prepared"] * 2}

    nodes = [RuntimeNode("prepare", prepare), RuntimeNode("review", review)]
    runtime = AgentRuntime(max_steps=4, timeout_seconds=5)
    try:
        runtime.execute({}, nodes, task_id, store, event_sink=sink)
    except OSError:
        pass
    output = runtime.execute({}, nodes, task_id, store, event_sink=sink)
    kinds = [item["kind"] for item in events]
    return {
        "id": task_id, "scenario": "checkpoint_resume", "recoverable": True,
        "recovered": output.get("result") == index * 2,
        "contained": True,
        "state_consistent": calls == {"prepare": 1, "review": 2},
        "trace_complete": (
            "node_failed" in kinds and "checkpoint_restored" in kinds
            and "node_completed" in kinds
        ),
        "duplicate_side_effects": 0, "events": events,
    }


def _duplicate_delivery_case(index: int) -> dict:
    store = _MemoryCheckpointStore()
    task_id = "duplicate-%02d" % index
    side_effects = []
    events, sink = _runtime_events()

    def publish(_state):
        side_effects.append("published-%02d" % index)
        return {"publication_id": "publication-%02d" % index}

    runtime = AgentRuntime(max_steps=2, timeout_seconds=5)
    nodes = [RuntimeNode("publish", publish)]
    first = runtime.execute({}, nodes, task_id, store, event_sink=sink)
    second = runtime.execute({}, nodes, task_id, store, event_sink=sink)
    duplicate_side_effects = max(0, len(side_effects) - 1)
    kinds = [item["kind"] for item in events]
    return {
        "id": task_id, "scenario": "duplicate_delivery", "recoverable": True,
        "recovered": first == second,
        "contained": duplicate_side_effects == 0,
        "state_consistent": len(side_effects) == 1,
        "trace_complete": (
            "node_completed" in kinds and "checkpoint_restored" in kinds
        ),
        "duplicate_side_effects": duplicate_side_effects, "events": events,
    }


def _budget_containment_case(index: int) -> dict:
    store = _MemoryCheckpointStore()
    task_id = "budget-%02d" % index
    calls = []
    events, sink = _runtime_events()
    nodes = [
        RuntimeNode("bounded-step", lambda _state: calls.append("first") or {"safe": True}),
        RuntimeNode("forbidden-step", lambda _state: calls.append("second") or {"bad": True}),
    ]
    contained = False
    try:
        AgentRuntime(max_steps=1, timeout_seconds=5).execute(
            {}, nodes, task_id, store, event_sink=sink
        )
    except RuntimeBudgetExceeded:
        contained = True
    checkpoint = store.load_checkpoints(task_id)
    kinds = [item["kind"] for item in events]
    return {
        "id": task_id, "scenario": "budget_containment", "recoverable": False,
        "recovered": False, "contained": contained,
        "state_consistent": (
            calls == ["first"] and checkpoint["bounded-step"]["state"] == {"safe": True}
        ),
        "trace_complete": (
            "node_completed" in kinds and "budget_exhausted" in kinds
        ),
        "duplicate_side_effects": 0, "events": events,
    }


def run_fault_injection_benchmark() -> Dict[str, Any]:
    """Run 50 deterministic failures across five runtime failure classes."""
    results = []
    factories = (
        _transient_retry_case, _tool_recovery_case, _checkpoint_resume_case,
        _duplicate_delivery_case, _budget_containment_case,
    )
    for factory in factories:
        results.extend(factory(index) for index in range(1, 11))
    recoverable = [item for item in results if item["recoverable"]]
    terminal = [item for item in results if not item["recoverable"]]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cases": len(results),
        "recoverable_cases": len(recoverable),
        "expected_terminal_cases": len(terminal),
        "fault_recovery_rate": _ratio(
            sum(item["recovered"] for item in recoverable), len(recoverable)
        ),
        "fault_containment_rate": _ratio(
            sum(item["contained"] for item in terminal), len(terminal)
        ),
        "state_consistency_rate": _ratio(
            sum(item["state_consistent"] for item in results), len(results)
        ),
        "trace_completeness_rate": _ratio(
            sum(item["trace_complete"] for item in results), len(results)
        ),
        "duplicate_side_effects": sum(
            item["duplicate_side_effects"] for item in results
        ),
        "case_results": results,
    }


_RECOVERY_DIFF = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old = value
+result = eval(value)
"""


def _benchmark_finding() -> Finding:
    return Finding(
        "SEC-EVAL", Severity.CRITICAL, "Dynamic execution",
        "The added line executes input as code.",
        "app.py", 1, "result = eval(value)",
        "Replace eval with an allow-listed parser.",
        "Add an untrusted-input regression test.", 0.9,
    )


class _ApprovingJudge:
    name = "benchmark-judge"

    def judge(self, _diff, _parsed, _findings, evidence):
        return {
            key: {"approved": report.grounded, "reasons": [], "confidence": 0.9}
            for key, report in evidence.items()
        }


def _agent_loop_observation_case(index: int) -> dict:
    calls = []
    checkpoints = []
    events = []
    loop = AgentLoop(max_steps=3, timeout_seconds=5)

    def interrupted(state):
        if not state.get("observations"):
            return {
                "action": "tool", "tool": "lookup",
                "arguments": {"key": "item-%02d" % index},
            }
        raise ConnectionError("injected provider interruption")

    try:
        loop.run(
            interrupted,
            {"lookup": lambda key: calls.append(key) or "value:%s" % key},
            {},
            event_sink=lambda kind, detail: events.append({"kind": kind, **detail}),
            checkpoint_sink=checkpoints.append,
        )
    except ConnectionError:
        pass
    result = loop.run(
        lambda state: {
            "action": "final", "output": state["observations"][0]["result"]
        },
        {"lookup": lambda key: calls.append(key) or "value:%s" % key},
        {}, resume_state=checkpoints[-1],
    )
    expected = "item-%02d" % index
    return {
        "id": "loop-observation-%02d" % index,
        "scenario": "agent_loop_observation",
        "recovered": result.output == "value:%s" % expected,
        "state_consistent": result.steps == 2 and len(result.observations) == 1,
        "trace_complete": bool(
            checkpoints and any(item["kind"] == "agent_loop_observation" for item in events)
        ),
        "duplicate_llm_calls": max(0, len(calls) - 1),
    }


def _reviewer_final_case(index: int) -> dict:
    store = _MemoryCheckpointStore()
    task_id = "reviewer-final-%02d" % index
    finding = _benchmark_finding()
    store.save_checkpoint(task_id, "reviewer:A01", {
        "agent": "security-specialist",
        "findings": [finding.to_dict()],
        "attempts": 1,
        "execution": {"loop_steps": 1, "tool_calls": 0},
        "substituted_for": "",
    })
    calls = []

    class MustNotRunReviewer:
        name = "security-specialist"
        domains = ("security",)

        def review(self, _diff, _parsed):
            calls.append("reviewer")
            raise AssertionError("reviewer checkpoint was ignored")

    coordinator = MultiAgentCoordinator(
        [MustNotRunReviewer()], store=store, judge=_ApprovingJudge()
    )
    result = coordinator.review_with_context(
        task_id, _RECOVERY_DIFF, parse_unified_diff(_RECOVERY_DIFF), "org/repo"
    )
    kinds = [item.get("kind") for item in store.messages.get(task_id, [])]
    return {
        "id": task_id, "scenario": "reviewer_final",
        "recovered": [item.rule_id for item in result] == ["SEC-EVAL"],
        "state_consistent": not calls,
        "trace_complete": "checkpoint_restored" in kinds,
        "duplicate_llm_calls": len(calls),
    }


def _judge_decision_case(index: int) -> dict:
    store = _MemoryCheckpointStore()
    task_id = "judge-decision-%02d" % index
    finding = _benchmark_finding()
    key = finding_key(finding)
    store.save_checkpoint(task_id, "judge", {
        "candidate_keys": [key],
        "decisions": {
            key: {
                "finding_key": key, "approved": True,
                "reasons": [], "confidence": 0.9,
            },
        },
        "judge_context": {"restored": True},
        "usage": {},
    })
    judge_calls = []

    class Reviewer:
        name = "security-specialist"
        domains = ("security",)

        def review(self, _diff, _parsed):
            return [finding]

    class MustNotRunJudge:
        name = "benchmark-judge"

        def judge(self, _diff, _parsed, _findings, _evidence):
            judge_calls.append("judge")
            raise AssertionError("judge checkpoint was ignored")

    coordinator = MultiAgentCoordinator(
        [Reviewer()], store=store, judge=MustNotRunJudge()
    )
    result = coordinator.review_with_context(
        task_id, _RECOVERY_DIFF, parse_unified_diff(_RECOVERY_DIFF), "org/repo"
    )
    kinds = [item.get("kind") for item in store.messages.get(task_id, [])]
    return {
        "id": task_id, "scenario": "judge_decision",
        "recovered": [item.rule_id for item in result] == ["SEC-EVAL"],
        "state_consistent": not judge_calls,
        "trace_complete": "checkpoint_restored" in kinds,
        "duplicate_llm_calls": len(judge_calls),
    }


def run_fine_grained_recovery_benchmark() -> Dict[str, Any]:
    """Exercise the three persisted boundaries added inside the review execution."""
    results = []
    for factory in (
        _agent_loop_observation_case, _reviewer_final_case, _judge_decision_case,
    ):
        results.extend(factory(index) for index in range(1, 11))
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cases": len(results),
        "recovery_rate": _ratio(sum(item["recovered"] for item in results), len(results)),
        "state_consistency_rate": _ratio(
            sum(item["state_consistent"] for item in results), len(results)
        ),
        "trace_completeness_rate": _ratio(
            sum(item["trace_complete"] for item in results), len(results)
        ),
        "duplicate_llm_calls": sum(item["duplicate_llm_calls"] for item in results),
        "case_results": results,
    }


def _large_diff(index: int, line_count: int, position: int) -> tuple:
    marker = "eval(untrusted_payload_marker_%02d)" % index
    change_count = max(1, line_count // 5)
    risk_index = (0, change_count // 2, change_count - 1)[position]
    added = []
    for line_index in range(change_count):
        for context_index in range(4):
            added.append(
                " stable_%05d_%d = existing_behavior(payload_%05d)\n"
                % (line_index, context_index, line_index)
            )
        if line_index == risk_index:
            added.append("+result = %s\n" % marker)
        else:
            added.append(
                "+normalized_%05d = normalize(payload_%05d)  # ordinary change\n"
                % (line_index, line_index)
            )
    diff = (
        "--- a/src/stress_%02d.py\n+++ b/src/stress_%02d.py\n"
        "@@ -1,%d +1,%d @@\n-result = safe_parse(payload)\n%s"
        % (
            index, index, change_count * 4 + 1,
            change_count * 5, "".join(added),
        )
    )
    return diff, marker


def run_context_stress_benchmark() -> Dict[str, Any]:
    """Verify conditional compaction, Hunk batching and changed-line coverage."""
    manager = ContextManager(max_tokens=1024, reserved_tokens=256)
    unbounded = ContextManager(max_tokens=50000, reserved_tokens=2000)
    system_prompt = (
        "You are a bounded security reviewer. Return structured changed-line "
        "findings and use only the listed read-only tools."
    )
    skills = [{
        "name": "review-auth-security", "version": 1,
        "body": "Inspect trust boundaries and require exact changed-line evidence.",
    }]
    tools = [{
        "name": "read_code_context", "description": "Read source context.",
        "parameters": {
            "type": "object", "properties": {
                "path": {"type": "string"}, "line": {"type": "integer"},
            }, "required": ["path", "line"],
        },
    }, {
        "name": "read_file_history", "description": "Read recent file history.",
        "parameters": {
            "type": "object", "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }]
    observations = [{
        "id": "O1", "step": 1, "tool": "read_code_context", "ok": True,
        "result": {"path": "src/stress.py", "content": "bounded context"},
    }]
    memories = [{
        "scope": "semantic", "kind": "review_feedback",
        "content": "Require exact evidence for dynamic execution findings.",
        "recall_score": 0.9,
    }]
    results = []
    tiers = (("medium", 600), ("large", 1200), ("xlarge", 2400))
    case_index = 0
    for tier, lines in tiers:
        for offset in range(10):
            case_index += 1
            diff, marker = _large_diff(case_index, lines, offset % 3)
            parsed = parse_unified_diff(diff)
            focus = next(item for item in parsed.added_lines if marker in item.content)
            assignment = {
                "agent": "security-reviewer",
                "objective": "review dangerous dynamic execution",
                "files": [focus.path],
                "risk_domains": ["security"],
                "focus_lines": [{"path": focus.path, "line": focus.line}],
            }
            fixed_tokens = manager.contract_tokens(
                system_prompt, assignment, skills, tools
            ) + manager.runtime_tokens(
                observations=observations, memories=memories
            ) + manager.estimate_tokens("DIFF_CONTEXT:\n")
            bundles = manager.build_batches(
                diff, assignment, fixed_tokens=fixed_tokens
            )
            managed_batches = [
                manager.compose(
                    bundle, assignment, system_prompt=system_prompt,
                    skills=skills, tools=tools,
                    observations=observations, memories=memories,
                )
                for bundle in bundles
            ]
            original_fixed = unbounded.contract_tokens(
                system_prompt, assignment, skills, tools
            ) + unbounded.runtime_tokens(
                observations=observations, memories=memories
            ) + unbounded.estimate_tokens("DIFF_CONTEXT:\n")
            original_bundle = unbounded.build(
                diff, assignment, fixed_tokens=original_fixed
            )
            original = unbounded.compose(
                original_bundle, assignment, system_prompt=system_prompt,
                skills=skills, tools=tools,
                observations=observations, memories=memories,
            )
            batch_tokens = [item.estimated_tokens for item in managed_batches]
            average_batch_tokens = sum(batch_tokens) / len(batch_tokens)
            reduction = 1.0 - average_batch_tokens / original.estimated_tokens
            contract_retained = all(
                managed.system_prompt == system_prompt
                and managed.manifest["included"]["skills"] == len(skills)
                and managed.manifest["included"]["tools"] == len(tools)
                for managed in managed_batches
            )
            rendered = "".join(bundle.text for bundle in bundles)
            changed_line_coverage = _ratio(
                sum(item.content in rendered for item in parsed.added_lines),
                len(parsed.added_lines),
            )
            results.append({
                "id": "context-%02d" % case_index,
                "size_tier": tier, "added_lines": lines,
                "risk_position": ("start", "middle", "end")[offset % 3],
                "original_tokens": original.estimated_tokens,
                "average_batch_tokens": round(average_batch_tokens),
                "max_batch_tokens": max(batch_tokens),
                "total_batch_tokens": sum(batch_tokens),
                "original_diff_tokens": bundles[0].original_tokens,
                "single_call_token_reduction_rate": round(reduction, 4),
                "cumulative_token_ratio": round(
                    sum(batch_tokens) / original.estimated_tokens, 4
                ),
                "compressed": any(bundle.compressed for bundle in bundles),
                "batched": len(bundles) > 1,
                "batch_count": len(bundles),
                "within_budget": all(
                    item.estimated_tokens <= manager.max_tokens
                    for item in managed_batches
                ),
                "contract_retained": contract_retained,
                "changed_line_coverage": changed_line_coverage,
                "risk_evidence_retained": marker in rendered,
                "strategies": sorted({bundle.strategy for bundle in bundles}),
                "manifests": [item.manifest for item in managed_batches],
            })
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cases": len(results),
        "context_max_tokens": 1024,
        "reserved_tokens": 256,
        "compression_activation_rate": _ratio(
            sum(item["compressed"] for item in results), len(results)
        ),
        "batch_activation_rate": _ratio(
            sum(item["batched"] for item in results), len(results)
        ),
        "budget_compliance_rate": _ratio(
            sum(item["within_budget"] for item in results), len(results)
        ),
        "risk_evidence_retention_rate": _ratio(
            sum(item["risk_evidence_retained"] for item in results), len(results)
        ),
        "contract_retention_rate": _ratio(
            sum(item["contract_retained"] for item in results), len(results)
        ),
        "changed_line_coverage_rate": round(
            sum(item["changed_line_coverage"] for item in results) / len(results),
            4,
        ),
        "average_single_call_token_reduction_rate": round(
            sum(item["single_call_token_reduction_rate"] for item in results)
            / len(results), 4
        ),
        "average_cumulative_token_ratio": round(
            sum(item["cumulative_token_ratio"] for item in results)
            / len(results), 4
        ),
        "case_results": results,
    }


def markdown_report(faults: dict, context: dict, fine_grained: dict) -> str:
    percent = lambda value: "%.1f%%" % (100 * value)
    return "\n".join([
        "# CapyReview 工程能力基准报告",
        "",
        "## Runtime 故障注入",
        "",
        "- 样本：%d 条（可恢复 %d 条，预期终止并隔离 %d 条）" % (
            faults["cases"], faults["recoverable_cases"],
            faults["expected_terminal_cases"],
        ),
        "- 可恢复故障恢复率：%s" % percent(faults["fault_recovery_rate"]),
        "- 预期终止故障隔离率：%s" % percent(faults["fault_containment_rate"]),
        "- 状态一致率：%s" % percent(faults["state_consistency_rate"]),
        "- Trace 完整率：%s" % percent(faults["trace_completeness_rate"]),
        "- 重复副作用：%d 次" % faults["duplicate_side_effects"],
        "",
        "故障类型覆盖瞬时节点失败、工具参数错误、Checkpoint 断点恢复、"
        "重复投递以及执行预算耗尽。恢复率仅以可恢复的前四类 40 条为分母；"
        "预算耗尽的 10 条按是否正确停止并保存已完成状态计算隔离率。",
        "",
        "## 细粒度 Agent 恢复",
        "",
        "- 样本：%d 条（Agent Loop Observation、Reviewer Final、Judge Decision 各 10 条）"
        % fine_grained["cases"],
        "- 恢复成功率：%s" % percent(fine_grained["recovery_rate"]),
        "- 状态一致率：%s" % percent(fine_grained["state_consistency_rate"]),
        "- Trace 完整率：%s" % percent(fine_grained["trace_completeness_rate"]),
        "- 重复 LLM 调用：%d 次" % fine_grained["duplicate_llm_calls"],
        "",
        "该组测试验证工具 Observation 后续跑、Reviewer 最终结果复用和 Judge 决策复用；"
        "它不宣称生成中 Token 级恢复。",
        "",
        "## 大 Diff 上下文压力",
        "",
        "- 样本：%d 条（medium/large/xlarge 各 10 条）" % context["cases"],
        "- 压缩触发率：%s" % percent(context["compression_activation_rate"]),
        "- Token 预算满足率：%s" % percent(context["budget_compliance_rate"]),
        "- 规则层完整保留率：%s" % percent(context["contract_retention_rate"]),
        "- 风险证据保留率：%s" % percent(context["risk_evidence_retention_rate"]),
        "- 平均单次输入 Token 缩减：%s" % percent(
            context["average_single_call_token_reduction_rate"]
        ),
        "- 变更行覆盖率：%s" % percent(context["changed_line_coverage_rate"]),
        "- Batch 触发率：%s" % percent(context["batch_activation_rate"]),
        "- 所有 Batch 累计 Token / 原完整请求：%s" % percent(
            context["average_cumulative_token_ratio"]
        ),
        "",
        "所有结果均由 `scripts/run_engineering_benchmarks.py` 本地可复现生成。",
        "",
    ])
