"""Application service for CapyReview's core PR-review workflow."""
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .agents import MultiAgentCoordinator
from .config import Settings
from .context_manager import ContextManager
from .evolution import EvolutionEngine
from .github import GitHubClient
from .harness import ReviewHarness
from .memory import MemoryManager
from .mcp import GitHubMcpClient, GitHubMcpToolProvider
from .models import TaskState, TraceEvent
from .postgres_store import create_store
from .report import to_markdown
from .reviewer import (
    OpenAIAgentsSDKReviewer, OpenAICompatibleJudge, OpenAICompatibleReviewer,
)
from .review_skills import ReviewSkillRegistry, ReviewSkillSelector
from .skill_evolution import ReviewSkillCandidateProposer
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
    """Coordinate storage, runtime, GitHub delivery, feedback, and Skill evolution.

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
        )
        self.github = GitHubClient(settings.github_token)
        self.mcp_tools = GitHubMcpToolProvider(
            GitHubMcpClient(settings.github_token)
        )
        self.review_skills_root = Path(__file__).resolve().parents[1] / "skills"
        self.review_skills = ReviewSkillRegistry(self.review_skills_root)
        self._injected_reviewer = reviewer
        self._skill_versions: Dict[str, int] = {}
        self._evolution_scheduled_batches: Dict[str, tuple] = {}
        self._harness_cache: Dict[tuple, ReviewHarness] = {}
        self.reviewer = reviewer
        self.harness = self._new_harness(reviewer) if reviewer is not None else None
        self.evolution = EvolutionEngine(
            self.store,
            reviewer_factory=(
                self._build_evaluation_reviewer
                if settings.deepseek_api_key.strip() else None
            ),
            candidate_proposer=(
                self._build_skill_proposer()
                if settings.deepseek_api_key.strip() else None
            ),
            min_cases=settings.eval_min_cases,
            max_cases=settings.eval_max_cases,
            min_improvement=settings.eval_min_improvement,
            min_holdout_cases=settings.eval_min_holdout_cases,
            max_metric_regression=settings.eval_max_metric_regression,
            base_skill_provider=self._builtin_skill_package,
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
            self.settings.timeout_seconds * 3,
        )

    def _llm_config(self) -> Dict[str, object]:
        return self.settings.resolved_llm()

    def _build_llm_reviewer(
        self, prompt: str, name: str, domains: tuple,
        model: str = "",
    ) -> OpenAIAgentsSDKReviewer:
        config = self._llm_config()
        reviewer = OpenAIAgentsSDKReviewer(
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

    def _build_evaluation_reviewer(self, prompt: str) -> OpenAICompatibleReviewer:
        return self._build_llm_reviewer(
            prompt,
            "llm-review-skill-evaluator",
            ("security", "correctness", "reliability", "regression"),
        )

    def _build_skill_proposer(self) -> ReviewSkillCandidateProposer:
        config = self._llm_config()
        client = OpenAICompatibleReviewer(
            str(config["base_url"]), str(config["api_key"]),
            str(config["model"]), self.settings.timeout_seconds,
            provider="deepseek",
        )
        return ReviewSkillCandidateProposer(
            client.request_json, str(config["model"]),
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
    def _package_from_record(record: dict) -> dict:
        return {
            **dict(record["package"]),
            "name": record["skill_name"],
            "version": int(record["version"]),
        }

    def _active_skill_packages(self) -> list:
        return [
            self._package_from_record(record)
            for record in self.store.list_active_skill_versions()
        ]

    def _builtin_skill_package(self, skill_name: str) -> Optional[dict]:
        try:
            return self.review_skills.export_package(skill_name)
        except ValueError:
            return None

    def _skill_packages_for_versions(self, versions: Dict[str, int]) -> list:
        packages = []
        for name, version in sorted(versions.items()):
            record = next((
                item for item in self.store.list_skill_versions(name)
                if int(item["version"]) == int(version)
            ), None)
            if record is None:
                raise ValueError(
                    "frozen review Skill version is unavailable: %s@%s"
                    % (name, version)
                )
            packages.append(self._package_from_record(record))
        return packages

    def _build_coordinator(
        self, skill_packages=(), model: str = "",
    ) -> MultiAgentCoordinator:
        reviewers = [
            self._build_llm_reviewer(
                SECURITY_REVIEW_PROMPT,
                "llm-security-specialist",
                ("security",),
                model,
            ),
            self._build_llm_reviewer(
                CORRECTNESS_REVIEW_PROMPT,
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
            agent_loop_timeout_seconds=self.settings.timeout_seconds * 2,
            runtime_timeout_seconds=self.settings.timeout_seconds * 3,
            judge=self._build_llm_judge(model),
            tool_provider=self.mcp_tools,
            skill_registry=ReviewSkillRegistry(
                self.review_skills_root, packages=skill_packages,
            ),
            skill_selector=ReviewSkillSelector(),
        )

    def _ensure_harness(self) -> ReviewHarness:
        if self._injected_reviewer is not None:
            return self.harness
        packages = self._active_skill_packages()
        versions = {item["name"]: item["version"] for item in packages}
        model = self.settings.deepseek_model
        key = (model, tuple(sorted(versions.items())))
        if key not in self._harness_cache:
            reviewer = self._build_coordinator(packages, model)
            self._harness_cache[key] = self._new_harness(reviewer)
        self.harness = self._harness_cache[key]
        self.reviewer = self.harness.reviewer
        self._skill_versions = versions
        return self.harness

    def _harness_for_task(self, task: Dict[str, Any]) -> ReviewHarness:
        if self._injected_reviewer is not None:
            return self.harness
        task_input = task.get("input") or {}
        if "model" not in task_input and "skill_versions" not in task_input:
            return self._ensure_harness()
        model = str(task_input.get("model") or self.settings.deepseek_model)
        versions = {
            str(name): int(version)
            for name, version in (task_input.get("skill_versions") or {}).items()
        }
        key = (model, tuple(sorted(versions.items())))
        if key not in self._harness_cache:
            reviewer = self._build_coordinator(
                self._skill_packages_for_versions(versions), model
            )
            self._harness_cache[key] = self._new_harness(reviewer)
        return self._harness_cache[key]

    def _execution_snapshot(self) -> Dict[str, Any]:
        return {
            "model": self.settings.deepseek_model,
            "skill_versions": dict(self._skill_versions),
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
        self._schedule_skill_evolution()
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
        if payload.get("kind") == "skill-evolution":
            self.evolution.auto_propose(
                str(payload.get("skill_name") or "")
            )
            return
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
        self._schedule_skill_evolution()

    def _schedule_skill_evolution(self) -> bool:
        cases = self.store.list_failure_cases(True, 100)
        cases_by_skill: Dict[str, list] = {}
        for case in cases:
            payload = case.get("payload") or {}
            if not payload.get("evolution_eligible"):
                continue
            skill_name = str(payload.get("skill_name", "")).strip()
            if skill_name:
                cases_by_skill.setdefault(skill_name, []).append(case)
        scheduled = False
        for skill_name, skill_cases in sorted(cases_by_skill.items()):
            count = len(skill_cases)
            batch = tuple(sorted(int(case["id"]) for case in skill_cases))
            if (
                not self.evolution.should_auto_propose(skill_name)
                or batch == self._evolution_scheduled_batches.get(skill_name)
            ):
                continue
            self._evolution_scheduled_batches[skill_name] = batch
            self.queue.submit(
                {
                    "kind": "skill-evolution",
                    "skill_name": skill_name,
                    "failure_cases": count,
                },
                message_id="skill-evolution-%s-%s" % (skill_name, count),
            )
            scheduled = True
        return scheduled

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
        if category in {"false_positive", "missed_issue"}:
            skill_name = self._feedback_skill(task, finding)
            self.store.record_failure_case(
                task_id,
                category,
                {
                    "finding": finding,
                    "note": note[:2000],
                    "skill_name": skill_name,
                    "evolution_eligible": bool(skill_name),
                },
            )
        self.memory.remember_feedback(
            task["repository"], task_id, category, finding, note[:2000]
        )
        if category != "accepted":
            self._schedule_skill_evolution()
        return {"recorded": True, "category": category}

    @staticmethod
    def _feedback_skill(task: Dict[str, Any], finding: Optional[dict]) -> str:
        versions = list((task.get("input") or {}).get("skill_versions") or {})
        versions.extend(
            str(item).rsplit("@", 1)[0]
            for item in ((task.get("report") or {}).get("collaboration") or {}).get(
                "activated_skills", []
            )
        )
        versions = list(dict.fromkeys(versions))
        if not versions:
            return ""
        rule_id = str((finding or {}).get("rule_id", "")).upper()
        path = str((finding or {}).get("path", "")).lower()
        if any(token in rule_id or token in path for token in (
            "SEC", "AUTH", "TOKEN", "PERMISSION", "CREDENTIAL",
        )) and "review-auth-security" in versions:
            return "review-auth-security"
        if any(token in rule_id or token in path for token in (
            "SQL", "DB", "MIGRATION", "SCHEMA",
        )) and "review-database-migration" in versions:
            return "review-database-migration"
        if any(token in rule_id or token in path for token in (
            "ASYNC", "RETRY", "QUEUE", "LOCK", "STREAM", "WORKER",
        )) and "review-async-reliability" in versions:
            return "review-async-reliability"
        return versions[0] if len(versions) == 1 else ""

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
