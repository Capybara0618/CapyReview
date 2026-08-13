import unittest

from capyreview.config import Settings
from capyreview.models import Finding, Severity
from capyreview.service import ReviewService
from tests.fakes import InMemoryTaskStore


DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"


def skill_package(marker):
    return {
        "name": "review-auth-security",
        "skill_md": """---
name: review-auth-security
description: Review auth and eval trust-boundary defects from confirmed feedback.
metadata:
  capyreview-domains: security
  capyreview-signals: eval auth token
---

# %s

Require exact changed-line evidence before reporting a concrete defect.
""" % marker,
        "references": {},
    }


class FakeCoordinator:
    name = "fake-llm-coordinator"

    def review_with_context(
        self, task_id, _diff, parsed, repository="", head_commit="",
        pull_request=None,
    ):
        line = parsed.added_lines[0]
        return [Finding(
            "SEC-EVAL", Severity.CRITICAL, "Dynamic execution",
            "Untrusted input reaches eval.", line.path, line.line, line.content,
            "Replace eval with a typed parser.", "Add a malicious-input test.", 0.95,
        )]

    def collaboration_summary(self, _task_id):
        return {
            "protocol": "route-review-evidence-judge",
            "messages": 4,
            "approved_findings": 1,
            "rejected_findings": 0,
        }


class CapturingQueue:
    backend = "test-queue"

    def __init__(self):
        self.messages = []

    def submit(self, payload, message_id=""):
        self.messages.append((message_id, dict(payload)))
        return message_id

    def close(self):
        return None


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            max_diff_bytes=10_000,
            timeout_seconds=10,
        )

    def service(self, reviewer=None, queue=None):
        return ReviewService(
            self.settings,
            reviewer=reviewer,
            store=InMemoryTaskStore(),
            queue=queue or CapturingQueue(),
        )

    def test_fake_llm_end_to_end_review(self):
        service = self.service(reviewer=FakeCoordinator())
        try:
            result = service.create_review("org/repo", DIFF, 1)
        finally:
            service.close()

        self.assertEqual("SUCCESS", result["state"])
        self.assertEqual("SEC-EVAL", result["report"]["findings"][0]["rule_id"])
        self.assertEqual(
            "route-review-evidence-judge",
            result["report"]["collaboration"]["protocol"],
        )

    def test_server_can_initialize_without_key_but_review_fails_clearly(self):
        service = self.service()
        try:
            with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
                service.create_review("org/repo", DIFF, 1)
        finally:
            service.close()

    def test_active_evolved_skill_package_is_available_to_the_runtime_registry(self):
        settings = Settings(
            deepseek_api_key="test-key",
            deepseek_model="test-model",
        )
        service = ReviewService(
            settings, store=InMemoryTaskStore(), queue=CapturingQueue()
        )
        try:
            service.store.save_skill_version(
                "review-auth-security", skill_package("Evolved Auth Review"),
                0.8,
                activate=True,
            )
            service._ensure_harness()
            activated = service.reviewer.skill_registry.activate(
                "review-auth-security"
            )
        finally:
            service.close()

        self.assertEqual(1, activated.version)
        self.assertIn("# Evolved Auth Review", activated.body)

    def test_queued_task_keeps_the_model_and_skill_versions_from_creation(self):
        settings = Settings(
            deepseek_api_key="test-key",
            deepseek_model="deepseek-chat-test",
        )
        queue = CapturingQueue()
        service = ReviewService(
            settings, store=InMemoryTaskStore(), queue=queue
        )
        calls = []

        class SkillCoordinator(FakeCoordinator):
            def __init__(self, versions, model):
                self.versions = versions
                self.model = model
                self.name = "skills-%s:%s" % (versions, model)

            def review_with_context(self, *args, **kwargs):
                calls.append((self.versions, self.model))
                return super().review_with_context(*args, **kwargs)

        service._build_coordinator = lambda skill_packages=(), model="": SkillCoordinator(
            {item["name"]: item["version"] for item in skill_packages}, model
        )
        try:
            version_one = service.store.save_skill_version(
                "review-auth-security", skill_package("Version One"),
                0.8, activate=True,
            )
            pending = service.enqueue_review("org/repo", DIFF, 1)
            service.store.save_skill_version(
                "review-auth-security", skill_package("Version Two"),
                0.9, activate=True,
            )
            message = queue.messages[0][1]
            service._process_queued(message)
            task = service.store.get(pending["task_id"])
        finally:
            service.close()

        self.assertEqual(
            {"review-auth-security": version_one["version"]},
            task["input"]["skill_versions"],
        )
        self.assertEqual("deepseek-chat-test", task["input"]["model"])
        self.assertEqual([(
            {"review-auth-security": version_one["version"]},
            "deepseek-chat-test",
        )], calls)

    def test_rejects_large_diff_before_creating_a_task(self):
        service = self.service(reviewer=FakeCoordinator())
        try:
            with self.assertRaises(ValueError):
                service.create_review("org/repo", "x" * 10_001)
            self.assertEqual([], service.store.list_tasks())
        finally:
            service.close()

    def test_feedback_is_persisted_without_tenant_or_repair_categories(self):
        service = self.service(reviewer=FakeCoordinator())
        try:
            result = service.create_review("org/repo", DIFF, 1)
            feedback = service.record_feedback(
                result["task_id"], "false_positive",
                result["report"]["findings"][0], "不是实际风险",
            )
            cases = service.store.list_task_failure_cases(result["task_id"])
            service.record_feedback(
                result["task_id"], "accepted",
                result["report"]["findings"][0], "confirmed issue",
            )
            cases_after_accept = service.store.list_task_failure_cases(
                result["task_id"]
            )
            with self.assertRaises(ValueError):
                service.record_feedback(result["task_id"], "bad_fix", None, "obsolete")
        finally:
            service.close()

        self.assertEqual({"recorded": True, "category": "false_positive"}, feedback)
        self.assertEqual(1, len(cases))
        self.assertEqual(1, len(cases_after_accept))
        self.assertEqual("SEC-EVAL", cases[0]["payload"]["finding"]["rule_id"])

    def test_webhook_is_idempotent_and_uses_the_same_core_queue(self):
        queue = CapturingQueue()
        service = ReviewService(
            self.settings, reviewer=FakeCoordinator(),
            store=InMemoryTaskStore(), queue=queue,
        )
        payload = {
            "action": "opened",
            "number": 7,
            "repository": {"full_name": "org/repo"},
            "pull_request": {
                "diff_url": "https://api.github.test/pulls/7.diff",
                "issue_url": "https://api.github.test/issues/7",
                "head": {"sha": "abc123"},
            },
        }
        try:
            first = service.handle_github_pull_request(payload, "delivery-1", "sha")
            second = service.handle_github_pull_request(payload, "delivery-1", "sha")
        finally:
            service.close()

        self.assertEqual(first["task_id"], second["task_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(1, len(queue.messages))
        task = service.store.get(first["task_id"])
        self.assertEqual("abc123", task["input"]["head_commit"])
        self.assertEqual("abc123", queue.messages[0][1]["head_commit"])

    def test_cancelled_task_can_be_prepared_and_requeued_for_resume(self):
        queue = CapturingQueue()
        service = ReviewService(
            self.settings, reviewer=FakeCoordinator(),
            store=InMemoryTaskStore(), queue=queue,
        )
        try:
            pending = service.enqueue_review("org/repo", DIFF, 1)
            self.assertTrue(service.cancel_task(pending["task_id"]))
            resumed = service.resume_task(pending["task_id"])
            self.assertFalse(service.store.is_cancelled(pending["task_id"]))
        finally:
            service.close()

        self.assertEqual("PENDING", resumed["state"])
        self.assertTrue(resumed["resumed"])
        self.assertEqual(2, len(queue.messages))

    def test_complete_failure_batch_schedules_skill_evolution_on_the_same_queue(self):
        queue = CapturingQueue()
        service = ReviewService(
            self.settings, reviewer=FakeCoordinator(),
            store=InMemoryTaskStore(), queue=queue,
        )
        calls = []
        service.evolution.auto_propose = lambda name: calls.append(name) or {
            "decision": "deferred"
        }
        try:
            for index in range(3):
                task_id = "failure-%s" % index
                service.store.create(task_id, "org/repo", index, {})
                service.store.record_failure_case(
                    task_id, "judge_rejected", {"reason": "confirmed"}
                )
            service._schedule_skill_evolution()
            message = queue.messages[0][1]
            service._process_queued(message)
        finally:
            service.close()

        self.assertEqual("skill-evolution", message["kind"])
        self.assertEqual(["review-evolved-patterns"], calls)


if __name__ == "__main__":
    unittest.main()
