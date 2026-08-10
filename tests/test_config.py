import os
import tempfile
import unittest
from unittest.mock import patch

from capyreview.config import Settings, load_dotenv


class DotenvTests(unittest.TestCase):
    def test_loads_valid_assignments_and_quoted_values(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write("# comment\n")
            handle.write("export DEEPSEEK_MODEL=deepseek-chat\n")
            handle.write('DEEPSEEK_API_KEY="test-key"\n')
            handle.write("invalid line\n")
            path = handle.name
        try:
            with patch.dict(os.environ, {}, clear=True):
                load_dotenv([path])
                self.assertEqual("deepseek-chat", os.environ["DEEPSEEK_MODEL"])
                self.assertEqual("test-key", os.environ["DEEPSEEK_API_KEY"])
        finally:
            os.unlink(path)

    def test_process_environment_has_priority(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write("DEEPSEEK_MODEL=deepseek-chat\n")
            path = handle.name
        try:
            with patch.dict(os.environ, {"DEEPSEEK_MODEL": "deepseek-reasoner"}, clear=True):
                load_dotenv([path])
                self.assertEqual("deepseek-reasoner", os.environ["DEEPSEEK_MODEL"])
        finally:
            os.unlink(path)


class DeepSeekSettingsTests(unittest.TestCase):
    def test_resolved_llm_is_fixed_to_the_official_deepseek_endpoint(self):
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_MODEL": "deepseek-reasoner",
                "CAPYREVIEW_LLM_PROVIDER": "local",
                "CAPYREVIEW_LLM_BASE_URL": "https://untrusted.example/v1",
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual("test-key", settings.deepseek_api_key)
        self.assertEqual("deepseek-reasoner", settings.deepseek_model)
        self.assertEqual(
            {
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com",
                "api_key": "test-key",
                "model": "deepseek-reasoner",
                "headers": {},
            },
            settings.resolved_llm(),
        )

    def test_deepseek_model_defaults_to_official_chat_model(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            settings = Settings.from_env()

        self.assertEqual("deepseek-chat", settings.deepseek_model)

    def test_legacy_deepseek_names_are_non_sensitive_fallbacks(self):
        with patch.dict(
            os.environ,
            {
                "CAPYREVIEW_DEEPSEEK_API_KEY": "legacy-key",
                "CAPYREVIEW_LLM_MODEL": "legacy-model",
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual("legacy-key", settings.deepseek_api_key)
        self.assertEqual("legacy-model", settings.deepseek_model)

    def test_standard_deepseek_names_take_priority_over_legacy_names(self):
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "standard-key",
                "DEEPSEEK_MODEL": "standard-model",
                "CAPYREVIEW_DEEPSEEK_API_KEY": "legacy-key",
                "CAPYREVIEW_LLM_MODEL": "legacy-model",
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual("standard-key", settings.deepseek_api_key)
        self.assertEqual("standard-model", settings.deepseek_model)

    def test_missing_deepseek_key_is_an_explicit_configuration_error(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()

        with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
            settings.resolved_llm()

    def test_removed_enterprise_and_provider_fields_are_not_part_of_settings(self):
        removed = {
            "auth_required", "auth_secret", "bootstrap_admin_username",
            "bootstrap_admin_password", "default_tenant_id", "session_ttl_seconds",
            "llm_provider", "llm_base_url", "llm_api_key", "llm_model",
            "openrouter_api_key", "openrouter_site_url", "openrouter_app_name",
            "skills_dir", "skill_timeout_seconds", "skill_memory_mb", "skill_sandbox",
            "skill_signing_key", "skill_container_image", "repair_test_command",
            "repair_verify_timeout_seconds", "otel_endpoint", "otel_service_name",
            "alert_failure_rate", "alert_min_samples", "alert_window_seconds",
            "alert_webhook_url", "alert_smtp_host", "alert_email_to",
            "github_app_id", "github_app_slug", "github_private_key_path",
            "public_base_url", "continuous_eval_seconds", "async_workers",
            "db_path",
        }

        self.assertTrue(removed.isdisjoint(Settings.__dataclass_fields__))
