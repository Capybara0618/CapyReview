"""SQLite persistence for the CapyReview product loop."""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional

from .models import ReviewReport, TaskState, TraceEvent


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


SQLITE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        repository TEXT NOT NULL,
        pull_request INTEGER,
        input_json TEXT NOT NULL,
        report_json TEXT,
        error TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS trace_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        step INTEGER NOT NULL,
        state TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS checkpoints (
        task_id TEXT NOT NULL,
        node TEXT NOT NULL,
        status TEXT NOT NULL,
        attempt INTEGER NOT NULL DEFAULT 1,
        state_json TEXT NOT NULL,
        error TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(task_id, node),
        FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS task_payloads (
        task_id TEXT PRIMARY KEY,
        diff TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS agent_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        sender TEXT NOT NULL,
        recipient TEXT NOT NULL,
        kind TEXT NOT NULL,
        correlation_id TEXT NOT NULL,
        content_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS webhook_deliveries (
        delivery_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        task_id TEXT,
        received_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS agent_memories (
        id TEXT PRIMARY KEY,
        repository TEXT NOT NULL,
        task_id TEXT NOT NULL DEFAULT '',
        agent TEXT NOT NULL DEFAULT '',
        scope TEXT NOT NULL,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        keywords_json TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        importance REAL NOT NULL DEFAULT 0.5,
        created_at TEXT NOT NULL,
        expires_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS failure_cases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        category TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        resolved INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS evaluation_cases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        split TEXT NOT NULL,
        diff TEXT NOT NULL,
        expected_json TEXT NOT NULL,
        source TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS evolution_runs (
        id TEXT PRIMARY KEY,
        skill_name TEXT NOT NULL,
        candidate_version INTEGER NOT NULL,
        baseline_version INTEGER,
        decision TEXT NOT NULL,
        candidate_score REAL NOT NULL,
        baseline_score REAL NOT NULL,
        metrics_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS skill_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        skill_name TEXT NOT NULL,
        version INTEGER NOT NULL,
        prompt TEXT NOT NULL,
        score REAL NOT NULL,
        active INTEGER NOT NULL DEFAULT 0,
        parent_version INTEGER,
        created_at TEXT NOT NULL,
        UNIQUE(skill_name, version)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at)",
    """CREATE INDEX IF NOT EXISTS idx_agent_memories_lookup
        ON agent_memories(repository, scope, created_at)""",
)


def _bounded_limit(value: int, maximum: int = 500) -> int:
    return max(1, min(int(value), maximum))


class TaskStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._init()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self) -> None:
        with self._connect() as conn:
            for statement in SQLITE_SCHEMA:
                conn.execute(statement)

    @staticmethod
    def _insert_trace(
        conn: sqlite3.Connection, task_id: str, event: TraceEvent
    ) -> None:
        conn.execute(
            "INSERT INTO trace_events(task_id,step,state,message,created_at) "
            "VALUES (?,?,?,?,?)",
            (
                task_id,
                event.step,
                event.state.value,
                event.message,
                event.created_at,
            ),
        )

    def create(
        self,
        task_id: str,
        repository: str,
        pull_request: Optional[int],
        payload: Dict[str, Any],
    ) -> None:
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks(id,state,repository,pull_request,input_json,"
                "report_json,error,cancel_requested,created_at,updated_at) "
                "VALUES (?,?,?,?,?,NULL,NULL,0,?,?)",
                (
                    task_id,
                    TaskState.PENDING.value,
                    repository,
                    pull_request,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )

    def transition(self, task_id: str, event: TraceEvent) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=?,updated_at=? WHERE id=?",
                (event.state.value, event.created_at, task_id),
            )
            self._insert_trace(conn, task_id, event)

    def succeed(
        self, task_id: str, report: ReviewReport, event: TraceEvent
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=?,report_json=?,error=NULL,"
                "cancel_requested=0,updated_at=? WHERE id=?",
                (
                    TaskState.SUCCESS.value,
                    json.dumps(report.to_dict(), ensure_ascii=False),
                    event.created_at,
                    task_id,
                ),
            )
            self._insert_trace(conn, task_id, event)

    def fail(self, task_id: str, error: str, event: TraceEvent) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=?,error=?,updated_at=? WHERE id=?",
                (
                    TaskState.FAILED.value,
                    str(error)[:2000],
                    event.created_at,
                    task_id,
                ),
            )
            self._insert_trace(conn, task_id, event)

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id,state,repository,pull_request,input_json,report_json,"
                "error,cancel_requested,created_at,updated_at FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            events = conn.execute(
                "SELECT step,state,message,created_at FROM trace_events "
                "WHERE task_id=? ORDER BY id",
                (task_id,),
            ).fetchall()
            messages = conn.execute(
                "SELECT sender,recipient,kind,correlation_id,content_json,created_at "
                "FROM agent_messages WHERE task_id=? ORDER BY id",
                (task_id,),
            ).fetchall()

        value = dict(row)
        value["input"] = json.loads(value.pop("input_json"))
        raw_report = value.pop("report_json")
        value["report"] = json.loads(raw_report) if raw_report else None
        value["cancel_requested"] = bool(value["cancel_requested"])
        value["trace"] = [dict(event) for event in events]
        value["collaboration"] = []
        for message in messages:
            item = dict(message)
            item["content"] = json.loads(item.pop("content_json"))
            value["collaboration"].append(item)
        return value

    def list_tasks(self, limit: int = 50) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,state,repository,pull_request,error,created_at,updated_at "
                "FROM tasks ORDER BY created_at DESC LIMIT ?",
                (_bounded_limit(limit, 200),),
            ).fetchall()
        return [dict(row) for row in rows]

    def dashboard_stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total,"
                "SUM(CASE WHEN state='SUCCESS' THEN 1 ELSE 0 END) AS success,"
                "SUM(CASE WHEN state='FAILED' THEN 1 ELSE 0 END) AS failed "
                "FROM tasks"
            ).fetchone()
            failures = conn.execute(
                "SELECT COUNT(*) AS n FROM failure_cases WHERE resolved=0"
            ).fetchone()["n"]
            skill_versions = conn.execute(
                "SELECT COUNT(*) AS n FROM skill_versions WHERE active=1"
            ).fetchone()["n"]
        total = int(row["total"] or 0)
        success = int(row["success"] or 0)
        failed = int(row["failed"] or 0)
        return {
            "tasks_total": total,
            "tasks_success": success,
            "tasks_failed": failed,
            "success_rate": round(success / total, 4) if total else 0.0,
            "unresolved_failure_cases": int(failures),
            "active_skill_versions": int(skill_versions),
        }

    def record_agent_message(
        self, task_id: str, message: Dict[str, Any]
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_messages(task_id,sender,recipient,kind,"
                "correlation_id,content_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    task_id,
                    message["sender"],
                    message["recipient"],
                    message["kind"],
                    message.get("correlation_id", ""),
                    json.dumps(message.get("content", {}), ensure_ascii=False),
                    message.get("created_at") or utc_now(),
                ),
            )

    def save_agent_memory(self, memory: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_memories(id,repository,task_id,agent,scope,kind,"
                "content,keywords_json,metadata_json,importance,created_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "importance=MAX(agent_memories.importance,excluded.importance),"
                "expires_at=excluded.expires_at",
                (
                    memory["id"],
                    memory["repository"],
                    memory.get("task_id", ""),
                    memory.get("agent", ""),
                    memory["scope"],
                    memory["kind"],
                    memory["content"],
                    json.dumps(memory.get("keywords", []), ensure_ascii=False),
                    json.dumps(memory.get("metadata", {}), ensure_ascii=False),
                    float(memory.get("importance", 0.5)),
                    memory.get("created_at") or utc_now(),
                    memory.get("expires_at"),
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_memories WHERE id=?", (memory["id"],)
            ).fetchone()
        return self._memory_from_row(row)

    def list_agent_memories(
        self, repository: str, scopes: tuple, limit: int = 100
    ) -> list:
        if not scopes:
            return []
        placeholders = ",".join("?" for _ in scopes)
        params = [repository, *scopes, utc_now(), _bounded_limit(limit)]
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_memories WHERE repository=? "
                "AND scope IN (%s) AND (expires_at IS NULL OR expires_at>?) "
                "ORDER BY importance DESC,created_at DESC LIMIT ?" % placeholders,
                params,
            ).fetchall()
        return [self._memory_from_row(row) for row in rows]

    def delete_agent_memories(
        self, task_id: str = "", scope: str = ""
    ) -> int:
        clauses = []
        params = []
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if scope:
            clauses.append("scope=?")
            params.append(scope)
        if not clauses:
            raise ValueError("memory deletion requires task_id or scope")
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_memories WHERE " + " AND ".join(clauses),
                params,
            )
            return cursor.rowcount

    def purge_expired_agent_memories(self) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_memories WHERE expires_at IS NOT NULL "
                "AND expires_at<=?",
                (utc_now(),),
            )
            return cursor.rowcount

    @staticmethod
    def _memory_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        value = dict(row)
        value["keywords"] = json.loads(value.pop("keywords_json"))
        value["metadata"] = json.loads(value.pop("metadata_json"))
        return value

    def record_failure_case(
        self, task_id: str, category: str, payload: Dict[str, Any]
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO failure_cases(task_id,category,payload_json,created_at) "
                "VALUES (?,?,?,?)",
                (
                    task_id,
                    category,
                    json.dumps(payload, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def list_failure_cases(
        self, unresolved_only: bool = False, limit: int = 100
    ) -> list:
        query = "SELECT * FROM failure_cases"
        params = []
        if unresolved_only:
            query += " WHERE resolved=0"
        query += " ORDER BY id DESC LIMIT ?"
        params.append(_bounded_limit(limit))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._failure_from_row(row) for row in rows]

    def list_task_failure_cases(self, task_id: str) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM failure_cases WHERE task_id=? ORDER BY id DESC",
                (task_id,),
            ).fetchall()
        return [self._failure_from_row(row) for row in rows]

    def resolve_failure_cases(self, case_ids: list) -> None:
        ids = [int(value) for value in case_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE failure_cases SET resolved=1 WHERE id IN (%s)"
                % placeholders,
                ids,
            )

    @staticmethod
    def _failure_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        value["resolved"] = bool(value["resolved"])
        return value

    def save_evaluation_case(
        self,
        name: str,
        split: str,
        diff: str,
        expected: list,
        source: str = "manual",
        active: bool = True,
    ) -> Dict[str, Any]:
        encoded = json.dumps(expected, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_cases WHERE name=?", (name,)
            ).fetchone()
            if row is not None:
                if (
                    row["split"] != split
                    or row["diff"] != diff
                    or json.loads(row["expected_json"]) != expected
                ):
                    raise ValueError(
                        "evaluation case names are immutable; use a new name "
                        "for revised content"
                    )
            else:
                conn.execute(
                    "INSERT INTO evaluation_cases(name,split,diff,expected_json,"
                    "source,active,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        name,
                        split,
                        diff,
                        encoded,
                        source,
                        int(active),
                        utc_now(),
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM evaluation_cases WHERE name=?", (name,)
                ).fetchone()
        return self._evaluation_from_row(row)

    def list_evaluation_cases(
        self,
        split: Optional[str] = None,
        active_only: bool = True,
        limit: int = 100,
    ) -> list:
        clauses = []
        params = []
        if split:
            clauses.append("split=?")
            params.append(split)
        if active_only:
            clauses.append("active=1")
        query = "SELECT * FROM evaluation_cases"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id LIMIT ?"
        params.append(_bounded_limit(limit))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._evaluation_from_row(row) for row in rows]

    @staticmethod
    def _evaluation_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        value = dict(row)
        value["expected"] = json.loads(value.pop("expected_json"))
        value["active"] = bool(value["active"])
        return value

    def save_evolution_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO evolution_runs(id,skill_name,candidate_version,"
                "baseline_version,decision,candidate_score,baseline_score,"
                "metrics_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run["id"],
                    run["skill_name"],
                    run["candidate_version"],
                    run.get("baseline_version"),
                    run["decision"],
                    run["candidate_score"],
                    run["baseline_score"],
                    json.dumps(run["metrics"], ensure_ascii=False),
                    run["created_at"],
                ),
            )
            row = conn.execute(
                "SELECT * FROM evolution_runs WHERE id=?", (run["id"],)
            ).fetchone()
        return self._run_from_row(row)

    def list_evolution_runs(self, limit: int = 50) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evolution_runs ORDER BY created_at DESC LIMIT ?",
                (_bounded_limit(limit, 200),),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        value = dict(row)
        value["metrics"] = json.loads(value.pop("metrics_json"))
        return value

    def save_skill_version(
        self,
        skill_name: str,
        prompt: str,
        score: float,
        activate: bool = False,
    ) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            maximum = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS version FROM skill_versions "
                "WHERE skill_name=?",
                (skill_name,),
            ).fetchone()
            version = int(maximum["version"]) + 1
            parent = conn.execute(
                "SELECT version FROM skill_versions WHERE skill_name=? AND active=1 "
                "ORDER BY version DESC LIMIT 1",
                (skill_name,),
            ).fetchone()
            if activate:
                conn.execute(
                    "UPDATE skill_versions SET active=0 WHERE skill_name=?",
                    (skill_name,),
                )
            conn.execute(
                "INSERT INTO skill_versions(skill_name,version,prompt,score,active,"
                "parent_version,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    skill_name,
                    version,
                    prompt,
                    float(score),
                    int(activate),
                    parent["version"] if parent else None,
                    utc_now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_name=? AND version=?",
                (skill_name, version),
            ).fetchone()
        return self._skill_from_row(row)

    def get_active_skill_version(
        self, skill_name: str
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_name=? AND active=1 "
                "ORDER BY version DESC LIMIT 1",
                (skill_name,),
            ).fetchone()
        return self._skill_from_row(row) if row else None

    def list_skill_versions(self, skill_name: str) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_name=? "
                "ORDER BY version DESC",
                (skill_name,),
            ).fetchall()
        return [self._skill_from_row(row) for row in rows]

    def activate_skill_version(self, skill_name: str, version: int) -> bool:
        with self._lock, self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM skill_versions WHERE skill_name=? AND version=?",
                (skill_name, version),
            ).fetchone()
            if not exists:
                return False
            conn.execute(
                "UPDATE skill_versions SET active=0 WHERE skill_name=?",
                (skill_name,),
            )
            conn.execute(
                "UPDATE skill_versions SET active=1 WHERE skill_name=? AND version=?",
                (skill_name, version),
            )
        return True

    @staticmethod
    def _skill_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        value = dict(row)
        value["active"] = bool(value["active"])
        return value

    def save_task_payload(self, task_id: str, diff: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO task_payloads(task_id,diff,created_at) VALUES (?,?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET diff=excluded.diff,"
                "created_at=excluded.created_at",
                (task_id, diff, utc_now()),
            )

    def update_task_input(
        self, task_id: str, updates: Dict[str, Any]
    ) -> None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT input_json FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                raise ValueError("task not found")
            value = json.loads(row["input_json"])
            value.update(updates)
            conn.execute(
                "UPDATE tasks SET input_json=?,updated_at=? WHERE id=?",
                (json.dumps(value, ensure_ascii=False), utc_now(), task_id),
            )

    def get_task_payload(self, task_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT diff FROM task_payloads WHERE task_id=?", (task_id,)
            ).fetchone()
        return str(row["diff"]) if row else None

    def save_checkpoint(
        self,
        task_id: str,
        node: str,
        state: Dict[str, Any],
        status: str = "completed",
        attempt: int = 1,
        error: str = "",
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints(task_id,node,status,attempt,state_json,"
                "error,updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(task_id,node) DO UPDATE SET status=excluded.status,"
                "attempt=excluded.attempt,state_json=excluded.state_json,"
                "error=excluded.error,updated_at=excluded.updated_at",
                (
                    task_id,
                    node,
                    status,
                    int(attempt),
                    json.dumps(state, ensure_ascii=False),
                    str(error)[:2000] or None,
                    utc_now(),
                ),
            )

    def load_checkpoints(self, task_id: str) -> Dict[str, Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT node,status,attempt,state_json,error,updated_at "
                "FROM checkpoints WHERE task_id=? ORDER BY updated_at",
                (task_id,),
            ).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            item["state"] = json.loads(item.pop("state_json"))
            result[item.pop("node")] = item
        return result

    def request_cancel(self, task_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET cancel_requested=1,updated_at=? "
                "WHERE id=? AND state<>?",
                (utc_now(), task_id, TaskState.SUCCESS.value),
            )
            return cursor.rowcount > 0

    def is_cancelled(self, task_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def cancel(self, task_id: str, event: TraceEvent) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=?,cancel_requested=1,updated_at=? WHERE id=?",
                (TaskState.CANCELLED.value, event.created_at, task_id),
            )
            self._insert_trace(conn, task_id, event)

    def prepare_resume(self, task_id: str) -> bool:
        """Reset a non-success task for checkpoint-backed queue resumption."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET state=?,error=NULL,cancel_requested=0,updated_at=? "
                "WHERE id=? AND state<>?",
                (
                    TaskState.PENDING.value,
                    utc_now(),
                    task_id,
                    TaskState.SUCCESS.value,
                ),
            )
            return cursor.rowcount > 0

    def claim_webhook(
        self, delivery_id: str, event_type: str, payload_sha256: str
    ) -> bool:
        if not delivery_id:
            raise ValueError("X-GitHub-Delivery is required")
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO webhook_deliveries(delivery_id,event_type,"
                    "payload_sha256,received_at) VALUES (?,?,?,?)",
                    (delivery_id, event_type, payload_sha256, utc_now()),
                )
                return True
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT event_type,payload_sha256 FROM webhook_deliveries "
                    "WHERE delivery_id=?",
                    (delivery_id,),
                ).fetchone()
                if row and (
                    row["payload_sha256"] != payload_sha256
                    or row["event_type"] != event_type
                ):
                    raise ValueError(
                        "delivery id was already used with a different payload"
                    )
                return False

    def complete_webhook(
        self, delivery_id: str, task_id: Optional[str]
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE webhook_deliveries SET task_id=? WHERE delivery_id=?",
                (task_id, delivery_id),
            )

    def get_webhook(self, delivery_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT delivery_id,event_type,payload_sha256,task_id,received_at "
                "FROM webhook_deliveries WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
        return dict(row) if row else None
