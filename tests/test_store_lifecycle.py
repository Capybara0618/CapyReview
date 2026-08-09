import os
import tempfile
import unittest
from pathlib import Path

from capyreview.store import TaskStore


class TaskStoreLifecycleTests(unittest.TestCase):
    def test_each_operation_releases_its_sqlite_connection(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            store = TaskStore(path)
            store.dashboard_stats()

            os.unlink(path)
            self.assertFalse(os.path.exists(path))
        finally:
            if os.path.exists(path):
                os.unlink(path)


class ProductBrandingTests(unittest.TestCase):
    def test_demo_surface_uses_capyreview_core_runtime_branding(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        api = (root / "capyreview" / "api.py").read_text(encoding="utf-8")

        self.assertIn("CapyReview", html)
        self.assertIn("PR REVIEW RUNTIME", html)
        self.assertIn("Risk Router", html)
        self.assertIn("FastAPI HTTP surface", api)

    def test_review_form_has_a_reproducible_input_sample(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="load-demo"', html)
        self.assertIn("demo/api", script)
        self.assertIn("eval(value)", script)

    def test_task_view_exposes_trace_and_evidence_sections(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="task-detail"', html)
        self.assertIn("任务与 Trace", html)
        self.assertIn("function renderTask", script)
        self.assertIn("Run Trace", script)

    def test_demo_navigation_has_evaluation_without_enterprise_controls(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        api = (root / "capyreview" / "api.py").read_text(encoding="utf-8")

        self.assertIn('data-view="evaluation"', html)
        self.assertIn('id="evaluation-summary"', html)
        self.assertNotIn('data-view="skills"', html)
        self.assertNotIn("SECURE CONTROL PLANE", html)
        self.assertIn('/api/evaluation', script)
        self.assertIn('@application.get("/api/evaluation")', api)


if __name__ == "__main__":
    unittest.main()
