"""Fast in-memory test doubles; production always uses PostgreSQL and Redis."""

from copy import deepcopy
from threading import RLock
from typing import Any, Dict, Optional

from capyreview.models import ReviewReport, TaskState, TraceEvent
from capyreview.store import utc_now


def _limit(value: int, maximum: int = 500) -> int:
    return max(1, min(int(value), maximum))


class InMemoryTaskStore:
    def __init__(self):
        self._lock = RLock()
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.payloads: Dict[str, str] = {}
        self.checkpoints: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.memories: Dict[str, Dict[str, Any]] = {}
        self.failures: list[Dict[str, Any]] = []
        self.evaluation_cases: list[Dict[str, Any]] = []
        self.evolution_runs: list[Dict[str, Any]] = []
        self.skill_versions: list[Dict[str, Any]] = []
        self.webhooks: Dict[str, Dict[str, Any]] = {}

    def create(
        self, task_id: str, repository: str, pull_request: Optional[int],
        payload: Dict[str, Any],
    ) -> None:
        now = utc_now()
        with self._lock:
            if task_id in self.tasks:
                raise ValueError("task already exists")
            self.tasks[task_id] = {
                "id": task_id,
                "state": TaskState.PENDING.value,
                "repository": repository,
                "pull_request": pull_request,
                "input": deepcopy(payload),
                "report": None,
                "error": None,
                "cancel_requested": False,
                "created_at": now,
                "updated_at": now,
                "trace": [],
                "collaboration": [],
            }

    def _trace(self, task_id: str, event: TraceEvent) -> None:
        self.tasks[task_id]["trace"].append({
            "step": event.step,
            "state": event.state.value,
            "message": event.message,
            "created_at": event.created_at,
        })

    def transition(self, task_id: str, event: TraceEvent) -> None:
        with self._lock:
            task = self.tasks[task_id]
            task["state"] = event.state.value
            task["updated_at"] = event.created_at
            self._trace(task_id, event)

    def succeed(
        self, task_id: str, report: ReviewReport, event: TraceEvent
    ) -> None:
        with self._lock:
            task = self.tasks[task_id]
            task.update({
                "state": TaskState.SUCCESS.value,
                "report": report.to_dict(),
                "error": None,
                "cancel_requested": False,
                "updated_at": event.created_at,
            })
            self._trace(task_id, event)

    def fail(self, task_id: str, error: str, event: TraceEvent) -> None:
        with self._lock:
            task = self.tasks[task_id]
            task.update({
                "state": TaskState.FAILED.value,
                "error": str(error)[:2000],
                "updated_at": event.created_at,
            })
            self._trace(task_id, event)

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            task = self.tasks.get(task_id)
            return deepcopy(task) if task is not None else None

    def list_tasks(self, limit: int = 50) -> list:
        with self._lock:
            rows = sorted(
                self.tasks.values(), key=lambda item: item["created_at"], reverse=True
            )[:_limit(limit, 200)]
            keys = (
                "id", "state", "repository", "pull_request", "error",
                "created_at", "updated_at",
            )
            return [{key: item[key] for key in keys} for item in rows]

    def dashboard_stats(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self.tasks)
            success = sum(
                item["state"] == TaskState.SUCCESS.value
                for item in self.tasks.values()
            )
            failed = sum(
                item["state"] == TaskState.FAILED.value
                for item in self.tasks.values()
            )
            return {
                "tasks_total": total,
                "tasks_success": success,
                "tasks_failed": failed,
                "success_rate": round(success / total, 4) if total else 0.0,
                "unresolved_failure_cases": sum(
                    not item["resolved"] for item in self.failures
                ),
                "active_skill_versions": sum(
                    item["active"] for item in self.skill_versions
                ),
            }

    def record_agent_message(
        self, task_id: str, message: Dict[str, Any]
    ) -> None:
        with self._lock:
            value = {
                "sender": message["sender"],
                "recipient": message["recipient"],
                "kind": message["kind"],
                "correlation_id": message.get("correlation_id", ""),
                "content": deepcopy(message.get("content", {})),
                "created_at": message.get("created_at") or utc_now(),
            }
            self.tasks[task_id]["collaboration"].append(value)

    def save_agent_memory(self, memory: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            existing = self.memories.get(memory["id"])
            if existing:
                existing["importance"] = max(
                    existing["importance"], float(memory.get("importance", 0.5))
                )
                existing["expires_at"] = memory.get("expires_at")
            else:
                existing = {
                    "id": memory["id"],
                    "repository": memory["repository"],
                    "task_id": memory.get("task_id", ""),
                    "agent": memory.get("agent", ""),
                    "scope": memory["scope"],
                    "kind": memory["kind"],
                    "content": memory["content"],
                    "keywords": deepcopy(memory.get("keywords", [])),
                    "metadata": deepcopy(memory.get("metadata", {})),
                    "importance": float(memory.get("importance", 0.5)),
                    "created_at": memory.get("created_at") or utc_now(),
                    "expires_at": memory.get("expires_at"),
                }
                self.memories[existing["id"]] = existing
            return deepcopy(existing)

    def list_agent_memories(
        self, repository: str, scopes: tuple, limit: int = 100
    ) -> list:
        if not scopes:
            return []
        now = utc_now()
        with self._lock:
            rows = [
                item for item in self.memories.values()
                if item["repository"] == repository
                and item["scope"] in scopes
                and (not item["expires_at"] or item["expires_at"] > now)
            ]
            rows.sort(
                key=lambda item: (item["importance"], item["created_at"]),
                reverse=True,
            )
            return deepcopy(rows[:_limit(limit)])

    def delete_agent_memories(
        self, task_id: str = "", scope: str = ""
    ) -> int:
        if not task_id and not scope:
            raise ValueError("memory deletion requires task_id or scope")
        with self._lock:
            ids = [
                key for key, item in self.memories.items()
                if (not task_id or item["task_id"] == task_id)
                and (not scope or item["scope"] == scope)
            ]
            for key in ids:
                del self.memories[key]
            return len(ids)

    def purge_expired_agent_memories(self) -> int:
        now = utc_now()
        with self._lock:
            ids = [
                key for key, item in self.memories.items()
                if item["expires_at"] and item["expires_at"] <= now
            ]
            for key in ids:
                del self.memories[key]
            return len(ids)

    def record_failure_case(
        self, task_id: str, category: str, payload: Dict[str, Any]
    ) -> None:
        with self._lock:
            self.failures.append({
                "id": len(self.failures) + 1,
                "task_id": task_id,
                "category": category,
                "payload": deepcopy(payload),
                "resolved": False,
                "created_at": utc_now(),
            })

    def list_failure_cases(
        self, unresolved_only: bool = False, limit: int = 100
    ) -> list:
        with self._lock:
            rows = [
                item for item in self.failures
                if not unresolved_only or not item["resolved"]
            ]
            return deepcopy(list(reversed(rows))[:_limit(limit)])

    def list_task_failure_cases(self, task_id: str) -> list:
        with self._lock:
            return deepcopy(list(reversed([
                item for item in self.failures if item["task_id"] == task_id
            ])))

    def resolve_failure_cases(self, case_ids: list) -> None:
        ids = {int(value) for value in case_ids}
        with self._lock:
            for item in self.failures:
                if item["id"] in ids:
                    item["resolved"] = True

    def save_evaluation_case(
        self, name: str, split: str, diff: str, expected: list,
        source: str = "manual", active: bool = True,
    ) -> Dict[str, Any]:
        with self._lock:
            existing = next(
                (item for item in self.evaluation_cases if item["name"] == name),
                None,
            )
            if existing:
                if (
                    existing["split"] != split
                    or existing["diff"] != diff
                    or existing["expected"] != expected
                ):
                    raise ValueError(
                        "evaluation case names are immutable; use a new name for revised content"
                    )
                return deepcopy(existing)
            value = {
                "id": len(self.evaluation_cases) + 1,
                "name": name,
                "split": split,
                "diff": diff,
                "expected": deepcopy(expected),
                "source": source,
                "active": bool(active),
                "created_at": utc_now(),
            }
            self.evaluation_cases.append(value)
            return deepcopy(value)

    def list_evaluation_cases(
        self, split: Optional[str] = None, active_only: bool = True,
        limit: int = 100,
    ) -> list:
        with self._lock:
            rows = [
                item for item in self.evaluation_cases
                if (not split or item["split"] == split)
                and (not active_only or item["active"])
            ]
            return deepcopy(rows[:_limit(limit)])

    def save_evolution_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if any(item["id"] == run["id"] for item in self.evolution_runs):
                raise ValueError("evolution run already exists")
            self.evolution_runs.append(deepcopy(run))
            return deepcopy(run)

    def list_evolution_runs(self, limit: int = 50) -> list:
        with self._lock:
            rows = sorted(
                self.evolution_runs,
                key=lambda item: item["created_at"], reverse=True,
            )
            return deepcopy(rows[:_limit(limit, 200)])

    def save_skill_version(
        self, skill_name: str, package: dict, score: float,
        activate: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
            matching = [
                item for item in self.skill_versions
                if item["skill_name"] == skill_name
            ]
            active = next((item for item in matching if item["active"]), None)
            if activate:
                for item in matching:
                    item["active"] = False
            value = {
                "id": len(self.skill_versions) + 1,
                "skill_name": skill_name,
                "version": max((item["version"] for item in matching), default=0) + 1,
                "package": deepcopy(package),
                "score": float(score),
                "active": bool(activate),
                "parent_version": active["version"] if active else None,
                "created_at": utc_now(),
            }
            self.skill_versions.append(value)
            return deepcopy(value)

    def get_active_skill_version(
        self, skill_name: str
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            rows = [
                item for item in self.skill_versions
                if item["skill_name"] == skill_name and item["active"]
            ]
            return deepcopy(max(rows, key=lambda item: item["version"])) if rows else None

    def list_skill_versions(self, skill_name: str) -> list:
        with self._lock:
            rows = [
                item for item in self.skill_versions
                if item["skill_name"] == skill_name
            ]
            rows.sort(key=lambda item: item["version"], reverse=True)
            return deepcopy(rows)

    def list_active_skill_versions(self) -> list:
        with self._lock:
            rows = [item for item in self.skill_versions if item["active"]]
            rows.sort(key=lambda item: (item["skill_name"], -item["version"]))
            return deepcopy(rows)

    def activate_skill_version(self, skill_name: str, version: int) -> bool:
        with self._lock:
            target = next((
                item for item in self.skill_versions
                if item["skill_name"] == skill_name and item["version"] == version
            ), None)
            if not target:
                return False
            for item in self.skill_versions:
                if item["skill_name"] == skill_name:
                    item["active"] = item is target
            return True

    def save_task_payload(self, task_id: str, diff: str) -> None:
        with self._lock:
            self.payloads[task_id] = diff

    def update_task_input(
        self, task_id: str, updates: Dict[str, Any]
    ) -> None:
        with self._lock:
            if task_id not in self.tasks:
                raise ValueError("task not found")
            self.tasks[task_id]["input"].update(deepcopy(updates))
            self.tasks[task_id]["updated_at"] = utc_now()

    def get_task_payload(self, task_id: str) -> Optional[str]:
        with self._lock:
            return self.payloads.get(task_id)

    def save_checkpoint(
        self, task_id: str, node: str, state: Dict[str, Any],
        status: str = "completed", attempt: int = 1, error: str = "",
    ) -> None:
        with self._lock:
            self.checkpoints.setdefault(task_id, {})[node] = {
                "status": status,
                "attempt": int(attempt),
                "state": deepcopy(state),
                "error": str(error)[:2000] or None,
                "updated_at": utc_now(),
            }

    def load_checkpoints(self, task_id: str) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return deepcopy(self.checkpoints.get(task_id, {}))

    def delete_checkpoint(self, task_id: str, node: str) -> bool:
        with self._lock:
            values = self.checkpoints.get(task_id, {})
            if node not in values:
                return False
            del values[node]
            return True

    def request_cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task["state"] == TaskState.SUCCESS.value:
                return False
            task["cancel_requested"] = True
            task["updated_at"] = utc_now()
            return True

    def is_cancelled(self, task_id: str) -> bool:
        with self._lock:
            task = self.tasks.get(task_id)
            return bool(task and task["cancel_requested"])

    def cancel(self, task_id: str, event: TraceEvent) -> None:
        with self._lock:
            task = self.tasks[task_id]
            task.update({
                "state": TaskState.CANCELLED.value,
                "cancel_requested": True,
                "updated_at": event.created_at,
            })
            self._trace(task_id, event)

    def prepare_resume(self, task_id: str) -> bool:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task["state"] == TaskState.SUCCESS.value:
                return False
            task.update({
                "state": TaskState.PENDING.value,
                "error": None,
                "cancel_requested": False,
                "updated_at": utc_now(),
            })
            return True

    def claim_webhook(
        self, delivery_id: str, event_type: str, payload_sha256: str
    ) -> bool:
        if not delivery_id:
            raise ValueError("X-GitHub-Delivery is required")
        with self._lock:
            existing = self.webhooks.get(delivery_id)
            if existing:
                if (
                    existing["event_type"] != event_type
                    or existing["payload_sha256"] != payload_sha256
                ):
                    raise ValueError(
                        "delivery id was already used with a different payload"
                    )
                return False
            self.webhooks[delivery_id] = {
                "delivery_id": delivery_id,
                "event_type": event_type,
                "payload_sha256": payload_sha256,
                "task_id": None,
                "received_at": utc_now(),
            }
            return True

    def complete_webhook(
        self, delivery_id: str, task_id: Optional[str]
    ) -> None:
        with self._lock:
            self.webhooks[delivery_id]["task_id"] = task_id

    def get_webhook(self, delivery_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            value = self.webhooks.get(delivery_id)
            return deepcopy(value) if value else None


class CapturingQueue:
    backend = "test-queue"

    def __init__(self):
        self.messages = []

    def submit(self, payload, message_id=""):
        self.messages.append((message_id, deepcopy(payload)))
        return message_id

    def close(self):
        return None
