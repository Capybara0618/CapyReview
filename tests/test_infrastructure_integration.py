"""Opt-in integration tests against real PostgreSQL and Redis services."""

import os
import threading
import unittest
import uuid

from capyreview.models import ReviewReport, TaskState, TraceEvent
from capyreview.postgres_store import PostgresTaskStore
from capyreview.store import utc_now
from capyreview.task_queue import TaskQueue


DATABASE_URL = os.getenv("CAPYREVIEW_TEST_DATABASE_URL", "")
REDIS_URL = os.getenv("CAPYREVIEW_TEST_REDIS_URL", "")


@unittest.skipUnless(DATABASE_URL, "CAPYREVIEW_TEST_DATABASE_URL is not set")
class PostgreSQLIntegrationTests(unittest.TestCase):
    def test_task_checkpoint_trace_and_report_round_trip(self):
        store = PostgresTaskStore(DATABASE_URL)
        task_id = "integration-%s" % uuid.uuid4()
        store.create(task_id, "integration/repo", 1, {"source": "integration"})
        store.save_task_payload(task_id, "diff --git a/a.py b/a.py")
        store.transition(
            task_id,
            TraceEvent(1, TaskState.PLANNING, "planned", utc_now()),
        )
        store.save_checkpoint(task_id, "plan", {"route": "routine"})
        report = ReviewReport(
            "integration/repo", 1, "reviewed", "low", reviewer="integration"
        )
        store.succeed(
            task_id,
            report,
            TraceEvent(2, TaskState.SUCCESS, "completed", utc_now()),
        )

        task = store.get(task_id)

        self.assertEqual("SUCCESS", task["state"])
        self.assertEqual("integration", task["report"]["reviewer"])
        self.assertEqual(["PLANNING", "SUCCESS"], [x["state"] for x in task["trace"]])
        self.assertEqual("routine", store.load_checkpoints(task_id)["plan"]["state"]["route"])
        self.assertIn("diff --git", store.get_task_payload(task_id))


@unittest.skipUnless(REDIS_URL, "CAPYREVIEW_TEST_REDIS_URL is not set")
class RedisStreamsIntegrationTests(unittest.TestCase):
    def test_consumer_group_delivers_and_acknowledges_task(self):
        task_id = "integration-%s" % uuid.uuid4()
        delivered = threading.Event()
        received = []

        def handler(payload):
            received.append(payload)
            if payload.get("task_id") == task_id:
                delivered.set()

        queue = TaskQueue(handler, REDIS_URL, max_attempts=2, lease_seconds=2)
        try:
            queue.submit({"task_id": task_id}, message_id=task_id)
            self.assertTrue(delivered.wait(10), "Redis Streams task was not delivered")
        finally:
            queue.close()

        self.assertEqual("redis-streams", queue.backend)
        self.assertTrue(any(item.get("task_id") == task_id for item in received))


if __name__ == "__main__":
    unittest.main()
