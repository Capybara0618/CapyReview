import unittest
from pathlib import Path

from capyreview.config import Settings
from capyreview.postgres_store import PostgresTaskStore, create_store
from capyreview.task_queue import TaskQueue


ROOT = Path(__file__).resolve().parents[1]


class ProductionInfrastructureContractTests(unittest.TestCase):
    def test_settings_require_postgresql_and_redis_urls(self):
        settings = Settings(database_url="", redis_url="")

        with self.assertRaisesRegex(ValueError, "CAPYREVIEW_DATABASE_URL"):
            settings.validate_infrastructure()

        settings = Settings(
            database_url="postgresql://user:pass@localhost/capyreview",
            redis_url="",
        )
        with self.assertRaisesRegex(ValueError, "CAPYREVIEW_REDIS_URL"):
            settings.validate_infrastructure()

    def test_store_factory_accepts_only_postgresql(self):
        with self.assertRaisesRegex(ValueError, "PostgreSQL"):
            create_store("")
        with self.assertRaisesRegex(ValueError, "PostgreSQL"):
            create_store("sqlite:///capyreview.db")

        store = object.__new__(PostgresTaskStore)
        self.assertIsInstance(store, PostgresTaskStore)

    def test_queue_has_no_in_process_fallback(self):
        with self.assertRaisesRegex(ValueError, "CAPYREVIEW_REDIS_URL"):
            TaskQueue(lambda payload: None, redis_url="")

    def test_production_source_has_no_sqlite_dependency(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "capyreview").glob("*.py")
        ).lower()

        self.assertNotIn("import sqlite3", source)
        self.assertNotIn("capyreview_db_path", source)
        self.assertNotIn("memory-acked", source)


if __name__ == "__main__":
    unittest.main()
