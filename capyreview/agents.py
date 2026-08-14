"""Risk-routed review with domain agents, evidence validation and a final judge.

The coordinator selects the smallest justified reviewer set, runs selected domain
reviewers under bounded tool loops, validates changed-line evidence, and delegates
semantic approval to one independent judge. Runtime events and hand-offs are
persisted when a task store is available.
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, TypedDict

from .context_manager import ContextManager
from .diff_parser import ParsedDiff
from .memory import MemoryManager
from .mcp import ReviewToolContext
from .models import Finding, Severity
from .reviewer import Reviewer
from .review_skills import ReviewSkillSelector
from .runtime import AgentLoop, AgentRuntime, AgentTool, RuntimeNode, ToolRegistry


@dataclass
class AgentMessage:
    sender: str
    recipient: str
    kind: str
    content: Dict[str, Any]
    correlation_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class CollaborationBus:
    """Task-scoped mailbox plus durable transcript."""

    def __init__(self, task_id: str = "", store=None):
        self.task_id = task_id
        self.store = store
        self.messages: List[AgentMessage] = []
        self._lock = threading.Lock()

    def send(
        self, sender: str, recipient: str, kind: str,
        content: Dict[str, Any], correlation_id: str = "",
    ) -> AgentMessage:
        message = AgentMessage(sender, recipient, kind, content, correlation_id)
        with self._lock:
            self.messages.append(message)
            if self.store is not None and self.task_id:
                self.store.record_agent_message(self.task_id, message.to_dict())
        return message

    def inbox(self, recipient: str, correlation_id: str = "") -> List[dict]:
        with self._lock:
            values = [
                message.to_dict() for message in self.messages
                if message.recipient in {recipient, "specialists", "all"}
                and (not correlation_id or message.correlation_id == correlation_id)
            ]
        return values

    def count(self, kind: str = "") -> int:
        with self._lock:
            return sum(1 for item in self.messages if not kind or item.kind == kind)


@dataclass
class ReviewAssignment:
    agent: str
    objective: str
    files: List[str]
    risk_domains: List[str]
    focus_lines: List[dict] = field(default_factory=list)
    assignment_id: str = ""
    round: int = 1
    reason: str = "initial-plan"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReviewPlan:
    languages: List[str]
    changed_files: List[str]
    risk_level: str
    assignments: List[ReviewAssignment]
    route: str = "specialized"

    def to_dict(self) -> dict:
        return {
            "languages": self.languages,
            "changed_files": self.changed_files,
            "risk_level": self.risk_level,
            "route": self.route,
            "assignments": [item.to_dict() for item in self.assignments],
        }


@dataclass
class EvidenceReport:
    finding_key: str
    grounded: bool
    method: str
    evidence: str
    supporting_evidence: List[dict] = field(default_factory=list)

    @property
    def reproducible(self) -> bool:
        """Compatibility name for persisted reports created by older versions."""
        return self.grounded

@dataclass
class VerificationDecision:
    finding_key: str
    approved: bool
    reasons: List[str]
    confidence: float


class CollaborationState(TypedDict, total=False):
    diff: str
    parsed: ParsedDiff
    task_id: str
    repository: str
    pull_request: Optional[int]
    head_commit: str
    bus: CollaborationBus
    plan: ReviewPlan
    specialist_findings: List[Finding]
    finding_sources: Dict[str, List[str]]
    assignments_by_agent: Dict[str, ReviewAssignment]
    reproductions: Dict[str, EvidenceReport]
    judge_context: Dict[str, Any]
    decisions: Dict[str, VerificationDecision]
    judge_usage: Dict[str, int]
    verified: List[Finding]
    agent_outcomes: List[dict]
    checkpoints: Dict[str, Dict[str, Any]]
    activated_skills_by_agent: Dict[str, List[str]]


def finding_key(finding: Finding) -> str:
    return "%s:%s:%s" % (finding.path, finding.line, finding.rule_id)


class AssignmentRouter:
    name = "assignment-router"

    DOMAIN_OBJECTIVES = {
        "security": "Trace attacker-controlled data and find exploitable security defects.",
        "reliability": "Find failure handling, observability and runtime reliability regressions.",
        "correctness": "Find behavior and data-flow defects introduced by the change.",
        "regression": "Identify compatibility and test gaps caused by the change.",
    }

    def plan(self, parsed: ParsedDiff, specialists: List[Reviewer]) -> ReviewPlan:
        extensions = {path.rsplit(".", 1)[-1].lower() for path in parsed.files if "." in path}
        languages = sorted({
            "python" if ext == "py" else
            "javascript" if ext in {"js", "jsx", "ts", "tsx"} else
            "configuration" if ext in {"yml", "yaml", "json", "toml"} else ext
            for ext in extensions
        })
        sensitive = any(
            token in path.lower()
            for path in parsed.files
            for token in ("auth", "security", "payment", "permission", "token", "migration")
        )
        default_domains = ["security", "reliability", "correctness", "regression"]
        assignments = []
        for index, agent in enumerate(specialists, 1):
            declared = list(getattr(agent, "domains", ()) or default_domains)
            objectives = [
                self.DOMAIN_OBJECTIVES[item]
                for item in declared if item in self.DOMAIN_OBJECTIVES
            ]
            assignments.append(ReviewAssignment(
                agent=agent.name,
                objective=" ".join(objectives) or "Find actionable defects and cite changed-line evidence.",
                files=list(parsed.files),
                risk_domains=declared,
                assignment_id="A%02d" % index,
            ))
        return ReviewPlan(
            languages=languages or ["unknown"],
            changed_files=list(parsed.files),
            risk_level="high" if sensitive or len(parsed.files) > 10 else "normal",
            assignments=assignments,
        )

    def replan(
        self, failed: ReviewAssignment, substitutes: List[Reviewer], error: str,
    ) -> Optional[ReviewAssignment]:
        if not substitutes:
            return None
        target = max(
            substitutes,
            key=lambda item: len(
                set(getattr(item, "domains", ()) or failed.risk_domains)
                .intersection(failed.risk_domains)
            ),
        )
        return ReviewAssignment(
            agent=target.name,
            objective=(
                failed.objective
                + " Take over a failed assignment and independently reconstruct its evidence."
            ),
            files=list(failed.files), risk_domains=list(failed.risk_domains),
            focus_lines=[dict(item) for item in failed.focus_lines],
            assignment_id=failed.assignment_id, round=failed.round + 1,
            reason="replacement-after-failure: %s" % error[:160],
        )


class RiskRouter(AssignmentRouter):
    """Select the smallest reviewer set justified by the changed code."""

    name = "risk-router"
    RISK_TOKENS = (
        "eval(", "exec(", "shell=true", "subprocess", "pickle.loads",
        "yaml.load(", "password", "secret", "api_key", "authorization",
        "permission", "token", "execute(", "query(", "md5(", "sha1(",
        "signature", "hmac", "bypass",
    )
    SENSITIVE_PATH_TOKENS = (
        "auth", "security", "payment", "permission", "token", "migration",
        "credential", "secret",
    )

    def route(self, parsed: ParsedDiff, reviewers: List[Reviewer]) -> ReviewPlan:
        if not reviewers:
            return ReviewPlan(["unknown"], list(parsed.files), "normal", [], "routine")
        risky_files = {
            line.path for line in parsed.added_lines
            if any(token in line.content.lower() for token in self.RISK_TOKENS)
        }
        focus_lines = [
            {"path": line.path, "line": line.line}
            for line in parsed.added_lines
            if any(token in line.content.lower() for token in self.RISK_TOKENS)
        ]
        sensitive_files = {
            path for path in parsed.files
            if any(token in path.lower() for token in self.SENSITIVE_PATH_TOKENS)
        }
        specialized = bool(risky_files or sensitive_files or len(parsed.files) > 10)
        if specialized:
            selected = list(reviewers)
        else:
            selected = [
                reviewer for reviewer in reviewers
                if not (
                    set(getattr(reviewer, "domains", ()) or ())
                    and set(getattr(reviewer, "domains", ()) or ()) <= {
                        "security", "authorization",
                    }
                )
            ] or [reviewers[0]]

        base = super().plan(parsed, selected)
        assignments = []
        for assignment in base.assignments:
            domains = set(assignment.risk_domains)
            scoped = list(assignment.files)
            if specialized and domains and domains <= {"security", "authorization"}:
                relevant = risky_files | sensitive_files
                scoped = [path for path in parsed.files if path in relevant] or scoped
            assignments.append(ReviewAssignment(
                agent=assignment.agent,
                objective=assignment.objective,
                files=scoped,
                risk_domains=assignment.risk_domains,
                focus_lines=[
                    dict(item) for item in focus_lines
                    if item["path"] in scoped
                ],
                assignment_id=assignment.assignment_id,
                round=assignment.round,
                reason="risk-routed" if specialized else "routine-route",
            ))
        return ReviewPlan(
            base.languages, base.changed_files,
            "high" if specialized else "normal", assignments,
            "specialized" if specialized else "routine",
        )

    def plan(self, parsed: ParsedDiff, specialists: List[Reviewer]) -> ReviewPlan:
        return self.route(parsed, specialists)


class EvidenceValidator:
    name = "evidence-validator"

    SUPPORTING_TOOLS = {
        "read_code_context", "search_repository", "read_file_history",
        "read_code_scanning_findings", "read_ci_failure",
    }

    def validate(
        self, finding: Finding, parsed: ParsedDiff, observations=(),
    ) -> EvidenceReport:
        line = next(
            (item.content for item in parsed.added_lines
             if item.path == finding.path and item.line == finding.line), ""
        )
        normalized_line = "".join(line.split())
        normalized_evidence = "".join(str(finding.evidence or "").split())
        grounded = bool(
            normalized_line
            and normalized_evidence
            and (
                normalized_evidence in normalized_line
                or normalized_line in normalized_evidence
            )
        )
        return EvidenceReport(
            finding_key(finding), grounded,
            "path-line-quote match",
            line.strip()[:240] if grounded else "No matching quote on the reported changed line.",
            self._supporting_evidence(finding, observations),
        )

    @classmethod
    def _supporting_evidence(cls, finding: Finding, observations) -> List[dict]:
        by_id = {
            str(item.get("id")): item for item in observations
            if isinstance(item, dict) and item.get("ok") is True
            and item.get("tool") in cls.SUPPORTING_TOOLS
        }
        supporting = []
        for reference in dict.fromkeys(finding.evidence_refs):
            observation = by_id.get(str(reference))
            if observation is None:
                continue
            raw = observation.get("result")
            try:
                result = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                result = raw
            if isinstance(result, dict):
                content = result.get("content")
                if content is None:
                    content = json.dumps(
                        result, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    )
                item = {
                    "source": observation["tool"],
                    "path": str(result.get("path") or finding.path),
                    "line": finding.line,
                    "content": str(content)[:1600],
                }
                try:
                    item["line"] = int(
                        result.get("start_line") or result.get("line")
                        or finding.line
                    )
                except (TypeError, ValueError):
                    pass
            else:
                item = {
                    "source": observation["tool"], "path": finding.path,
                    "line": finding.line, "content": str(result)[:1600],
                }
            supporting.append(item)
        return supporting[:6]


class MultiAgentCoordinator(Reviewer):
    """Bounded risk-routed review with failure recovery and independent judging."""

    name = "risk-routed-multi-agent-review"

    def __init__(
        self, agents: List[Reviewer], max_workers: int = 4, store=None,
        agent_retries: int = 1,
        context_manager: Optional[ContextManager] = None,
        memory_manager: Optional[MemoryManager] = None,
        agent_loop_max_steps: int = 4, agent_loop_timeout_seconds: int = 240,
        runtime_timeout_seconds: int = 360,
        judge=None, tool_provider=None, skill_registry=None, skill_selector=None,
    ):
        if not agents:
            raise ValueError("at least one LLM reviewer is required")
        if judge is None or not callable(getattr(judge, "judge", None)):
            raise ValueError("an explicit independent LLM judge is required")
        self.agents = list(agents)
        self.max_workers = max_workers
        self.store = store
        self.agent_retries = max(0, agent_retries)
        self.context_manager = context_manager or ContextManager()
        self.memory_manager = memory_manager
        self.agent_loop = AgentLoop(agent_loop_max_steps, agent_loop_timeout_seconds)
        self.runtime = AgentRuntime(
            max_steps=8, timeout_seconds=runtime_timeout_seconds
        )
        self.router = RiskRouter()
        self.planner = self.router
        self.evidence_validator = EvidenceValidator()
        self.evidence_agent = self.evidence_validator
        self.test_agent = self.evidence_validator
        self.judge = judge
        self.tool_provider = tool_provider
        self.skill_registry = skill_registry
        self.skill_selector = skill_selector or ReviewSkillSelector()
        self._summaries: Dict[str, dict] = {}
        self._last_summary: Dict[str, Any] = {}
        self._summary_lock = threading.Lock()
        self._checkpoint_lock = threading.Lock()

    def review(self, diff: str, parsed: ParsedDiff) -> List[Finding]:
        return self.review_with_context("", diff, parsed)

    def review_with_context(
        self, task_id: str, diff: str, parsed: ParsedDiff,
        repository: str = "", head_commit: str = "",
        pull_request: Optional[int] = None,
    ) -> List[Finding]:
        checkpoints = {}
        if self.store is not None and task_id:
            checkpoints = self.store.load_checkpoints(task_id)
        state: CollaborationState = {
            "task_id": task_id, "diff": diff, "parsed": parsed,
            "repository": repository, "pull_request": pull_request,
            "head_commit": head_commit,
            "bus": CollaborationBus(task_id, self.store),
            "checkpoints": checkpoints,
        }
        result = self.runtime.execute(
            state,
            [
                RuntimeNode("router", self._plan_node, checkpoint=False),
                RuntimeNode("reviewers", self._specialist_node, checkpoint=False),
                RuntimeNode("evidence", self._evidence_node, checkpoint=False),
                RuntimeNode("judge", self._judge_node, checkpoint=False),
                RuntimeNode("finalize", self._arbitrate_node, checkpoint=False),
            ],
            task_id=task_id,
        )
        summary = self._make_summary(result)
        with self._summary_lock:
            self._last_summary = summary
            if task_id:
                self._summaries[task_id] = summary
        return result["verified"]

    def collaboration_summary(self, task_id: str) -> dict:
        with self._summary_lock:
            return dict(self._summaries.get(task_id, self._last_summary if not task_id else {}))

    @staticmethod
    def _bus(state: CollaborationState) -> CollaborationBus:
        return state["bus"]

    def _emit(
        self, state: CollaborationState, sender: str, recipient: str,
        kind: str, content: Dict[str, Any], correlation_id: str = "",
    ) -> None:
        self._bus(state).send(sender, recipient, kind, content, correlation_id)

    def _completed_checkpoint(
        self, state: CollaborationState, node: str,
    ) -> Optional[Dict[str, Any]]:
        checkpoint = (state.get("checkpoints") or {}).get(node)
        if not checkpoint or checkpoint.get("status") != "completed":
            return None
        value = checkpoint.get("state")
        return dict(value) if isinstance(value, dict) else None

    def _save_completed_checkpoint(
        self, state: CollaborationState, node: str,
        value: Dict[str, Any], attempt: int = 1,
    ) -> None:
        task_id = state.get("task_id", "")
        if self.store is None or not task_id:
            return
        self.store.save_checkpoint(
            task_id, node, value, status="completed", attempt=attempt,
        )
        with self._checkpoint_lock:
            state.setdefault("checkpoints", {})[node] = {
                "status": "completed", "attempt": attempt,
                "state": dict(value), "error": None,
            }
        self._emit(
            state, "agent-runtime", "coordinator", "checkpoint_saved",
            {"node": node, "status": "completed"}, node,
        )

    def _delete_checkpoint(
        self, state: CollaborationState, node: str,
    ) -> bool:
        task_id = state.get("task_id", "")
        if self.store is None or not task_id:
            return False
        deleted = self.store.delete_checkpoint(task_id, node)
        with self._checkpoint_lock:
            state.setdefault("checkpoints", {}).pop(node, None)
        if deleted:
            self._emit(
                state, "agent-runtime", "coordinator", "checkpoint_cleared",
                {"node": node}, node,
            )
        return deleted

    @staticmethod
    def _finding_from_dict(value: Dict[str, Any]) -> Finding:
        data = dict(value)
        data["severity"] = Severity(data["severity"])
        return Finding(**data)

    @staticmethod
    def _decision_from_dict(value: Dict[str, Any]) -> VerificationDecision:
        return VerificationDecision(
            finding_key=str(value["finding_key"]),
            approved=bool(value.get("approved", False)),
            reasons=[str(item)[:500] for item in value.get("reasons", [])],
            confidence=max(0.0, min(1.0, float(value.get("confidence", 0.0)))),
        )

    def _plan_node(self, state: CollaborationState) -> Dict[str, Any]:
        plan = self.router.route(state["parsed"], self.agents)
        for assignment in plan.assignments:
            self._emit(
                state, self.router.name, assignment.agent, "assignment",
                assignment.to_dict(), assignment.assignment_id,
            )
        return {
            "plan": plan,
            "assignments_by_agent": {item.agent: item for item in plan.assignments},
        }

    def _recall_memories(
        self, state: CollaborationState, assignment: ReviewAssignment,
    ) -> List[dict]:
        if not self.memory_manager or not state.get("repository"):
            return []
        query = " ".join([
            assignment.objective, " ".join(assignment.files),
            " ".join(assignment.risk_domains),
        ])
        memories = self.memory_manager.recall(
            state.get("repository", ""), query
        )
        if memories:
            self._emit(
                state, "memory-manager", assignment.agent, "memory_recalled",
                {
                    "count": len(memories),
                    "memory_ids": [item["id"] for item in memories],
                    "scopes": sorted({item["scope"] for item in memories}),
                }, assignment.assignment_id,
            )
        return memories

    def _agent_tools(
        self, state: CollaborationState, assignment: ReviewAssignment, skills=(),
    ) -> ToolRegistry:
        if (
            self.tool_provider is None or not state.get("repository")
            or not state.get("head_commit")
        ):
            tools = ToolRegistry()
        else:
            tools = self.tool_provider.registry(ReviewToolContext(
                repository=state["repository"],
                head_commit=state["head_commit"],
                pull_request=state.get("pull_request"),
                files=tuple(assignment.files),
                domains=tuple(assignment.risk_domains),
            ))
        activated = {item.name: item for item in skills}
        if activated:
            def read_skill_reference(skill: str, path: str):
                if skill not in activated:
                    raise ValueError("only an activated review skill may be read")
                if str(path) not in activated[skill].references:
                    raise ValueError("reference is not declared by the activated review skill")
                return {
                    "skill": skill, "path": str(path),
                    "content": self.skill_registry.read_reference(skill, str(path)),
                }

            tools.register(AgentTool(
                "read_skill_reference",
                "Read one declared reference from an activated review skill.",
                {
                    "type": "object",
                    "properties": {
                        "skill": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["skill", "path"],
                    "additionalProperties": False,
                },
                read_skill_reference,
            ))
        return tools

    def _activate_review_skills(
        self, state: CollaborationState, assignment: ReviewAssignment,
    ) -> list:
        if self.skill_registry is None:
            return []
        selected = self.skill_selector.select(
            self.skill_registry.discover(), assignment.risk_domains,
            assignment.files, state["diff"],
        )
        activated = [self.skill_registry.activate(item.name) for item in selected]
        for skill in activated:
            self._emit(
                state, "skill-selector", assignment.agent, "skill_activated",
                {
                    "name": skill.name, "version": skill.version,
                    "references": list(skill.references),
                }, assignment.assignment_id,
            )
        return activated

    def _run_agent_loop(
        self, state: CollaborationState, agent: Reviewer,
        assignment: ReviewAssignment, feedback: Optional[List[str]],
    ) -> tuple:
        memories = self._recall_memories(state, assignment)
        activated_skills = self._activate_review_skills(state, assignment)
        with self._checkpoint_lock:
            state.setdefault("activated_skills_by_agent", {})[agent.name] = [
                item.name for item in activated_skills
            ]
        tools = self._agent_tools(state, assignment, activated_skills)
        skill_context = [
            {"name": item.name, "version": item.version, "body": item.body}
            for item in activated_skills
        ]
        initial_tools = tools.catalog()
        system_factory = getattr(agent, "agent_system_prompt", None)

        def system_prompt(tool_catalog: List[dict]) -> str:
            if callable(system_factory):
                return str(system_factory(tool_catalog))
            return str(getattr(agent, "system_prompt", "") or (
                "You are a bounded PR review specialist. Follow the assignment, "
                "use only the listed tools, and return exact changed-line evidence."
            ))

        initial_system = system_prompt(initial_tools)
        fixed_tokens = self.context_manager.contract_tokens(
            initial_system, assignment.to_dict(), skill_context, initial_tools
        ) + self.context_manager.estimate_tokens("DIFF_CONTEXT:\n")
        bundle = self.context_manager.build(
            state["diff"], assignment.to_dict(), memories,
            fixed_tokens=fixed_tokens,
        )
        self._emit(
            state, "context-manager", agent.name, "context_prepared",
            bundle.metadata(), assignment.assignment_id,
        )

        def on_event(kind: str, detail: Dict[str, Any]) -> None:
            self._emit(
                state, "agent-runtime", agent.name, kind, detail,
                assignment.assignment_id,
            )
            if (
                kind == "agent_loop_observation"
                and detail.get("ok") is False
                and self.store is not None
                and state.get("task_id")
            ):
                self.store.record_failure_case(
                    state["task_id"], "tool_error",
                    {
                        "reviewer": agent.name,
                        "tool": str(detail.get("tool", ""))[:120],
                        "error": str(detail.get("error", ""))[:1000],
                        "evolution_eligible": False,
                    },
                )

        loop_state = {
            "diff": state["diff"], "context": bundle.text,
            "context_metadata": bundle.metadata(), "parsed": state["parsed"],
            "assignment": assignment.to_dict(), "feedback": list(feedback or []),
            "inbox": self._bus(state).inbox(agent.name, assignment.assignment_id),
            "memories": memories, "available_tools": tools.catalog(),
            "activated_skills": skill_context,
        }
        last_context = {"metadata": bundle.metadata()}
        checkpoint_node = "loop:%s" % assignment.assignment_id
        checkpoint = (state.get("checkpoints") or {}).get(checkpoint_node) or {}
        resume_state = None
        if checkpoint.get("status") == "running" and isinstance(
            checkpoint.get("state"), dict
        ):
            resume_state = dict(checkpoint["state"])
            self._emit(
                state, "agent-runtime", agent.name,
                "agent_loop_checkpoint_restored",
                {
                    "node": checkpoint_node,
                    "next_step": int(resume_state.get("next_step", 1)),
                    "observations": len(resume_state.get("observations") or []),
                },
                assignment.assignment_id,
            )

        def save_loop_checkpoint(value: Dict[str, Any]) -> None:
            task_id = state.get("task_id", "")
            if self.store is None or not task_id:
                return
            saved = dict(value)
            saved["agent"] = agent.name
            attempt = max(1, int(saved.get("next_step", 1)) - 1)
            self.store.save_checkpoint(
                task_id, checkpoint_node, saved,
                status="running", attempt=attempt,
            )
            with self._checkpoint_lock:
                state.setdefault("checkpoints", {})[checkpoint_node] = {
                    "status": "running", "attempt": attempt,
                    "state": saved, "error": None,
                }
            self._emit(
                state, "agent-runtime", agent.name,
                "agent_loop_checkpoint_saved",
                {
                    "node": checkpoint_node,
                    "next_step": saved["next_step"],
                    "observations": len(saved["observations"]),
                },
                assignment.assignment_id,
            )

        def managed_step(loop_iteration: Dict[str, Any]) -> Dict[str, Any]:
            final_only = int(loop_iteration.get("loop_step", 1)) >= self.agent_loop.max_steps
            active_tools = [] if final_only else tools.catalog()
            active_system = system_prompt(active_tools)
            managed = self.context_manager.compose(
                bundle, assignment.to_dict(), feedback=list(feedback or []),
                inbox=loop_iteration.get("inbox") or [], memories=memories,
                observations=loop_iteration.get("observations") or [],
                tools=active_tools, skills=skill_context,
                system_prompt=active_system,
            )
            metadata = managed.metadata()
            last_context["metadata"] = metadata
            self._emit(
                state, "context-manager", agent.name, "context_window_prepared",
                metadata, assignment.assignment_id,
            )
            prepared = dict(loop_iteration)
            prepared["context"] = managed.text
            prepared["managed_context"] = managed.text
            prepared["context_metadata"] = metadata
            prepared["available_tools"] = active_tools
            prepared["activated_skills"] = skill_context
            prepared["managed_system_prompt"] = managed.system_prompt
            return getattr(agent, "agent_step")(prepared)

        result = self.agent_loop.run(
            managed_step, tools, loop_state, on_event,
            resume_state=resume_state,
            checkpoint_sink=save_loop_checkpoint,
        )
        findings = list(result.output or [])
        if not all(isinstance(item, Finding) for item in findings):
            raise TypeError("agent loop final output must contain Finding objects")
        return findings, {
            "loop_steps": result.steps, "loop_stop_reason": result.stop_reason,
            "context": last_context["metadata"], "memories_recalled": len(memories),
            "tools_available": len(tools.names()),
            "tool_calls": len(result.observations),
            "activated_skills": [
                "%s@%s" % (item.name, item.version) for item in activated_skills
            ],
            "observations": [dict(item) for item in result.observations],
            "usage": dict(result.usage),
        }

    def _invoke_agent(
        self, state: CollaborationState, agent: Reviewer,
        assignment: ReviewAssignment, feedback: Optional[List[str]] = None,
    ) -> tuple:
        last_error = None
        for attempt in range(1, self.agent_retries + 2):
            self._emit(
                state, "coordinator", agent.name, "attempt_started",
                {"attempt": attempt, "round": assignment.round}, assignment.assignment_id,
            )
            try:
                loop_stepper = getattr(agent, "agent_step", None)
                execution = {"loop_steps": 0, "loop_stop_reason": "one-shot"}
                if loop_stepper:
                    findings, execution = self._run_agent_loop(
                        state, agent, assignment, feedback
                    )
                else:
                    collaborative = getattr(agent, "review_assignment", None)
                    if collaborative:
                        findings = collaborative(
                            state["diff"], state["parsed"], assignment.to_dict(),
                            list(feedback or []),
                            self._bus(state).inbox(agent.name, assignment.assignment_id),
                        )
                    else:
                        findings = agent.review(state["diff"], state["parsed"])
                    consume_usage = getattr(agent, "consume_usage", None)
                    if consume_usage:
                        execution["usage"] = consume_usage()
                self._emit(
                    state, agent.name, self.judge.name, "reviewer_candidates",
                    {
                        "attempt": attempt, "round": assignment.round,
                        "findings": [item.to_dict() for item in findings],
                        "execution": {
                            key: value for key, value in execution.items()
                            if key != "observations"
                        },
                    }, assignment.assignment_id,
                )
                return findings, attempt, "", execution
            except Exception as exc:
                last_error = str(exc)
                self._emit(
                    state, agent.name, self.router.name, "agent_failure",
                    {"attempt": attempt, "error": last_error[:1000]},
                    assignment.assignment_id,
                )
                if attempt <= self.agent_retries:
                    self._emit(
                        state, self.router.name, agent.name, "retry_request",
                        {"next_attempt": attempt + 1, "reason": last_error[:500]},
                        assignment.assignment_id,
                    )
        return (
            [], self.agent_retries + 1, last_error or "unknown agent failure",
            {"loop_steps": 0, "loop_stop_reason": "failed"},
        )

    def _replacement_candidates(self, failed_agent: Reviewer) -> List[Reviewer]:
        return [item for item in self.agents if item is not failed_agent]

    def _run_assignment(
        self, state: CollaborationState, assignment: ReviewAssignment,
        agent: Reviewer,
    ) -> dict:
        checkpoint_node = "reviewer:%s" % assignment.assignment_id
        checkpoint = self._completed_checkpoint(state, checkpoint_node)
        known_agents = {item.name for item in self.agents}
        if checkpoint and checkpoint.get("agent") in known_agents:
            restored_findings = [
                self._finding_from_dict(item)
                for item in checkpoint.get("findings", [])
            ]
            restored_agent = str(checkpoint["agent"])
            restored_assignment = assignment
            if restored_agent != assignment.agent:
                restored_assignment = ReviewAssignment(
                    agent=restored_agent,
                    objective=assignment.objective,
                    files=list(assignment.files),
                    risk_domains=list(assignment.risk_domains),
                    focus_lines=[dict(item) for item in assignment.focus_lines],
                    assignment_id=assignment.assignment_id,
                    round=max(assignment.round, 2),
                    reason="restored-handoff",
                )
            self._emit(
                state, "agent-runtime", "coordinator", "checkpoint_restored",
                {"node": checkpoint_node, "agent": restored_agent},
                assignment.assignment_id,
            )
            return {
                "agent": restored_agent,
                "assignment_id": assignment.assignment_id,
                "attempts": int(checkpoint.get("attempts", 1)),
                "status": "completed", "findings": restored_findings,
                "error": "",
                "substituted_for": str(checkpoint.get("substituted_for", "")),
                "assignment": restored_assignment,
                "execution": dict(checkpoint.get("execution") or {}),
                "restored": True,
            }
        findings, attempts, error, execution = self._invoke_agent(
            state, agent, assignment
        )
        result = {
            "agent": agent.name, "assignment_id": assignment.assignment_id,
            "attempts": attempts, "status": "completed" if not error else "failed",
            "findings": findings, "error": error, "substituted_for": "",
            "assignment": assignment, "execution": execution,
        }
        if not error:
            self._save_completed_checkpoint(
                state, checkpoint_node,
                {
                    "agent": result["agent"],
                    "findings": [item.to_dict() for item in findings],
                    "attempts": attempts,
                    "execution": execution,
                    "substituted_for": "",
                },
                attempt=attempts,
            )
            self._delete_checkpoint(state, "loop:%s" % assignment.assignment_id)
            return result
        replacement = self.router.replan(
            assignment, self._replacement_candidates(agent), error
        )
        if replacement is None:
            return result
        substitute = next(
            item for item in self._replacement_candidates(agent)
            if item.name == replacement.agent
        )
        self._emit(
            state, self.router.name, substitute.name, "assignment_handoff",
            {
                "from": agent.name, "reason": error[:500],
                "assignment": replacement.to_dict(),
            }, assignment.assignment_id,
        )
        findings, replacement_attempts, replacement_error, replacement_execution = self._invoke_agent(
            state, substitute, replacement, ["Take over after %s failed: %s" % (agent.name, error)]
        )
        result = {
            "agent": substitute.name, "assignment_id": assignment.assignment_id,
            "attempts": attempts + replacement_attempts,
            "status": "completed" if not replacement_error else "failed",
            "findings": findings, "error": replacement_error,
            "substituted_for": agent.name,
            "assignment": replacement, "execution": replacement_execution,
        }
        if not replacement_error:
            self._save_completed_checkpoint(
                state, checkpoint_node,
                {
                    "agent": result["agent"],
                    "findings": [item.to_dict() for item in findings],
                    "attempts": result["attempts"],
                    "execution": replacement_execution,
                    "substituted_for": agent.name,
                },
                attempt=result["attempts"],
            )
            self._delete_checkpoint(state, "loop:%s" % assignment.assignment_id)
        return result

    def _specialist_node(self, state: CollaborationState) -> Dict[str, Any]:
        outcomes = []
        by_name = {item.name: item for item in self.agents}
        assignments = state["plan"].assignments
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, max(1, len(assignments)))
        ) as pool:
            futures = {
                pool.submit(self._run_assignment, state, assignment, by_name[assignment.agent]): assignment
                for assignment in assignments
            }
            for future in as_completed(futures):
                outcomes.append(future.result())
        findings = []
        sources: Dict[str, List[str]] = {}
        finding_observations: Dict[str, List[dict]] = {}
        assignment_map = dict(state["assignments_by_agent"])
        for outcome in outcomes:
            assignment_map[outcome["agent"]] = outcome["assignment"]
            for finding in outcome["findings"]:
                key = finding_key(finding)
                sources.setdefault(key, []).append(outcome["agent"])
                finding_observations.setdefault(key, [
                    dict(item) for item in (
                        outcome.get("execution") or {}
                    ).get("observations", [])
                ])
                findings.append(finding)
        if outcomes and all(item["status"] == "failed" for item in outcomes):
            raise RuntimeError(
                "all review assignments failed after retry/replanning: "
                + "; ".join(item["error"] for item in outcomes)
            )
        return {
            "specialist_findings": findings,
            "finding_sources": sources,
            "finding_observations": finding_observations,
            "agent_outcomes": outcomes,
            "assignments_by_agent": assignment_map,
        }

    def _reflect_rejected(
        self, state: CollaborationState, rejected: List[tuple], stage: str,
    ) -> tuple[List[Finding], Dict[str, List[str]]]:
        """Give each originating reviewer one bounded correction round."""
        grouped: Dict[str, List[tuple]] = {}
        for finding, reason in rejected:
            sources = state.get("finding_sources", {}).get(finding_key(finding), [])
            if sources:
                grouped.setdefault(sources[0], []).append((finding, reason))
        corrections: List[Finding] = []
        sources: Dict[str, List[str]] = {}
        agents = {item.name: item for item in self.agents}
        assignments = state.get("assignments_by_agent", {})
        for agent_name, failures in grouped.items():
            agent = agents.get(agent_name)
            assignment = assignments.get(agent_name)
            if agent is None or assignment is None:
                continue
            checkpoint_node = "reflection:%s:%s" % (
                stage, assignment.assignment_id,
            )
            rejected_keys = sorted(finding_key(finding) for finding, _ in failures)
            checkpoint = self._completed_checkpoint(state, checkpoint_node)
            if (
                checkpoint
                and checkpoint.get("agent") == agent_name
                and sorted(checkpoint.get("rejected_keys") or []) == rejected_keys
            ):
                restored = [
                    self._finding_from_dict(item)
                    for item in checkpoint.get("findings", [])
                ]
                corrections.extend(restored)
                for finding in restored:
                    key = finding_key(finding)
                    sources.setdefault(key, []).append(agent_name)
                execution = dict(checkpoint.get("execution") or {})
                if execution:
                    state.setdefault("reflection_executions", []).append(execution)
                    for finding in restored:
                        state.setdefault("finding_observations", {})[
                            finding_key(finding)
                        ] = [
                            dict(item)
                            for item in execution.get("observations", [])
                        ]
                state["reflection_rounds"] = int(
                    state.get("reflection_rounds", 0)
                ) + 1
                self._emit(
                    state, "agent-runtime", "coordinator", "checkpoint_restored",
                    {"node": checkpoint_node, "agent": agent_name},
                    assignment.assignment_id,
                )
                continue
            feedback = [json.dumps({
                "stage": stage,
                "finding": finding.to_dict(),
                "reason": str(reason)[:500],
                "instruction": "Correct this candidate once; do not repeat it unchanged.",
            }, ensure_ascii=False) for finding, reason in failures]
            self._emit(
                state, stage, agent_name, "reflection_requested",
                {"stage": stage, "candidates": len(failures), "reasons": [
                    str(reason)[:500] for _finding, reason in failures
                ]}, assignment.assignment_id,
            )
            corrected, attempts, error, execution = self._invoke_agent(
                state, agent, assignment, feedback,
            )
            state.setdefault("reflection_executions", []).append(execution)
            state["reflection_rounds"] = int(state.get("reflection_rounds", 0)) + 1
            if error:
                corrected = []
            selected = []
            used = set()
            for rejected_finding, _reason in failures:
                exact_key = finding_key(rejected_finding)
                match = next((
                    finding for finding in corrected
                    if finding_key(finding) == exact_key
                    and finding_key(finding) not in used
                ), None)
                if match is None:
                    match = next((
                        finding for finding in corrected
                        if finding.path == rejected_finding.path
                        and finding.line == rejected_finding.line
                        and finding_key(finding) not in used
                    ), None)
                if match is not None:
                    selected.append(match)
                    used.add(finding_key(match))
            corrected = selected
            corrections.extend(corrected)
            for finding in corrected:
                key = finding_key(finding)
                sources.setdefault(key, []).append(agent_name)
                state.setdefault("finding_observations", {})[key] = [
                    dict(item) for item in execution.get("observations", [])
                ]
            self._emit(
                state, agent_name, stage, "reflection_completed",
                {
                    "stage": stage, "attempts": attempts,
                    "findings": [item.to_dict() for item in corrected],
                    "error": error[:500],
                }, assignment.assignment_id,
            )
            self._save_completed_checkpoint(
                state, checkpoint_node,
                {
                    "agent": agent_name,
                    "findings": [item.to_dict() for item in corrected],
                    "rejected_keys": rejected_keys,
                    "feedback": feedback,
                    "error": error,
                    "execution": execution,
                }, attempt=max(1, attempts),
            )
        return corrections, sources

    def _skill_for_reviewer(
        self, state: CollaborationState, reviewer: str,
    ) -> str:
        selected = list(
            (state.get("activated_skills_by_agent") or {}).get(reviewer, [])
        )
        if not selected:
            for outcome in state.get("agent_outcomes", []):
                if outcome.get("agent") != reviewer:
                    continue
                selected = [
                    str(item).rsplit("@", 1)[0]
                    for item in (outcome.get("execution") or {}).get(
                        "activated_skills", []
                    )
                ]
                break
        return selected[0] if selected else ""

    def _archive_unresolved_failures(
        self, state: CollaborationState, failures: List[tuple], stage: str,
        resolved_findings: List[Finding],
    ) -> None:
        if self.store is None or not state.get("task_id") or not failures:
            return
        resolved_locations = {
            (finding.path, finding.line) for finding in resolved_findings
        }
        existing = {
            (
                item.get("category"),
                finding_key(self._finding_from_dict(item["payload"]["finding"])),
            )
            for item in self.store.list_task_failure_cases(state["task_id"])
            if isinstance((item.get("payload") or {}).get("finding"), dict)
        }
        for finding, reason in failures:
            if (finding.path, finding.line) in resolved_locations:
                continue
            sources = state.get("finding_sources", {}).get(finding_key(finding), [])
            reviewer = sources[0] if sources else ""
            skill_name = self._skill_for_reviewer(state, reviewer)
            identity = ("%s_rejected" % stage, finding_key(finding))
            if identity in existing:
                continue
            self.store.record_failure_case(
                state["task_id"], identity[0],
                {
                    "stage": stage,
                    "reviewer": reviewer,
                    "finding": finding.to_dict(),
                    "reason": str(reason)[:1000],
                    "skill_name": skill_name,
                    "evolution_eligible": bool(skill_name),
                },
            )

    def _validate_evidence(
        self, state: CollaborationState, finding: Finding,
    ) -> EvidenceReport:
        return self.evidence_validator.validate(
            finding, state["parsed"],
            state.get("finding_observations", {}).get(finding_key(finding), []),
        )

    def _evidence_node(self, state: CollaborationState) -> Dict[str, Any]:
        grounded = []
        rejected = []
        for finding in state["specialist_findings"]:
            reproduction = self._validate_evidence(state, finding)
            self._emit(
                state, self.evidence_validator.name, self.judge.name, "evidence_validation",
                asdict(reproduction), reproduction.finding_key,
            )
            if reproduction.grounded:
                grounded.append(finding)
            else:
                rejected.append((finding, reproduction.evidence))
        corrected, corrected_sources = self._reflect_rejected(
            state, rejected, "evidence",
        ) if rejected else ([], {})
        grounded_corrections = []
        sources = {
            finding_key(finding): list(
                state.get("finding_sources", {}).get(finding_key(finding), [])
            )
            for finding in grounded
        }
        reproductions = {}
        for finding in grounded:
            reproduction = self._validate_evidence(state, finding)
            reproductions[reproduction.finding_key] = reproduction
        for finding in corrected:
            reproduction = self._validate_evidence(state, finding)
            self._emit(
                state, self.evidence_validator.name, self.judge.name,
                "evidence_revalidation", asdict(reproduction),
                reproduction.finding_key,
            )
            if reproduction.grounded:
                grounded_corrections.append(finding)
                reproductions[reproduction.finding_key] = reproduction
                sources[reproduction.finding_key] = corrected_sources.get(
                    reproduction.finding_key, []
                )
        self._archive_unresolved_failures(
            state, rejected, "evidence", grounded_corrections,
        )
        candidates = grounded + grounded_corrections
        return {
            "specialist_findings": candidates,
            "finding_sources": sources,
            "reproductions": reproductions,
            "initial_reviewer_candidates": len(state["specialist_findings"]),
            "evidence_rejected_count": max(
                0, len(rejected) - len(grounded_corrections)
            ),
        }

    def _judge_node(self, state: CollaborationState) -> Dict[str, Any]:
        candidates = state["specialist_findings"]
        if not candidates:
            self._emit(
                state, "coordinator", self.judge.name, "judge_skipped",
                {"reason": "no grounded candidate"}, "judge",
            )
            return {"decisions": {}, "judge_context": {}, "judge_usage": {}}
        candidate_keys = sorted(finding_key(item) for item in candidates)
        checkpoint = self._completed_checkpoint(state, "judge")
        if checkpoint and checkpoint.get("candidate_keys") == candidate_keys:
            stored = checkpoint.get("decisions") or {}
            if sorted(stored) == candidate_keys:
                restored_payload = checkpoint.get("findings") or []
                restored_candidates = [
                    self._finding_from_dict(item) for item in restored_payload
                ]
                if (
                    restored_candidates
                    and sorted(finding_key(item) for item in restored_candidates)
                    == candidate_keys
                ):
                    candidates = restored_candidates
                restored_evidence = {
                    finding_key(finding): self._validate_evidence(state, finding)
                    for finding in candidates
                }
                decisions = {}
                for finding in candidates:
                    key = finding_key(finding)
                    decision = self._decision_from_dict(stored[key])
                    evidence = restored_evidence[key]
                    decision.approved = decision.approved and evidence.grounded
                    if not evidence.grounded and not any(
                        "evidence" in reason.lower() for reason in decision.reasons
                    ):
                        decision.reasons.append(
                            "evidence is not grounded on the reported changed line"
                        )
                    decisions[key] = decision
                    self._emit(
                        state, self.judge.name, "review-report", "judge_decision",
                        asdict(decision), key,
                    )
                self._emit(
                    state, "agent-runtime", "coordinator", "checkpoint_restored",
                    {"node": "judge", "candidates": len(candidate_keys)}, "judge",
                )
                result = {
                    "decisions": decisions,
                    "judge_context": dict(checkpoint.get("judge_context") or {}),
                    "judge_usage": dict(checkpoint.get("usage") or {}),
                }
                if restored_candidates:
                    result.update({
                        "specialist_findings": candidates,
                        "finding_sources": dict(
                            checkpoint.get("finding_sources") or {}
                        ),
                        "reproductions": restored_evidence,
                    })
                return result
        plan = state.get("plan")
        risk_domains = sorted({
            domain
            for assignment in (plan.assignments if plan else [])
            for domain in assignment.risk_domains
        })
        evidence_clues = " ".join(
            "%s %s %s %s" % (
                finding.path,
                finding.rule_id,
                finding.title,
                finding.evidence,
            )
            for finding in candidates
            if state["reproductions"][finding_key(finding)].grounded
        )[:4000]
        judge_bundle = self.context_manager.build(
            state["diff"],
            {
                "agent": self.judge.name,
                "objective": "Validate grounded candidate evidence. " + evidence_clues,
                "files": sorted({finding.path for finding in candidates}),
                "risk_domains": risk_domains,
                "focus_lines": [
                    {"path": finding.path, "line": finding.line}
                    for finding in candidates
                ],
            },
        )
        self._emit(
            state,
            "context-manager",
            self.judge.name,
            "context_prepared",
            judge_bundle.metadata(),
        )
        def judge_once(
            batch: List[Finding], evidence: Dict[str, EvidenceReport],
        ) -> tuple[Dict[str, VerificationDecision], Dict[str, int]]:
            raw_decisions = self.judge.judge(
                judge_bundle.text, state["parsed"], batch, evidence,
            )
            consume_judge_usage = getattr(self.judge, "consume_usage", None)
            usage = consume_judge_usage() if consume_judge_usage else {}
            batch_decisions: Dict[str, VerificationDecision] = {}
            for finding in batch:
                key = finding_key(finding)
                raw = dict(raw_decisions.get(key) or {})
                report = evidence[key]
                approved = bool(raw.get("approved", False)) and report.grounded
                reasons = [str(item)[:500] for item in raw.get("reasons", [])]
                if not report.grounded and not any(
                    "evidence" in reason.lower() for reason in reasons
                ):
                    reasons.append(
                        "evidence is not grounded on the reported changed line"
                    )
                decision = VerificationDecision(
                    key, approved, reasons,
                    max(0.0, min(
                        1.0, float(raw.get("confidence", finding.confidence))
                    )),
                )
                batch_decisions[key] = decision
                self._emit(
                    state, self.judge.name, "review-report", "judge_decision",
                    asdict(decision), key,
                )
            return batch_decisions, usage

        decisions, judge_usage = judge_once(candidates, state["reproductions"])
        rejected = [
            (finding, "; ".join(decisions[finding_key(finding)].reasons))
            for finding in candidates
            if not decisions[finding_key(finding)].approved
            and state["reproductions"][finding_key(finding)].grounded
        ]
        corrected, corrected_sources = self._reflect_rejected(
            state, rejected, "judge",
        ) if rejected else ([], {})
        resolved_reflections = []
        if corrected:
            approved = [
                finding for finding in candidates
                if decisions[finding_key(finding)].approved
            ]
            corrected_evidence: Dict[str, EvidenceReport] = {}
            grounded_corrections = []
            for finding in corrected:
                report = self._validate_evidence(state, finding)
                self._emit(
                    state, self.evidence_validator.name, self.judge.name,
                    "evidence_revalidation", asdict(report), report.finding_key,
                )
                if report.grounded:
                    grounded_corrections.append(finding)
                    corrected_evidence[report.finding_key] = report
            if grounded_corrections:
                corrected_decisions, corrected_usage = judge_once(
                    grounded_corrections, corrected_evidence,
                )
                decisions = {
                    finding_key(finding): decisions[finding_key(finding)]
                    for finding in approved
                }
                decisions.update(corrected_decisions)
                resolved_reflections = [
                    finding for finding in grounded_corrections
                    if corrected_decisions[finding_key(finding)].approved
                ]
                for key, value in corrected_usage.items():
                    judge_usage[key] = int(judge_usage.get(key, 0)) + int(value)
            else:
                decisions = {
                    finding_key(finding): decisions[finding_key(finding)]
                    for finding in approved
                }
            candidates = approved + grounded_corrections
            sources = {
                finding_key(finding): list(
                    state.get("finding_sources", {}).get(finding_key(finding), [])
                )
                for finding in approved
            }
            sources.update({
                key: value for key, value in corrected_sources.items()
                if key in corrected_evidence
            })
            reproductions = {
                finding_key(finding): state["reproductions"][finding_key(finding)]
                for finding in approved
            }
            reproductions.update(corrected_evidence)
            candidate_keys = sorted(finding_key(item) for item in candidates)
        self._archive_unresolved_failures(
            state, rejected, "judge", resolved_reflections,
        )
        result = {
            "decisions": decisions,
            "judge_context": judge_bundle.metadata(),
            "judge_usage": judge_usage,
        }
        if corrected:
            result.update({
                "specialist_findings": candidates,
                "finding_sources": sources,
                "reproductions": reproductions,
            })
        self._save_completed_checkpoint(
            state, "judge",
            {
                "candidate_keys": candidate_keys,
                "decisions": {
                    key: asdict(decision) for key, decision in decisions.items()
                },
                "findings": [item.to_dict() for item in candidates],
                "finding_sources": {
                    finding_key(item): list(
                        (result.get("finding_sources") or state.get(
                            "finding_sources", {}
                        )).get(finding_key(item), [])
                    )
                    for item in candidates
                },
                "judge_context": result["judge_context"],
                "usage": judge_usage,
            },
        )
        return result

    def _arbitrate_node(self, state: CollaborationState) -> Dict[str, Any]:
        merged: Dict[tuple, Finding] = {}
        severity_rank = {
            Severity.CRITICAL: 4, Severity.HIGH: 3,
            Severity.MEDIUM: 2, Severity.LOW: 1,
        }
        for finding in state["specialist_findings"]:
            decision = state["decisions"][finding_key(finding)]
            if not decision.approved:
                continue
            finding.confidence = decision.confidence
            # A changed line contributes one primary review conclusion. Specialists
            # may describe the same root cause with different CWE identifiers or
            # expand it into secondary consequences; those are alternatives, not
            # independent final findings.
            identity = (finding.path, finding.line)
            current = merged.get(identity)
            finding_rank = (
                severity_rank[finding.severity], finding.confidence, finding.rule_id
            )
            current_rank = (
                severity_rank[current.severity], current.confidence, current.rule_id
            ) if current is not None else (-1, -1.0, "")
            if current is None or finding_rank > current_rank:
                merged[identity] = finding
        severity_order = {
            Severity.CRITICAL: 0, Severity.HIGH: 1,
            Severity.MEDIUM: 2, Severity.LOW: 3,
        }
        verified = sorted(
            merged.values(),
            key=lambda item: (severity_order[item.severity], item.path, item.line),
        )
        rejected = [
            {"finding_key": key, "reasons": decision.reasons}
            for key, decision in state["decisions"].items() if not decision.approved
        ]
        self._emit(
            state, self.judge.name, "review-report", "final_decision",
            {
                "approved_findings": [item.to_dict() for item in verified],
                "rejected_findings": rejected,
            },
        )
        if self.memory_manager and state.get("repository"):
            approved_keys = {finding_key(item) for item in verified}
            for finding in state["specialist_findings"]:
                key = finding_key(finding)
                decision = state["decisions"][key]
                self.memory_manager.remember_finding(
                    state["repository"], state.get("task_id", ""), finding.to_dict(),
                    key in approved_keys, decision.reasons,
                )
            if state.get("task_id"):
                outcomes = state.get("agent_outcomes", [])
                executions = [
                    item.get("execution") or {} for item in outcomes
                ] + list(state.get("reflection_executions", []))
                memory_summary = {
                    "proposed_findings": len(state.get("specialist_findings", [])),
                    "approved_findings": len(verified),
                    "rejected_findings": len(rejected),
                    "dialogue_rounds": int(state.get("reflection_rounds", 0)),
                    "agent_loop_steps": sum(
                        int(execution.get("loop_steps", 0))
                        for execution in executions
                    ),
                    "tool_calls": sum(
                        int(execution.get("tool_calls", 0))
                        for execution in executions
                    ),
                }
                archived = self.memory_manager.consolidate_task(
                    state["repository"], state["task_id"], memory_summary,
                )
                self._emit(
                    state, "memory-manager", "agent-runtime", "memory_consolidated",
                    {
                        "task_id": state["task_id"],
                        "summary_memory_id": (archived or {}).get("id", ""),
                        "task_summary_archived": bool(archived),
                    },
                )
        return {"verified": verified}

    def _make_summary(self, state: CollaborationState) -> dict:
        outcomes = state.get("agent_outcomes", [])
        reflection_executions = state.get("reflection_executions", [])
        executions = [
            item.get("execution") or {} for item in outcomes
        ] + list(reflection_executions)
        decisions = state.get("decisions", {})
        candidates = state.get("specialist_findings", [])
        reproductions = state.get("reproductions", {})
        evidence_rejected = int(state.get("evidence_rejected_count", 0))
        judge_rejected = sum(
            1 for finding in candidates
            if reproductions.get(finding_key(finding))
            and reproductions[finding_key(finding)].grounded
            and not decisions.get(
                finding_key(finding),
                VerificationDecision(finding_key(finding), False, [], 0.0),
            ).approved
        )
        approved_before_dedup = sum(
            1 for finding in candidates
            if decisions.get(
                finding_key(finding),
                VerificationDecision(finding_key(finding), False, [], 0.0),
            ).approved
        )
        final_findings = len(state.get("verified", []))
        funnel = {
            "reviewer_candidates": int(
                state.get("initial_reviewer_candidates", len(candidates))
            ),
            "evidence_rejected": evidence_rejected,
            "judge_rejected": judge_rejected,
            "approved_before_dedup": approved_before_dedup,
            "duplicates_merged": max(0, approved_before_dedup - final_findings),
            "final_findings": final_findings,
        }
        judge_context = dict(state.get("judge_context") or {})
        usage_keys = (
            "llm_calls", "prompt_tokens", "completion_tokens",
            "total_tokens", "latency_ms",
        )
        usage = {
            key: sum(
                int((execution.get("usage") or {}).get(key, 0))
                for execution in executions
            ) + int((state.get("judge_usage") or {}).get(key, 0))
            for key in usage_keys
        }
        return {
            "protocol": "route-review-evidence-judge",
            "roles": [
                self.router.name, "reviewers", self.evidence_validator.name,
                self.judge.name,
            ],
            "route": state.get("plan").route if state.get("plan") else "unknown",
            "planned_assignments": len(state.get("plan").assignments) if state.get("plan") else 0,
            "dialogue_rounds": 0,
            "reflection_rounds": int(state.get("reflection_rounds", 0)),
            "messages": self._bus(state).count(),
            "retries": self._bus(state).count("retry_request"),
            "handoffs": self._bus(state).count("assignment_handoff"),
            "agents": [
                {
                    "agent": item["agent"], "status": item["status"],
                    "attempts": item["attempts"],
                    "substituted_for": item.get("substituted_for", ""),
                    "loop_steps": (item.get("execution") or {}).get("loop_steps", 0),
                    "loop_stop_reason": (
                        item.get("execution") or {}
                    ).get("loop_stop_reason", "one-shot"),
                    "context_compressed": bool(
                        ((item.get("execution") or {}).get("context") or {}).get("compressed")
                    ),
                    "memories_recalled": (
                        item.get("execution") or {}
                    ).get("memories_recalled", 0),
                }
                for item in outcomes
            ],
            "agent_loop_steps": sum(
                int(execution.get("loop_steps", 0))
                for execution in executions
            ),
            "tool_calls": sum(
                int(execution.get("tool_calls", 0))
                for execution in executions
            ),
            "activated_skills": sorted({
                skill
                for execution in executions
                for skill in (execution.get("activated_skills") or [])
            }),
            "context_compressions": sum(
                bool((execution.get("context") or {}).get("compressed"))
                for execution in executions
            ) + int(bool(judge_context.get("compressed"))),
            "judge_context": judge_context,
            "usage": usage,
            "recovery": {
                "checkpoints_saved": (
                    self._bus(state).count("checkpoint_saved")
                    + self._bus(state).count("agent_loop_checkpoint_saved")
                ),
                "checkpoints_restored": (
                    self._bus(state).count("checkpoint_restored")
                    + self._bus(state).count("agent_loop_checkpoint_restored")
                ),
                "checkpoints_cleared": self._bus(state).count("checkpoint_cleared"),
            },
            "memories_recalled": sum(
                int(execution.get("memories_recalled", 0))
                for execution in executions
            ),
            "proposed_findings": len(state.get("specialist_findings", [])),
            "approved_findings": sum(1 for item in decisions.values() if item.approved),
            "rejected_findings": sum(1 for item in decisions.values() if not item.approved),
            "review_funnel": funnel,
        }
