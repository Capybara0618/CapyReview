"""OpenAI Agents SDK adapter for CapyReview's bounded reviewer loop."""
import atexit
import asyncio
import json
import threading
import time
from typing import Any, Callable, Dict, Optional

from agents import Agent, FunctionTool, MaxTurnsExceeded, ModelSettings, RunConfig, Runner
from agents.models.interface import Model
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from .runtime import AgentLoopResult, RuntimeBudgetExceeded, ToolRegistry


_SDK_EVENT_LOOPS = set()
_SDK_EVENT_LOOPS_LOCK = threading.Lock()


def _remember_sdk_event_loop() -> None:
    """Retain SDK run_sync loops until a clean process shutdown.

    Agents SDK intentionally leaves the thread's default loop open so SDK
    sessions can reuse loop-bound primitives. CapyReview owns persistence and
    does not use SDK sessions, but retaining and closing those loops prevents a
    later asyncio runner from orphaning them and emitting ResourceWarning.
    """
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        return
    with _SDK_EVENT_LOOPS_LOCK:
        _SDK_EVENT_LOOPS.add(loop)


def _close_sdk_event_loops() -> None:
    with _SDK_EVENT_LOOPS_LOCK:
        loops = tuple(_SDK_EVENT_LOOPS)
        _SDK_EVENT_LOOPS.clear()
    for loop in loops:
        if not loop.is_running() and not loop.is_closed():
            loop.close()


atexit.register(_close_sdk_event_loops)


class OpenAIAgentsSDKLoop:
    """Run native SDK tool calls while keeping application-owned checkpoints."""

    def __init__(
        self, model: Model, timeout_seconds: int = 60,
        max_observation_chars: int = 4000,
    ):
        self.model = model
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.max_observation_chars = max(256, int(max_observation_chars))

    @classmethod
    def openai_compatible(
        cls, base_url: str, api_key: str, model: str,
        timeout_seconds: int = 60, extra_headers: Optional[Dict[str, str]] = None,
    ) -> "OpenAIAgentsSDKLoop":
        client = AsyncOpenAI(
            base_url=base_url.rstrip("/"), api_key=api_key,
            timeout=timeout_seconds, max_retries=0,
            default_headers=extra_headers or None,
        )
        return cls(
            OpenAIChatCompletionsModel(model=model, openai_client=client),
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def _usage(value: Any) -> Dict[str, int]:
        return {
            "llm_calls": max(0, int(getattr(value, "requests", 0))),
            "prompt_tokens": max(0, int(getattr(value, "input_tokens", 0))),
            "completion_tokens": max(0, int(getattr(value, "output_tokens", 0))),
            "total_tokens": max(0, int(getattr(value, "total_tokens", 0))),
        }

    @staticmethod
    def _merge_usage(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, int]:
        return {
            key: max(0, int(left.get(key, 0))) + max(0, int(right.get(key, 0)))
            for key in {
                "llm_calls", "prompt_tokens", "completion_tokens",
                "total_tokens", "latency_ms",
            }
        }

    def run(
        self, *, agent_name: str, instructions: str, input_text: str,
        tools: ToolRegistry, output_parser: Callable[[Any], Any], max_turns: int,
        event_sink: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        resume_state: Optional[Dict[str, Any]] = None,
        checkpoint_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
        tool_enabled: Optional[Callable[[str], bool]] = None,
    ) -> AgentLoopResult:
        resume = dict(resume_state or {})
        observations = [dict(item) for item in resume.get("observations", [])]
        next_step = int(resume.get("next_step", 1))
        remaining_turns = int(max_turns) - next_step + 1
        if remaining_turns < 1:
            raise RuntimeBudgetExceeded("agent loop step budget exceeded")
        prior_usage = dict(resume.get("usage") or {})
        started = time.monotonic()

        def emit(kind: str, detail: Dict[str, Any]) -> None:
            if event_sink:
                event_sink(kind, dict(detail))

        sdk_tools = []
        for entry in tools.catalog():
            name = entry["name"]

            async def invoke(tool_context, raw_arguments: str, tool_name=name):
                step = next_step + len(observations) - len(
                    resume.get("observations", [])
                )
                emit("agent_loop_action", {"step": step, "action": "tool"})
                try:
                    arguments = json.loads(raw_arguments or "{}")
                    value = await asyncio.to_thread(tools.invoke, tool_name, arguments)
                    rendered = (
                        json.dumps(value, ensure_ascii=False, sort_keys=True)
                        if isinstance(value, (dict, list, tuple)) else str(value)
                    )
                    observation = {
                        "id": "O%d" % (len(observations) + 1),
                        "step": step, "tool": tool_name, "ok": True,
                        "result": rendered[:self.max_observation_chars],
                    }
                except Exception as exc:
                    observation = {
                        "id": "O%d" % (len(observations) + 1),
                        "step": step, "tool": tool_name, "ok": False,
                        "error": str(exc)[:1000],
                    }
                observations.append(observation)
                emit("agent_loop_observation", observation)
                if checkpoint_sink:
                    checkpoint_sink({
                        "next_step": step + 1,
                        "observations": [dict(item) for item in observations],
                        "usage": self._merge_usage(
                            prior_usage, self._usage(tool_context.usage),
                        ),
                    })
                return json.dumps(observation, ensure_ascii=False, sort_keys=True)

            sdk_tools.append(FunctionTool(
                name=name, description=entry["description"],
                params_json_schema=entry["parameters"], on_invoke_tool=invoke,
                strict_json_schema=False,
                is_enabled=(
                    (lambda _context, _agent, tool_name=name: tool_enabled(tool_name))
                    if tool_enabled else True
                ),
                timeout_seconds=self.timeout_seconds,
            ))

        agent = Agent(
            name=agent_name, instructions=instructions, model=self.model,
            tools=sdk_tools,
            model_settings=ModelSettings(
                temperature=0, max_tokens=2048, parallel_tool_calls=False,
                timeout=self.timeout_seconds,
            ),
        )
        try:
            try:
                result = Runner.run_sync(
                    agent, input_text, max_turns=remaining_turns,
                    run_config=RunConfig(
                        tracing_disabled=True,
                        workflow_name="CapyReview reviewer loop",
                    ),
                )
            finally:
                _remember_sdk_event_loop()
        except MaxTurnsExceeded as exc:
            emit("agent_loop_budget_exhausted", {
                "step": max_turns, "budget": "steps",
            })
            raise RuntimeBudgetExceeded("agent loop step budget exceeded") from exc

        current_usage = self._usage(result.context_wrapper.usage)
        current_usage["latency_ms"] = max(
            0, int((time.monotonic() - started) * 1000),
        )
        usage = self._merge_usage(prior_usage, current_usage)
        steps = min(max_turns, next_step - 1 + max(1, current_usage["llm_calls"]))
        emit("agent_loop_action", {"step": steps, "action": "final"})
        return AgentLoopResult(
            output_parser(result.final_output), steps,
            observations, "final", usage,
        )
