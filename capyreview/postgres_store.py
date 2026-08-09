"""PostgreSQL persistence mirroring the SQLite product-core contract."""

import json
from typing import Any, Dict, Optional

from .models import ReviewReport, TaskState, TraceEvent
from .store import utc_now


SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        repository TEXT NOT NULL,
        pull_request INTEGER,
        input_json JSONB NOT NULL,
        report_json JSONB,
        error TEXT,
        cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS trace_events (
        id BIGSERIAL PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        step INTEGER NOT NULL,
        state TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS checkpoints (
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        node TEXT NOT NULL,
        status TEXT NOT NULL,
        attempt INTEGER NOT NULL DEFAULT 1,
        state_json JSONB NOT NULL,
        error TEXT,
        updated_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY(task_id, node)
    )""",
    """CREATE TABLE IF NOT EXISTS task_payloads (
        task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
        diff TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS agent_messages (
        id BIGSERIAL PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        sender TEXT NOT NULL,
        recipient TEXT NOT NULL,
        kind TEXT NOT NULL,
        correlation_id TEXT NOT NULL,
        content_json JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS webhook_deliveries (
        delivery_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        task_id TEXT,
        received_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS agent_memories (
        id TEXT PRIMARY KEY,
        repository TEXT NOT NULL,
        task_id TEXT NOT NULL DEFAULT '',
        agent TEXT NOT NULL DEFAULT '',
        scope TEXT NOT NULL,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        keywords_json JSONB NOT NULL,
        metadata_json JSONB NOT NULL,
        importance DOUBLE PRECISION NOT NULL DEFAULT .5,
        created_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS failure_cases (
        id BIGSERIAL PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        category TEXT NOT NULL,
        payload_json JSONB NOT NULL,
        resolved BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS evaluation_cases (
        id BIGSERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        split TEXT NOT NULL,
        diff TEXT NOT NULL,
        expected_json JSONB NOT NULL,
        source TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS evolution_runs (
        id TEXT PRIMARY KEY,
        skill_name TEXT NOT NULL,
        candidate_version INTEGER NOT NULL,
        baseline_version INTEGER,
        decision TEXT NOT NULL,
        candidate_score DOUBLE PRECISION NOT NULL,
        baseline_score DOUBLE PRECISION NOT NULL,
        metrics_json JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS skill_versions (
        id BIGSERIAL PRIMARY KEY,
        skill_name TEXT NOT NULL,
        version INTEGER NOT NULL,
        prompt TEXT NOT NULL,
        score DOUBLE PRECISION NOT NULL,
        active BOOLEAN NOT NULL DEFAULT FALSE,
        parent_version INTEGER,
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE(skill_name, version)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at)",
    """CREATE INDEX IF NOT EXISTS idx_agent_memories_lookup
        ON agent_memories(repository, scope, created_at)""",
)


def _bounded_limit(value: int, maximum: int = 500) -> int:
    return max(1, min(int(value), maximum))


def _json_value(value):
    return json.loads(value) if isinstance(value, str) else value


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


class PostgresTaskStore:
    def __init__(self, url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL mode requires: pip install psycopg[binary]"
            ) from exc
        self.psycopg = psycopg
        self.dict_row = dict_row
        self.url = url
        self._init()

    def _connect(self):
        return self.psycopg.connect(self.url, row_factory=self.dict_row)

    def _init(self) -> None:
        with self._connect() as conn:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)

    @staticmethod
    def _insert_trace(conn, task_id: str, event: TraceEvent) -> None:
        conn.execute(
            "INSERT INTO trace_events(task_id,step,state,message,created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
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
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks(id,state,repository,pull_request,input_json,"
                "report_json,error,cancel_requested,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,NULL,NULL,FALSE,%s,%s)",
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
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=%s,updated_at=%s WHERE id=%s",
                (event.state.value, event.created_at, task_id),
            )
            self._insert_trace(conn, task_id, event)

    def succeed(
        self, task_id: str, report: ReviewReport, event: TraceEvent
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=%s,report_json=%s::jsonb,error=NULL,"
                "cancel_requested=FALSE,updated_at=%s WHERE id=%s",
                (
                    TaskState.SUCCESS.value,
                    json.dumps(report.to_dict(), ensure_ascii=False),
                    event.created_at,
                    task_id,
                ),
            )
            self._insert_trace(conn, task_id, event)

    def fail(self, task_id: str, error: str, event: TraceEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=%s,error=%s,updated_at=%s WHERE id=%s",
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
                "error,cancel_requested,created_at,updated_at FROM tasks WHERE id=%s",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            events = conn.execute(
                "SELECT step,state,message,created_at FROM trace_events "
                "WHERE task_id=%s ORDER BY id",
                (task_id,),
            ).fetchall()
            messages = conn.execute(
                "SELECT sender,recipient,kind,correlation_id,content_json,created_at "
                "FROM agent_messages WHERE task_id=%s ORDER BY id",
                (task_id,),
            ).fetchall()

        value = dict(row)
        value["input"] = _json_value(value.pop("input_json"))
        value["report"] = _json_value(value.pop("report_json"))
        value["created_at"] = _iso(value["created_at"])
        value["updated_at"] = _iso(value["updated_at"])
        value["trace"] = []
        for event in events:
            item = dict(event)
            item["created_at"] = _iso(item["created_at"])
            value["trace"].append(item)
        value["collaboration"] = []
        for message in messages:
            item = dict(message)
            item["content"] = _json_value(item.pop("content_json"))
            item["created_at"] = _iso(item["created_at"])
            value["collaboration"].append(item)
        return value

    def list_tasks(self, limit: int = 50) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,state,repository,pull_request,error,created_at,updated_at "
                "FROM tasks ORDER BY created_at DESC LIMIT %s",
                (_bounded_limit(limit, 200),),
            ).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            item["created_at"] = _iso(item["created_at"])
            item["updated_at"] = _iso(item["updated_at"])
            values.append(item)
        return values

    def dashboard_stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total,"
                "COUNT(*) FILTER (WHERE state='SUCCESS') AS success,"
                "COUNT(*) FILTER (WHERE state='FAILED') AS failed FROM tasks"
            ).fetchone()
            failures = conn.execute(
                "SELECT COUNT(*) AS n FROM failure_cases WHERE resolved=FALSE"
            ).fetchone()["n"]
            skill_versions = conn.execute(
                "SELECT COUNT(*) AS n FROM skill_versions WHERE active=TRUE"
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
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_messages(task_id,sender,recipient,kind,"
                "correlation_id,content_json,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)",
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
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO agent_memories(id,repository,task_id,agent,scope,kind,"
                "content,keywords_json,metadata_json,importance,created_at,expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET "
                "importance=GREATEST(agent_memories.importance,EXCLUDED.importance),"
                "expires_at=EXCLUDED.expires_at RETURNING *",
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
            ).fetchone()
        return self._memory_from_row(row)

    def list_agent_memories(
        self, repository: str, scopes: tuple, limit: int = 100
    ) -> list:
        if not scopes:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_memories WHERE repository=%s "
                "AND scope=ANY(%s) AND (expires_at IS NULL OR expires_at>%s) "
                "ORDER BY importance DESC,created_at DESC LIMIT %s",
                (
                    repository,
                    list(scopes),
                    utc_now(),
                    _bounded_limit(limit),
                ),
            ).fetchall()
        return [self._memory_from_row(row) for row in rows]

    def delete_agent_memories(
        self, task_id: str = "", scope: str = ""
    ) -> int:
        clauses = []
        params = []
        if task_id:
            clauses.append("task_id=%s")
            params.append(task_id)
        if scope:
            clauses.append("scope=%s")
            params.append(scope)
        if not clauses:
            raise ValueError("memory deletion requires task_id or scope")
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_memories WHERE " + " AND ".join(clauses),
                params,
            )
            return cursor.rowcount

    def purge_expired_agent_memories(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_memories WHERE expires_at IS NOT NULL "
                "AND expires_at<=%s",
                (utc_now(),),
            )
            return cursor.rowcount

    @staticmethod
    def _memory_from_row(row) -> Dict[str, Any]:
        value = dict(row)
        value["keywords"] = _json_value(value.pop("keywords_json"))
        value["metadata"] = _json_value(value.pop("metadata_json"))
        value["created_at"] = _iso(value["created_at"])
        value["expires_at"] = _iso(value["expires_at"])
        return value

    def record_failure_case(
        self, task_id: str, category: str, payload: Dict[str, Any]
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO failure_cases(task_id,category,payload_json,created_at) "
                "VALUES (%s,%s,%s::jsonb,%s)",
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
            query += " WHERE resolved=FALSE"
        query += " ORDER BY id DESC LIMIT %s"
        params.append(_bounded_limit(limit))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._failure_from_row(row) for row in rows]

    def list_task_failure_cases(self, task_id: str) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM failure_cases WHERE task_id=%s ORDER BY id DESC",
                (task_id,),
            ).fetchall()
        return [self._failure_from_row(row) for row in rows]

    def resolve_failure_cases(self, case_ids: list) -> None:
        ids = [int(value) for value in case_ids]
        if not ids:
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE failure_cases SET resolved=TRUE WHERE id=ANY(%s)",
                (ids,),
            )

    @staticmethod
    def _failure_from_row(row) -> Dict[str, Any]:
        value = dict(row)
        value["payload"] = _json_value(value.pop("payload_json"))
        value["created_at"] = _iso(value["created_at"])
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
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evaluation_cases WHERE name=%s", (name,)
            ).fetchone()
            if row is not None:
                if (
                    row["split"] != split
                    or row["diff"] != diff
                    or _json_value(row["expected_json"]) != expected
                ):
                    raise ValueError(
                        "evaluation case names are immutable; use a new name "
                        "for revised content"
                    )
            else:
                row = conn.execute(
                    "INSERT INTO evaluation_cases(name,split,diff,expected_json,"
                    "source,active,created_at) VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s) "
                    "RETURNING *",
                    (
                        name,
                        split,
                        diff,
                        json.dumps(expected, ensure_ascii=False),
                        source,
                        active,
                        utc_now(),
                    ),
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
            clauses.append("split=%s")
            params.append(split)
        if active_only:
            clauses.append("active=TRUE")
        query = "SELECT * FROM evaluation_cases"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id LIMIT %s"
        params.append(_bounded_limit(limit))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._evaluation_from_row(row) for row in rows]

    @staticmethod
    def _evaluation_from_row(row) -> Dict[str, Any]:
        value = dict(row)
        value["expected"] = _json_value(value.pop("expected_json"))
        value["created_at"] = _iso(value["created_at"])
        return value

    def save_evolution_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO evolution_runs(id,skill_name,candidate_version,"
                "baseline_version,decision,candidate_score,baseline_score,"
                "metrics_json,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) "
                "RETURNING *",
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
            ).fetchone()
        return self._run_from_row(row)

    def list_evolution_runs(self, limit: int = 50) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evolution_runs ORDER BY created_at DESC LIMIT %s",
                (_bounded_limit(limit, 200),),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    @staticmethod
    def _run_from_row(row) -> Dict[str, Any]:
        value = dict(row)
        value["metrics"] = _json_value(value.pop("metrics_json"))
        value["created_at"] = _iso(value["created_at"])
        return value

    def save_skill_version(
        self,
        skill_name: str,
        prompt: str,
        score: float,
        activate: bool = False,
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (skill_name,))
            maximum = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS version FROM skill_versions "
                "WHERE skill_name=%s",
                (skill_name,),
            ).fetchone()
            version = int(maximum["version"]) + 1
            parent = conn.execute(
                "SELECT version FROM skill_versions WHERE skill_name=%s AND active=TRUE "
                "ORDER BY version DESC LIMIT 1",
                (skill_name,),
            ).fetchone()
            if activate:
                conn.execute(
                    "UPDATE skill_versions SET active=FALSE WHERE skill_name=%s",
                    (skill_name,),
                )
            row = conn.execute(
                "INSERT INTO skill_versions(skill_name,version,prompt,score,active,"
                "parent_version,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING *",
                (
                    skill_name,
                    version,
                    prompt,
                    float(score),
                    activate,
                    parent["version"] if parent else None,
                    utc_now(),
                ),
            ).fetchone()
        return self._skill_from_row(row)

    def get_active_skill_version(
        self, skill_name: str
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_name=%s AND active=TRUE "
                "ORDER BY version DESC LIMIT 1",
                (skill_name,),
            ).fetchone()
        return self._skill_from_row(row) if row else None

    def list_skill_versions(self, skill_name: str) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_name=%s "
                "ORDER BY version DESC",
                (skill_name,),
            ).fetchall()
        return [self._skill_from_row(row) for row in rows]

    def activate_skill_version(self, skill_name: str, version: int) -> bool:
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM skill_versions WHERE skill_name=%s AND version=%s",
                (skill_name, version),
            ).fetchone()
            if not exists:
                return False
            conn.execute(
                "UPDATE skill_versions SET active=FALSE WHERE skill_name=%s",
                (skill_name,),
            )
            conn.execute(
                "UPDATE skill_versions SET active=TRUE WHERE skill_name=%s "
                "AND version=%s",
                (skill_name, version),
            )
        return True

    @staticmethod
    def _skill_from_row(row) -> Dict[str, Any]:
        value = dict(row)
        value["created_at"] = _iso(value["created_at"])
        return value

    def save_task_payload(self, task_id: str, diff: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO task_payloads(task_id,diff,created_at) VALUES (%s,%s,%s) "
                "ON CONFLICT(task_id) DO UPDATE SET diff=EXCLUDED.diff,"
                "created_at=EXCLUDED.created_at",
                (task_id, diff, utc_now()),
            )

    def update_task_input(
        self, task_id: str, updates: Dict[str, Any]
    ) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT input_json FROM tasks WHERE id=%s", (task_id,)
            ).fetchone()
            if not row:
                raise ValueError("task not found")
            value = dict(_json_value(row["input_json"]) or {})
            value.update(updates)
            conn.execute(
                "UPDATE tasks SET input_json=%s::jsonb,updated_at=%s WHERE id=%s",
                (json.dumps(value, ensure_ascii=False), utc_now(), task_id),
            )

    def get_task_payload(self, task_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT diff FROM task_payloads WHERE task_id=%s", (task_id,)
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
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints(task_id,node,status,attempt,state_json,"
                "error,updated_at) VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s) "
                "ON CONFLICT(task_id,node) DO UPDATE SET status=EXCLUDED.status,"
                "attempt=EXCLUDED.attempt,state_json=EXCLUDED.state_json,"
                "error=EXCLUDED.error,updated_at=EXCLUDED.updated_at",
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
                "FROM checkpoints WHERE task_id=%s ORDER BY updated_at",
                (task_id,),
            ).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            item["state"] = _json_value(item.pop("state_json"))
            item["updated_at"] = _iso(item["updated_at"])
            result[item.pop("node")] = item
        return result

    def request_cancel(self, task_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET cancel_requested=TRUE,updated_at=%s "
                "WHERE id=%s AND state<>%s",
                (utc_now(), task_id, TaskState.SUCCESS.value),
            )
            return cursor.rowcount > 0

    def is_cancelled(self, task_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM tasks WHERE id=%s", (task_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def cancel(self, task_id: str, event: TraceEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=%s,cancel_requested=TRUE,updated_at=%s "
                "WHERE id=%s",
                (TaskState.CANCELLED.value, event.created_at, task_id),
            )
            self._insert_trace(conn, task_id, event)

    def prepare_resume(self, task_id: str) -> bool:
        """Reset a non-success task for checkpoint-backed queue resumption."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET state=%s,error=NULL,cancel_requested=FALSE,"
                "updated_at=%s WHERE id=%s AND state<>%s",
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
        with self._connect() as conn:
            inserted = conn.execute(
                "INSERT INTO webhook_deliveries(delivery_id,event_type,"
                "payload_sha256,received_at) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT(delivery_id) DO NOTHING RETURNING delivery_id",
                (delivery_id, event_type, payload_sha256, utc_now()),
            ).fetchone()
            if inserted:
                return True
            row = conn.execute(
                "SELECT event_type,payload_sha256 FROM webhook_deliveries "
                "WHERE delivery_id=%s",
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
        with self._connect() as conn:
            conn.execute(
                "UPDATE webhook_deliveries SET task_id=%s WHERE delivery_id=%s",
                (task_id, delivery_id),
            )

    def get_webhook(self, delivery_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT delivery_id,event_type,payload_sha256,task_id,received_at "
                "FROM webhook_deliveries WHERE delivery_id=%s",
                (delivery_id,),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["received_at"] = _iso(value["received_at"])
        return value


def create_store(database_url: str, sqlite_path: str):
    if database_url.startswith(("postgres://", "postgresql://")):
        return PostgresTaskStore(database_url)
    from .store import TaskStore

    return TaskStore(sqlite_path)
