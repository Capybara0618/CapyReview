from pathlib import Path
import inspect
import unittest

from capyreview.task_queue import TaskQueue


ROOT = Path(__file__).resolve().parents[1]


class SlimArchitectureTests(unittest.TestCase):
    def test_enterprise_and_side_product_modules_are_removed(self):
        obsolete = (
            "auth.py",
            "fixer.py",
            "verifier.py",
            "rollout.py",
            "observability.py",
            "metrics.py",
            "skills.py",
            "skill_runner.py",
            "evaluation_benchmark.py",
            "evolution_proof.py",
        )
        remaining = [
            name for name in obsolete if (ROOT / "capyreview" / name).exists()
        ]
        self.assertEqual([], remaining)

    def test_obsolete_entry_points_are_removed(self):
        obsolete = (
            ROOT / "scripts" / "run_e2e_evaluation.py",
            ROOT / "scripts" / "run_prompt_evolution_proof.py",
            ROOT / "scripts" / "render_knowledge_base_pdf.py",
            ROOT / "scripts" / "import_github_pr_dataset.py",
            ROOT / "web" / "login.css",
        )
        remaining = [str(path.relative_to(ROOT)) for path in obsolete if path.exists()]
        self.assertEqual([], remaining)

    def test_core_service_has_no_enterprise_dependencies(self):
        source = (ROOT / "capyreview" / "service.py").read_text(encoding="utf-8")
        for forbidden in (
            ".auth",
            ".fixer",
            ".verifier",
            ".rollout",
            ".observability",
            ".metrics",
            ".skills",
            "GitHubAppAuthenticator",
            "tenant_id",
            "create_fix",
            "_run_shadow",
        ):
            self.assertNotIn(forbidden, source)

    def test_queue_keeps_delivery_reliability_without_concurrency_control_plane(self):
        parameters = inspect.signature(TaskQueue.__init__).parameters
        self.assertNotIn("workers", parameters)
        self.assertFalse(hasattr(TaskQueue, "DLQ"))
        self.assertFalse(hasattr(TaskQueue, "dead_letters"))
        self.assertFalse(hasattr(TaskQueue, "replay_dead_letter"))


if __name__ == "__main__":
    unittest.main()
