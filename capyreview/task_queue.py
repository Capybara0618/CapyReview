"""Single-consumer task delivery with ACK, leases and bounded retries."""
import json
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional


class PermanentTaskError(RuntimeError):
    """An error that must not be retried."""


class TaskQueue:
    STREAM = "capyreview:review:stream"
    GROUP = "capyreview-workers"

    def __init__(
        self, handler: Callable[[Dict[str, Any]], None], redis_url: str,
        max_attempts: int = 3, lease_seconds: int = 60,
        on_terminal_failure: Optional[Callable[[Dict[str, Any], str], None]] = None,
    ):
        if not redis_url.startswith(("redis://", "rediss://")):
            raise ValueError("CAPYREVIEW_REDIS_URL must be a Redis connection URL")
        self.handler = handler
        self.redis_url = redis_url
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.on_terminal_failure = on_terminal_failure
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="capyreview-worker"
        )
        self._stop = threading.Event()
        self.consumer = "%s-%s" % (socket.gethostname(), uuid.uuid4().hex[:8])
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("Redis mode requires: pip install redis") from exc
        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self._redis.ping()
        try:
            self._redis.xgroup_create(self.STREAM, self.GROUP, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._executor.submit(self._redis_worker)

    @property
    def backend(self) -> str:
        return "redis-streams"

    def submit(self, payload: Dict[str, Any], message_id: str = "") -> str:
        envelope = {
            "message_id": message_id or str(payload.get("task_id") or uuid.uuid4()),
            "attempt": 0,
            "payload": payload,
            "submitted_at": time.time(),
        }
        self._redis.xadd(
            self.STREAM, {"envelope": json.dumps(envelope, ensure_ascii=False)}
        )
        return envelope["message_id"]

    def _deliver(self, envelope: Dict[str, Any]) -> bool:
        envelope["attempt"] = int(envelope.get("attempt", 0)) + 1
        try:
            self.handler(envelope["payload"])
            return True
        except PermanentTaskError as exc:
            self._terminal_failure(envelope, str(exc))
            return False
        except Exception as exc:
            if envelope["attempt"] >= self.max_attempts:
                self._terminal_failure(envelope, str(exc))
            else:
                self._redis.xadd(self.STREAM, {
                    "envelope": json.dumps(envelope, ensure_ascii=False)
                })
            return False

    def _redis_worker(self) -> None:
        while not self._stop.is_set():
            self._reclaim_stale()
            messages = self._redis.xreadgroup(
                self.GROUP, self.consumer, {self.STREAM: ">"}, count=1, block=1000
            )
            for _stream, entries in messages:
                for redis_id, fields in entries:
                    try:
                        envelope = json.loads(fields["envelope"])
                    except Exception as exc:
                        envelope = {
                            "message_id": redis_id, "attempt": self.max_attempts,
                            "payload": {}, "submitted_at": time.time(),
                        }
                        self._terminal_failure(
                            envelope, "invalid queue envelope: %s" % exc
                        )
                        self._redis.xack(self.STREAM, self.GROUP, redis_id)
                        continue
                    try:
                        self._deliver(envelope)
                        # ACK only after work completed, was requeued, or its
                        # terminal failure was persisted on the task.
                        self._redis.xack(self.STREAM, self.GROUP, redis_id)
                    except Exception:
                        # Infrastructure failure: leave pending for lease recovery.
                        continue

    def _reclaim_stale(self) -> None:
        try:
            result = self._redis.xautoclaim(
                self.STREAM, self.GROUP, self.consumer,
                min_idle_time=self.lease_seconds * 1000, start_id="0-0", count=10,
            )
            entries = result[1] if len(result) > 1 else []
            for redis_id, fields in entries:
                envelope = json.loads(fields["envelope"])
                self._deliver(envelope)
                self._redis.xack(self.STREAM, self.GROUP, redis_id)
        except Exception:
            # Redis versions without XAUTOCLAIM still process new entries.
            return

    def _terminal_failure(self, envelope: Dict[str, Any], error: str) -> None:
        if self.on_terminal_failure:
            self.on_terminal_failure(
                envelope.get("payload") or {}, str(error)[:2000]
            )

    def close(self) -> None:
        self._stop.set()
        self._executor.shutdown(wait=True)
