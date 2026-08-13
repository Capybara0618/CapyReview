"""Application service for CapyReview's core PR-review workflow."""
import uuid
from typing import Any, Dict, Optional

from .agents import MultiAgentCoordinator
from .config import Settings
from .context_manager import ContextManager
from .evolution import EvolutionEngine
from .github import GitHubClient
from .harness import ReviewHarness
from .memory import MemoryManager
from .models import TaskState, TraceEvent
from .postgres_store import create_store
from .report import to_markdown
from .reviewer import OpenAICompatibleJudge, OpenAICompatibleReviewer
from .skill_evolution import ReviewPolicy
from .store import utc_now
from .task_queue import PermanentTaskError, TaskQueue


SECURITY_REVIEW_PROMPT = """You are the security specialist in a bounded PR review
team. Report only concrete, exploitable security defects introduced by added lines.
Trace the relevant trust boundary, cite the exact changed line, and avoid reliability,
style, or speculative findings."""

CORRECTNESS_REVIEW_PROMPT = """You are the correctness and reliability specialist
in a bounded PR review team. Report only concrete runtime, error-handling, data
integrity, and regression defects introduced by added lines. Cite exact changed-line
evidence and leave security-dominant findings to the security specialist."""


class ReviewService:
    """Coordinate storage, runtime, GitHub delivery, feedback, and policy evolution.

    A reviewer can be injected for deterministic tests. Production construction is
    intentionally lazy: the web server can expose health and configuration guidance
    without a key, while any real review fails clearly until DeepSeek is configured.
    """

    def __init__(
        self, settings: Settings, reviewer=None, store=None, queue=None,
    ):
        self.settings = settings
        settings.validate_evolution()
        if store is None or queue is None:
            settings.validate_infrastructure()
        self.store = store or create_store(settings.database_url)
        self.context_manager = ContextManager(
            settings.context_max_tokens, settings.context_reserved_tokens
        )
        self.memory = MemoryManager(
            self.store,
            settings.memory_enabled,
            settings.memory_recall_limit,
            settings.memory_working_ttl_seconds,
        )
        self.github = GitHubClient(settings.github_token)
        self._injected_reviewer = reviewer
        self._policy_version: Optional[int] = None
        self._harness_cache: Dict[tuple, ReviewHarness] = {}
        self.reviewer = reviewer
        self.harness = self._new_harness(reviewer) if reviewer is not None else None
        self.evolution = EvolutionEngine(
            self.store,
            reviewer_factory=(
                self._build_policy_reviewer if settings.deepseek_api_key.strip() else None
            ),
            min_cases=settings.eval_min_cases,
            max_cases=settings.eval_max_cases,
            min_improvement=settings.eval_min_improvement,
            min_holdout_cases=settings.eval_min_holdout_cases,
            max_metric_regression=settings.eval_max_metric_regression,
        )
        self.queue = queue or TaskQueue(
            self._process_queued,
            redis_url=settings.redis_url,
            max_attempts=settings.queue_max_attempts,
            lease_seconds=settings.queue_lease_seconds,
            on_terminal_failure=self._on_terminal_failure,
        )

    def _new_harness(self, reviewer) -> ReviewHarness:
        return ReviewHarness(
            self.store,
            reviewer,
            self.settings.max_steps,
            self.settings.timeout_seconds,
        )

    def _llm_config(self) -> Dict[str, object]:
        return self.settings.resolved_llm()

    def _build_llm_reviewer(
        self, prompt: str, name: str, domains: tuple,
        model: str = "",
    ) -> OpenAICompatibleReviewer:
        config = self._llm_config()
        reviewer = OpenAICompatibleReviewer(
            str(config["base_url"]),
            str(config["api_key"]),
            model or str(config["model"]),
            self.settings.timeout_seconds,
            system_prompt=prompt,
            provider="deepseek",
        )
        reviewer.name = name
        reviewer.domains = domains
        return reviewer

    def _build_policy_reviewer(self, prompt: str) -> OpenAICompatibleReviewer:
        return self._build_llm_reviewer(
            prompt,
            "llm-review-policy-evaluator",
            ("security", "correctness", "reliability", "regression"),
        )

    def _build_llm_judge(self, model: str = "") -> OpenAICompatibleJudge:
        config = self._llm_config()
        return OpenAICompatibleJudge(
            str(config["base_url"]),
            str(config["api_key"]),
            model or str(config["model"]),
            self.settings.timeout_seconds,
            provider="deepseek",
        )

    @staticmethod
    def _policy_from_record(record: dict) -> ReviewPolicy:
        return ReviewPolicy({
            "name": "evolved-review",
            "description": "Replay-gated instructions learned from review feedback",
            "instructions": [{
                "rule_id": "POLICY-REVIEW",
                "severity": "medium",
                "domains": ["security", "correctness", "reliability", "regression"],
                "instruction": str(record["prompt"]),
            }],
        }, int(record["version"]))

    def _active_policy(self) -> tuple:
        active = self.store.get_active_skill_version("llm-review")
        if not active:
            return None, None
        return int(active["version"]), self._policy_from_record(active)

    def _policy_for_version(self, version: Optional[int]) -> Optional[ReviewPolicy]:
        if version is None:
            return None
        record = next((
            item for item in self.store.list_skill_versions("llm-review")
            if int(item["version"]) == int(version)
        ), None)
        if record is None:
            raise ValueError("frozen review policy version is unavailable: %s" % version)
        return self._policy_from_record(record)

    def _build_coordinator(
        self, policy: Optional[ReviewPolicy] = None, model: str = "",
    ) -> MultiAgentCoordinator:
        security_prompt = (
            policy.compose_system_prompt(SECURITY_REVIEW_PROMPT, ("security",))
            if policy else SECURITY_REVIEW_PROMPT
        )
        correctness_prompt = (
            policy.compose_system_prompt(
                CORRECTNESS_REVIEW_PROMPT,
                ("correctness", "reliability", "regression"),
            )
            if policy else CORRECTNESS_REVIEW_PROMPT
        )
        reviewers = [
            self._build_llm_reviewer(
                security_prompt,
                "llm-security-specialist",
                ("security",),
                model,
            ),
            self._build_llm_reviewer(
                correctness_prompt,
                "llm-correctness-specialist",
                ("correctness", "reliability", "regression"),
                model,
            ),
        ]
        return MultiAgentCoordinator(
            reviewers,
            max_workers=self.settings.agent_max_workers,
            store=self.store,
            agent_retries=self.settings.agent_retries,
            context_manager=self.context_manager,
            memory_manager=self.memory,
            agent_loop_max_steps=self.settings.agent_loop_max_steps,
            agent_loop_timeout_seconds=self.settings.agent_loop_timeout_seconds,
            judge=self._build_llm_judge(model),
            repository_reader=self.github.read_file_context,
        )

    def _ensure_harness(self) -> ReviewHarness:
        if self._injected_reviewer is not None:
            return self.harness
        version, policy = self._active_policy()
        model = self.settings.deepseek_model
        key = (model, version)
        if key not in self._harness_cache:
            reviewer = self._build_coordinator(policy, model)
            self._harness_cache[key] = self._new_harness(reviewer)
        self.harness = self._harness_cache[key]
        self.reviewer = self.harness.reviewer
        self._policy_version = version
        return self.harness

    def _harness_for_task(self, task: Dict[str, Any]) -> ReviewHarness:
        if self._injected_reviewer is not None:
            return self.harness
        task_input = task.get("input") or {}
        if "model" not in task_input and "policy_version" not in task_input:
            return self._ensure_harness()
        model = str(task_input.get("model") or self.settings.deepseek_model)
        version = task_input.get("policy_version")
        version = int(version) if version is not None else None
        key = (model, version)
        if key not in self._harness_cache:
            reviewer = self._build_coordinator(
                self._policy_for_version(version), model
            )
            self._harness_cache[key] = self._new_harness(reviewer)
        return self._harness_cache[key]

    def _execution_snapshot(self) -> Dict[str, Any]:
        return {
            "model": self.settings.deepseek_model,
            "policy_version": self._policy_version,
        }

    def _validate_review(self, repository: str, diff: str) -> None:
        if not repository or len(repository) > 250:
            raise ValueError("repository is required and must be at most 250 characters")
        size = len(diff.encode("utf-8"))
        if size == 0:
            raise ValueError("diff is required")
        if size > self.settings.max_diff_bytes:
            raise ValueError(
                "diff exceeds maximum size of %d bytes" % self.settings.max_diff_bytes
            )

    def _create_task(
        self, repository: str, diff: str, pull_request: Optional[int], source: str,
    ) -> str:
        task_id = str(uuid.uuid4())
        encoded = diff.encode("utf-8")
        self.store.create(
            task_id,
            repository,
            pull_request,
            {
                "source": source,
                "diff_bytes": len(encoded),
                **self._execution_snapshot(),
            },
        )
        self.store.save_task_payload(task_id, diff)
        return task_id

    def _create_deferred_task(
        self, repository: str, pull_request: Optional[int], source: str,
        task_input: Dict[str, Any],
    ) -> str:
        task_id = str(uuid.uuid4())
        self.store.create(
            task_id,
            repository,
            pull_request,
            {
                "source": source, "diff_pending": True,
                **self._execution_snapshot(), **task_input,
            },
        )
        return task_id

    def create_review(
        self, repository: str, diff: str, pull_request: Optional[int] = None,
        source: str = "api",
    ) -> Dict[str, Any]:
        self._validate_review(repository, diff)
        harness = self._ensure_harness()
        task_id = self._create_task(repository, diff, pull_request, source)
        report = harness.run(task_id, repository, pull_request, diff)
        return {"task_id": task_id, "state": "SUCCESS", "report": report.to_dict()}

    def enqueue_review(
        self, repository: str, diff: str, pull_request: Optional[int] = None,
        source: str = "api", github_issue_url: str = "",
    ) -> Dict[str, Any]:
        self._validate_review(repository, diff)
        self._ensure_harness()
        task_id = self._create_task(repository, diff, pull_request, source)
        self.queue.submit(
            {
                "task_id": task_id,
                "repository": repository,
                "pull_request": pull_request,
                "github_issue_url": github_issue_url,
            },
            message_id=task_id,
        )
        return {"task_id": task_id, "state": "PENDING", "queue": self.queue.backend}

    def _process_queued(self, payload: Dict[str, Any]) -> None:
        task_id = str(payload.get("task_id", ""))
        task = self.store.get(task_id)
        if not task:
            raise PermanentTaskError("task record no longer exists")
        diff = self.store.get_task_payload(task_id)
        if diff is None and payload.get("diff_url"):
            self.github.ensure_repository_access(payload["repository"])
            diff = self.github.fetch_diff(payload["diff_url"])
            self._validate_review(payload["repository"], diff)
            encoded = diff.encode("utf-8")
            self.store.save_task_payload(task_id, diff)
            self.store.update_task_input(task_id, {
                "diff_pending": False,
                "diff_bytes": len(encoded),
            })
        if diff is None:
            raise PermanentTaskError("task payload no longer exists")

        report = self._harness_for_task(task).run(
            task_id,
            payload["repository"],
            payload.get("pull_request"),
            diff,
            str(payload.get("head_commit") or task.get("input", {}).get("head_commit") or ""),
        )
        if payload.get("github_issue_url") and self.settings.auto_post_review:
            self.github.upsert_comment(
                payload["github_issue_url"],
                to_markdown(report.to_dict()),
                "<!-- capyreview-review:%s -->" % task_id,
            )

    def _on_terminal_failure(self, payload: Dict[str, Any], error: str) -> None:
        task_id = str(payload.get("task_id", ""))
        task = self.store.get(task_id) if task_id else None
        if not task or task.get("state") in {
            TaskState.SUCCESS.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        }:
            return
        step = max(
            [int(item.get("step", 0)) for item in task.get("trace", [])] or [0]
        ) + 1
        self.store.fail(
            task_id,
            error,
            TraceEvent(
                step,
                TaskState.FAILED,
                "Task exhausted queue retries: %s" % error,
                utc_now(),
            ),
        )

    def handle_github_pull_request(
        self, payload: Dict[str, Any], delivery_id: str, payload_sha256: str,
    ) -> Dict[str, Any]:
        if not self.store.claim_webhook(delivery_id, "pull_request", payload_sha256):
            existing = self.store.get_webhook(delivery_id) or {}
            return {
                "duplicate": True,
                "task_id": existing.get("task_id"),
                "state": "PENDING" if existing.get("task_id") else "ACCEPTED",
            }

        action = payload.get("action")
        if action not in {"opened", "reopened", "synchronize"}:
            self.store.complete_webhook(delivery_id, None)
            return {
                "ignored": True,
                "reason": "unsupported pull_request action: %s" % action,
            }
        pull = payload.get("pull_request") or {}
        repository = (payload.get("repository") or {}).get("full_name", "")
        number = payload.get("number")
        diff_url = pull.get("diff_url")
        head_commit = str((pull.get("head") or {}).get("sha", "")).strip()
        if not repository or not isinstance(number, int) or not diff_url or not head_commit:
            raise ValueError("invalid GitHub pull_request payload")
        self._ensure_harness()
        task_id = self._create_deferred_task(
            repository,
            number,
            "github-webhook",
            {"diff_url": diff_url, "head_commit": head_commit},
        )
        self.queue.submit(
            {
                "task_id": task_id,
                "repository": repository,
                "pull_request": number,
                "github_issue_url": pull.get("issue_url", ""),
                "diff_url": diff_url,
                "head_commit": head_commit,
            },
            message_id=task_id,
        )
        self.store.complete_webhook(delivery_id, task_id)
        return {
            "task_id": task_id,
            "state": "PENDING",
            "queue": self.queue.backend,
            "will_post_to_github": self.settings.auto_post_review,
        }

    def record_feedback(
        self, task_id: str, category: str, finding: Optional[dict], note: str,
    ) -> dict:
        task = self.store.get(task_id)
        if not task:
            raise ValueError("task not found")
        if task.get("state") != TaskState.SUCCESS.value or not task.get("report"):
            raise ValueError("feedback requires a completed review task")
        if category not in {"false_positive", "missed_issue", "accepted"}:
            raise ValueError("unsupported feedback category")
        self.store.record_failure_case(
            task_id,
            category,
            {"finding": finding, "note": note[:2000]},
        )
        self.memory.remember_feedback(
            task["repository"], task_id, category, finding, note[:2000]
        )
        return {"recorded": True, "category": category}

    def resume_task(self, task_id: str) -> dict:
        task = self.store.get(task_id)
        if not task:
            raise ValueError("task not found")
        if task["state"] == TaskState.SUCCESS.value:
            return {"task_id": task_id, "state": "SUCCESS", "report": task["report"]}
        if self.store.get_task_payload(task_id) is None:
            raise ValueError("task payload is no longer available")
        self._ensure_harness()
        self.store.prepare_resume(task_id)
        self.queue.submit(
            {
                "task_id": task_id,
                "repository": task["repository"],
                "pull_request": task.get("pull_request"),
                "head_commit": str(task.get("input", {}).get("head_commit", "")),
            },
            message_id=task_id,
        )
        return {"task_id": task_id, "state": "PENDING", "resumed": True}

    def cancel_task(self, task_id: str) -> bool:
        return self.store.request_cancel(task_id)

    def close(self) -> None:
        self.queue.close()
