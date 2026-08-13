import inspect
import unittest

from capyreview.models import ReviewReport, TaskState, TraceEvent
from capyreview.postgres_store import PostgresTaskStore, SCHEMA_STATEMENTS, create_store
from capyreview.store import utc_now
from tests.fakes import InMemoryTaskStore


CORE_TABLES = {
    "tasks",
    "trace_events",
    "checkpoints",
    "task_payloads",
    "agent_messages",
    "webhook_deliveries",
    "agent_memories",
    "failure_cases",
    "evaluation_cases",
    "evolution_runs",
    "skill_versions",
}

REMOVED_METHODS = {
    "create_user",
    "get_user",
    "grant_repository",
    "repository_allowed",
    "audit",
    "list_audit",
    "save_deployment",
    "get_deployment",
    "record_deployment_result",
    "record_shadow_observation",
    "list_release_observations",
    "create_alert",
    "list_alerts",
    "save_installation",
    "installation_tenant",
    "save_skill_artifact",
    "get_active_skill_artifact",
    "list_active_skill_artifacts",
    "list_skill_artifact_versions",
    "activate_skill_artifact",
    "save_skill_evolution_run",
    "list_skill_evolution_runs",
}

CORE_METHODS = {
    "create",
    "transition",
    "succeed",
    "fail",
    "get",
    "list_tasks",
    "dashboard_stats",
    "record_agent_message",
    "save_agent_memory",
    "list_agent_memories",
    "delete_agent_memories",
    "purge_expired_agent_memories",
    "record_failure_case",
    "list_failure_cases",
    "list_task_failure_cases",
    "resolve_failure_cases",
    "save_evaluation_case",
    "list_evaluation_cases",
    "save_evolution_run",
    "list_evolution_runs",
    "save_skill_version",
    "get_active_skill_version",
    "list_skill_versions",
    "activate_skill_version",
    "save_task_payload",
    "update_task_input",
    "get_task_payload",
    "save_checkpoint",
    "load_checkpoints",
    "delete_checkpoint",
    "request_cancel",
    "is_cancelled",
    "cancel",
    "prepare_resume",
    "claim_webhook",
    "complete_webhook",
    "get_webhook",
}


def public_methods(cls):
    return {
        name
        for name, member in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


class InMemoryStoreContractTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryTaskStore()

    def test_public_api_has_no_tenant_or_enterprise_surface(self):
        methods = public_methods(InMemoryTaskStore)

        self.assertTrue(CORE_METHODS.issubset(methods))
        self.assertTrue(REMOVED_METHODS.isdisjoint(methods))
        for name in CORE_METHODS:
            self.assertNotIn(
                "tenant_id",
                inspect.signature(getattr(InMemoryTaskStore, name)).parameters,
            )

    def test_task_trace_checkpoint_cancel_resume_and_success_lifecycle(self):
        self.store.create("task-1", "org/repo", 7, {"source": "api"})
        self.store.save_task_payload("task-1", "diff --git a/a.py b/a.py")
        self.store.update_task_input("task-1", {"risk": "high"})
        self.store.transition(
            "task-1", TraceEvent(1, TaskState.PLANNING, "planned", utc_now())
        )
        self.store.record_agent_message(
            "task-1",
            {
                "sender": "router",
                "recipient": "security-reviewer",
                "kind": "task_assigned",
                "correlation_id": "review-1",
                "content": {"domain": "security"},
            },
        )
        self.store.save_checkpoint(
            "task-1", "review", {"findings": []}, attempt=2
        )

        self.assertTrue(self.store.request_cancel("task-1"))
        self.assertTrue(self.store.is_cancelled("task-1"))
        self.store.cancel(
            "task-1", TraceEvent(2, TaskState.CANCELLED, "cancelled", utc_now())
        )
        self.assertTrue(self.store.prepare_resume("task-1"))

        resumed = self.store.get("task-1")
        self.assertEqual("PENDING", resumed["state"])
        self.assertFalse(resumed["cancel_requested"])
        self.assertIsNone(resumed["error"])
        self.assertEqual("high", resumed["input"]["risk"])
        self.assertEqual("security", resumed["collaboration"][0]["content"]["domain"])
        self.assertEqual(2, self.store.load_checkpoints("task-1")["review"]["attempt"])
        self.store.save_checkpoint("task-1", "loop:A01", {"next_step": 2})
        self.assertTrue(self.store.delete_checkpoint("task-1", "loop:A01"))
        self.assertFalse(self.store.delete_checkpoint("task-1", "loop:A01"))
        self.assertIn("review", self.store.load_checkpoints("task-1"))
        self.assertIn("diff --git", self.store.get_task_payload("task-1"))

        report = ReviewReport(
            "org/repo", 7, "reviewed", "low", reviewer="deepseek-chat"
        )
        self.store.succeed(
            "task-1", report, TraceEvent(3, TaskState.SUCCESS, "done", utc_now())
        )

        task = self.store.get("task-1")
        self.assertEqual("SUCCESS", task["state"])
        self.assertEqual("deepseek-chat", task["report"]["reviewer"])
        self.assertEqual(["task-1"], [item["id"] for item in self.store.list_tasks()])
        self.assertEqual(1, self.store.dashboard_stats()["tasks_success"])
        self.assertFalse(self.store.prepare_resume("task-1"))

    def test_memory_is_repository_scoped_without_tenant_data(self):
        memory = {
            "id": "memory-1",
            "repository": "org/repo",
            "task_id": "task-1",
            "agent": "security-reviewer",
            "scope": "semantic",
            "kind": "review_feedback",
            "content": "confirmed injection finding",
            "keywords": ["injection"],
            "metadata": {"approved": True},
            "importance": 0.9,
            "created_at": utc_now(),
            "expires_at": None,
        }
        saved = self.store.save_agent_memory(memory)

        self.assertNotIn("tenant_id", saved)
        self.assertEqual(
            ["memory-1"],
            [
                item["id"]
                for item in self.store.list_agent_memories(
                    "org/repo", ("semantic",), 10
                )
            ],
        )
        self.assertEqual(
            [], self.store.list_agent_memories("org/other", ("semantic",), 10)
        )
        self.assertEqual(
            1,
            self.store.delete_agent_memories(
                task_id="task-1", scope="semantic"
            ),
        )

        expired = dict(memory)
        expired.update(
            {
                "id": "expired",
                "task_id": "old-task",
                "scope": "working",
                "expires_at": "2000-01-01T00:00:00+00:00",
            }
        )
        self.store.save_agent_memory(expired)
        self.assertEqual(1, self.store.purge_expired_agent_memories())

    def test_feedback_evaluation_and_evolution_records_round_trip(self):
        self.store.create("task-1", "org/repo", 7, {})
        self.store.record_failure_case(
            "task-1", "missed_issue", {"finding": {"rule_id": "SEC-1"}}
        )
        failures = self.store.list_task_failure_cases("task-1")
        self.assertEqual("SEC-1", failures[0]["payload"]["finding"]["rule_id"])
        self.store.resolve_failure_cases([failures[0]["id"]])
        self.assertTrue(self.store.list_failure_cases()[0]["resolved"])

        case = self.store.save_evaluation_case(
            "case-1", "validation", "diff", [{"rule_id": "SEC-1"}]
        )
        self.assertEqual("SEC-1", case["expected"][0]["rule_id"])
        with self.assertRaises(ValueError):
            self.store.save_evaluation_case(
                "case-1", "holdout", "changed", [{"rule_id": "SEC-2"}]
            )

        version = self.store.save_skill_version(
            "llm-review", "review security", 0.8, activate=True
        )
        self.assertEqual(1, version["version"])
        self.assertEqual(
            1, self.store.get_active_skill_version("llm-review")["version"]
        )
        self.assertTrue(self.store.activate_skill_version("llm-review", 1))

        run = {
            "id": "run-1",
            "skill_name": "llm-review",
            "candidate_version": 1,
            "baseline_version": None,
            "decision": "activated",
            "candidate_score": 0.8,
            "baseline_score": 0.7,
            "metrics": {"f1": 0.8},
            "created_at": utc_now(),
        }
        self.store.save_evolution_run(run)
        self.assertEqual(0.8, self.store.list_evolution_runs()[0]["metrics"]["f1"])

    def test_webhook_claim_is_idempotent_without_tenant_data(self):
        self.assertTrue(
            self.store.claim_webhook("delivery-1", "pull_request", "sha-1")
        )
        self.assertFalse(
            self.store.claim_webhook("delivery-1", "pull_request", "sha-1")
        )
        with self.assertRaises(ValueError):
            self.store.claim_webhook("delivery-1", "pull_request", "sha-2")

        self.store.complete_webhook("delivery-1", "task-1")
        delivery = self.store.get_webhook("delivery-1")
        self.assertEqual("task-1", delivery["task_id"])
        self.assertNotIn("tenant_id", delivery)


class PostgresStoreStaticContractTests(unittest.TestCase):
    def test_schema_and_method_signatures_match_product_contract(self):
        schema = "\n".join(SCHEMA_STATEMENTS).lower()
        for table in CORE_TABLES:
            self.assertIn("create table if not exists %s" % table, schema)
        for removed in (
            "tenant_id",
            "users",
            "memberships",
            "repository_grants",
            "audit_log",
            "deployments",
            "release_observations",
            "alerts",
            "installations",
            "review_policy_versions",
            "review_policy_runs",
        ):
            self.assertNotIn(removed, schema)

        postgres_methods = public_methods(PostgresTaskStore)
        self.assertTrue(CORE_METHODS.issubset(postgres_methods))
        self.assertTrue(REMOVED_METHODS.isdisjoint(postgres_methods))
        for name in CORE_METHODS:
            fake_parameters = list(
                inspect.signature(getattr(InMemoryTaskStore, name)).parameters
            )[1:]
            postgres_parameters = list(
                inspect.signature(getattr(PostgresTaskStore, name)).parameters
            )[1:]
            self.assertEqual(fake_parameters, postgres_parameters, name)

    def test_create_store_rejects_non_postgresql_urls(self):
        with self.assertRaisesRegex(ValueError, "PostgreSQL"):
            create_store("")


if __name__ == "__main__":
    unittest.main()
