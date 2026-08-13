import hashlib
import hmac
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from capyreview.api import create_app


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeStore:
    def __init__(self):
        self.task = {
            "id": "11111111-1111-1111-1111-111111111111",
            "state": "SUCCESS",
            "repository": "demo/api",
            "pull_request": 7,
            "trace": [{"step": 1, "state": "PLANNING", "message": "planned"}],
            "collaboration": [],
            "report": {
                "repository": "demo/api",
                "pull_request": 7,
                "risk": "low",
                "reviewer": "risk-routed-multi-agent-review",
                "summary": "No issue",
                "findings": [],
            },
        }

    def dashboard_stats(self):
        return {"tasks_total": 1, "tasks_success": 1, "tasks_failed": 0}

    def list_tasks(self, limit=50):
        return [self.task][:limit]

    def get(self, task_id):
        return self.task if task_id == self.task["id"] else None

    def list_task_failure_cases(self, task_id):
        return [{"task_id": task_id, "category": "accepted", "payload": {}}]

    def list_failure_cases(self, resolved_only=False, limit=100):
        return [{"task_id": self.task["id"], "category": "accepted", "resolved": False}][:limit]

    def list_evaluation_cases(self, split="validation", active_only=True, limit=100):
        return [{"name": "case-1", "split": split}][:limit]

    def list_evolution_runs(self, limit=50):
        return [{"id": "prompt-run", "decision": "activated"}][:limit]

    def list_skill_versions(self, skill_name):
        return [{"skill_name": skill_name, "version": 1, "active": True}]


class FakeEvolution:
    def status(self):
        return {"ready": True, "validation_cases": 3, "holdout_cases": 2}

    def auto_propose(self, skill_name):
        return {"decision": "activated", "skill_name": skill_name}

    def propose(self, skill_name, package, regression_score=None):
        return {"decision": "activated", "skill_name": skill_name, "package": package}

    def rollback(self, skill_name, version):
        return skill_name == "review-auth-security" and version == 1


class FakeService:
    def __init__(self):
        self.store = FakeStore()
        self.queue = SimpleNamespace(backend="redis-streams", close=lambda: None)
        self.reviewer = SimpleNamespace(name="risk-routed-multi-agent-review")
        self.harness = SimpleNamespace(name="capyreview-runtime")
        self.llm_config = {"provider": "deepseek", "model": "deepseek-v4-flash"}
        self.evolution = FakeEvolution()
        self.calls = []

    def create_review(self, repository, diff, pull_request=None):
        self.calls.append(("review", repository, diff, pull_request))
        return {"task_id": self.store.task["id"], "state": "SUCCESS", "report": self.store.task["report"]}

    def enqueue_review(self, repository, diff, pull_request=None):
        self.calls.append(("enqueue", repository, diff, pull_request))
        return {"task_id": self.store.task["id"], "state": "PENDING", "queue": self.queue.backend}

    def cancel_task(self, task_id):
        self.calls.append(("cancel", task_id))
        return task_id == self.store.task["id"]

    def resume_task(self, task_id):
        self.calls.append(("resume", task_id))
        return {"task_id": task_id, "state": "PENDING", "resumed": True}

    def record_feedback(self, task_id, category, finding, note):
        self.calls.append(("feedback", task_id, category, finding, note))
        return {"recorded": True, "category": category}

    def handle_github_pull_request(self, payload, delivery_id, payload_sha256):
        self.calls.append(("webhook", payload, delivery_id, payload_sha256))
        return {"task_id": self.store.task["id"], "state": "PENDING"}


class FastApiContractTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            max_diff_bytes=1024 * 1024,
            github_webhook_secret="webhook-secret",
            webhook_max_age_seconds=10**9,
            deepseek_api_key="test-key",
            llm_model="deepseek-v4-flash",
            host="127.0.0.1",
            port=8080,
        )
        self.service = FakeService()
        self.app = create_app(
            settings=self.settings,
            service=self.service,
            evaluation_provider=lambda: {
                "dataset": {"cases": 100, "risk_cases": 40, "clean_cases": 60},
                "result": {"f1": 0.825},
                "limitations": [],
            },
        )
        self.client = TestClient(self.app)

    def test_app_is_fastapi_and_exposes_only_the_core_surface(self):
        self.assertIsInstance(self.app, FastAPI)
        paths = set(self.app.openapi()["paths"])
        for path in {
            "/health", "/api/dashboard", "/api/tasks", "/api/evaluation",
            "/v1/reviews", "/v1/tasks/{task_id}",
            "/v1/tasks/{task_id}/cancel", "/v1/tasks/{task_id}/resume",
            "/v1/tasks/{task_id}/feedback", "/webhooks/github",
            "/v1/evolution/status", "/v1/evolution/auto",
            "/v1/skills/{skill_name}/versions",
        }:
            self.assertIn(path, paths)
        for removed in {
            "/v1/auth/login", "/v1/tasks/{task_id}/fix",
            "/v1/deployments/llm-review", "/api/alerts", "/api/audit",
            "/v1/queue/dead-letters/replay", "/github/install", "/v1/skills/reload",
            "/v1/skill-evolution/status", "/v1/skill-evolution/propose",
        }:
            self.assertNotIn(removed, paths)

    def test_health_dashboard_and_evolution_work_before_lazy_runtime_is_built(self):
        self.service.reviewer = None
        self.service.harness = None
        del self.service.llm_config

        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["reviewer"], "not-initialized")
        self.assertEqual(health.json()["runtime"], "capyreview-runtime")
        self.assertEqual(health.json()["database"], "postgresql")
        self.assertEqual(health.json()["queue"], "redis-streams")

        dashboard = self.client.get("/api/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertTrue(dashboard.json()["llm"]["enabled"])
        self.assertEqual(dashboard.json()["llm"]["model"], "deepseek-v4-flash")

        evolution = self.client.get("/v1/evolution/status")
        self.assertEqual(evolution.status_code, 200)
        self.assertEqual(evolution.json()["provider"], "deepseek")

    def test_missing_deepseek_configuration_is_service_unavailable(self):
        def missing_key(*_args, **_kwargs):
            raise ValueError(
                "DeepSeek is not configured: set DEEPSEEK_API_KEY in the project .env"
            )

        self.service.create_review = missing_key
        response = self.client.post("/v1/reviews", json={
            "repository": "demo/api",
            "diff": "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+value = 1\n",
        })
        self.assertEqual(response.status_code, 503)
        self.assertIn("DEEPSEEK_API_KEY", response.json()["detail"])

    def test_default_evaluation_reads_latest_complete_llm_report(self):
        with tempfile.TemporaryDirectory() as directory:
            older = os.path.join(directory, "20260101T000000Z")
            latest = os.path.join(directory, "20260102T000000Z")
            os.makedirs(older)
            os.makedirs(latest)
            with open(
                os.path.join(older, "llm-evaluation-report.json"),
                "w", encoding="utf-8",
            ) as handle:
                json.dump({"incomplete": True}, handle)
            report = {
                "schema_version": 2,
                "evaluation": {
                    "model": "deepseek-v4-flash", "dataset_cases": 100,
                    "risk_cases": 40, "clean_cases": 60,
                },
                "result": {
                    "dataset": {
                        "cases": 100, "risk_cases": 40, "clean_cases": 60,
                        "repositories": 10,
                    },
                    "metrics": {
                        "cases": 100, "f1": 0.825,
                        "high_risk_recall": 0.947, "clean_accuracy": 0.917,
                    },
                },
                "limitations": ["controlled synthetic diffs"],
            }
            report_path = os.path.join(latest, "llm-evaluation-report.json")
            with open(report_path, "w", encoding="utf-8") as handle:
                json.dump(report, handle)

            app = create_app(settings=self.settings, service=self.service)
            with patch("capyreview.api.EVALUATION_OUTPUT_ROOT", directory):
                response = TestClient(app).get("/api/evaluation")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "complete")
        self.assertEqual(response.json()["result"]["f1"], 0.825)
        self.assertEqual(response.json()["report_id"], "20260102T000000Z")

    def test_default_evaluation_is_explicit_when_no_run_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(settings=self.settings, service=self.service)
            with patch("capyreview.api.EVALUATION_OUTPUT_ROOT", directory):
                response = TestClient(app).get("/api/evaluation")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "not_run")
        self.assertIn("cases", response.json()["dataset"])

    def test_review_task_cancel_resume_feedback_and_report_flow(self):
        response = self.client.post("/v1/reviews", json={
            "repository": "demo/api",
            "pull_request": 7,
            "diff": "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+value = 1\n",
        })
        self.assertEqual(response.status_code, 201)
        task_id = response.json()["task_id"]

        self.assertEqual(self.client.get("/api/tasks").status_code, 200)
        self.assertEqual(self.client.get(f"/v1/tasks/{task_id}").json()["state"], "SUCCESS")
        report = self.client.get(f"/v1/tasks/{task_id}/report")
        self.assertEqual(report.status_code, 200)
        self.assertIn("CapyReview PR Review", report.text)

        feedback = self.client.post(f"/v1/tasks/{task_id}/feedback", json={
            "category": "accepted", "finding": None, "note": "confirmed",
        })
        self.assertEqual(feedback.status_code, 201)
        self.assertEqual(
            self.client.get(f"/v1/tasks/{task_id}/feedback").json()["cases"][0]["category"],
            "accepted",
        )
        self.assertEqual(self.client.post(f"/v1/tasks/{task_id}/cancel", json={}).status_code, 202)
        self.assertEqual(self.client.post(f"/v1/tasks/{task_id}/resume", json={}).status_code, 202)

    def test_async_review_and_signed_github_webhook(self):
        async_response = self.client.post("/v1/reviews?async=true", json={
            "repository": "demo/api",
            "diff": "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+value = 1\n",
        })
        self.assertEqual(async_response.status_code, 202)
        self.assertEqual(async_response.json()["queue"], "redis-streams")

        body = b'{"action":"opened","pull_request":{},"repository":{}}'
        signature = "sha256=" + hmac.new(
            self.settings.github_webhook_secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        response = self.client.post(
            "/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-1",
                "X-Hub-Signature-256": signature,
            },
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["state"], "PENDING")
        self.assertTrue(any(call[0] == "webhook" for call in self.service.calls))

        rejected = self.client.post(
            "/webhooks/github", content=body,
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=bad"},
        )
        self.assertEqual(rejected.status_code, 401)

    def test_evolution_versions_and_evaluation_are_available(self):
        self.assertTrue(self.client.get("/v1/evolution/status").json()["ready"])
        proposed = self.client.post(
            "/v1/evolution/auto",
            json={"skill_name": "review-auth-security"},
        )
        self.assertEqual(proposed.status_code, 201)
        self.assertEqual(proposed.json()["decision"], "activated")
        rollback = self.client.post(
            "/v1/skills/review-auth-security/versions/1/activate", json={}
        )
        self.assertEqual(rollback.status_code, 200)

        versions = self.client.get(
            "/v1/skills/review-auth-security/versions"
        )
        self.assertEqual(versions.json()["versions"][0]["version"], 1)
        self.assertEqual(self.client.get("/api/evaluation").status_code, 200)

    def test_root_serves_the_five_view_core_console(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("CapyReview", response.text)
        for view in ("overview", "review", "tasks", "evolution", "evaluation"):
            self.assertIn(f'id="view-{view}"', response.text)
        for removed in ("login-overlay", "create-fix", 'id="view-skills"'):
            self.assertNotIn(removed, response.text)

    def test_core_console_uses_version_rollback_without_removed_enterprise_calls(self):
        with open(os.path.join(ROOT, "web", "app.js"), "r", encoding="utf-8") as handle:
            script = handle.read()
        self.assertIn("/v1/skills/review-auth-security/versions", script)
        self.assertIn("data-activate-version", script)
        self.assertIn('data.status === "not_run"', script)
        self.assertIn("data.result", script)
        for removed in (
            "/v1/auth/login", "/fix", "/v1/deployments/", "/api/alerts",
            "/api/audit", "/v1/queue/dead-letters", "/v1/skills/reload",
        ):
            self.assertNotIn(removed, script)


if __name__ == "__main__":
    unittest.main()
