import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, Optional


_DOTENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_dotenv(paths: Optional[Iterable[str]] = None) -> None:
    """Load local dotenv files without overriding real process environment values.

    The project-root file has priority over ``capyreview/.env``.  This allows the
    latter to remain compatible with existing local setups while keeping the
    conventional root-level ``.env`` as the recommended location.
    """
    package_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(package_dir)
    candidates = list(paths) if paths is not None else [
        os.path.join(project_root, ".env"),
        os.path.join(package_dir, ".env"),
    ]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if not _DOTENV_KEY.fullmatch(key):
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)


load_dotenv()


def _int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError("%s must be positive" % name)
    return value


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _non_negative_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 0:
        raise ValueError("%s must be non-negative" % name)
    return value


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8080
    max_diff_bytes: int = 1024 * 1024
    max_steps: int = 8
    timeout_seconds: int = 120
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    github_webhook_secret: str = ""
    github_token: str = ""
    auto_post_review: bool = False
    database_url: str = ""
    redis_url: str = ""
    agent_max_workers: int = 2
    agent_retries: int = 1
    agent_loop_max_steps: int = 4
    agent_loop_timeout_seconds: int = 45
    context_max_tokens: int = 12000
    context_reserved_tokens: int = 2500
    memory_enabled: bool = True
    memory_recall_limit: int = 6
    memory_working_ttl_seconds: int = 86400
    eval_max_cases: int = 5
    eval_min_cases: int = 3
    eval_min_improvement: float = 0.01
    eval_min_holdout_cases: int = 2
    eval_max_metric_regression: float = 0.0
    webhook_max_age_seconds: int = 600
    queue_max_attempts: int = 3
    queue_lease_seconds: int = 60

    def resolved_llm(self) -> Dict[str, object]:
        """Return the single supported official DeepSeek transport."""
        if not self.deepseek_api_key.strip():
            raise ValueError(
                "DeepSeek is not configured: set DEEPSEEK_API_KEY in the project .env"
            )
        return {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "api_key": self.deepseek_api_key.strip(),
            "model": self.deepseek_model.strip() or "deepseek-chat",
            "headers": {},
        }

    def validate_infrastructure(self) -> None:
        if not self.database_url.startswith(("postgres://", "postgresql://")):
            raise ValueError(
                "CAPYREVIEW_DATABASE_URL must be a PostgreSQL connection URL"
            )
        if not self.redis_url.startswith(("redis://", "rediss://")):
            raise ValueError("CAPYREVIEW_REDIS_URL must be a Redis connection URL")

    def validate_evolution(self) -> None:
        if self.eval_min_cases > self.eval_max_cases:
            raise ValueError("CAPYREVIEW_EVAL_MIN_CASES cannot exceed CAPYREVIEW_EVAL_MAX_CASES")
        if not 0.0 <= self.eval_min_improvement <= 1.0:
            raise ValueError("CAPYREVIEW_EVAL_MIN_IMPROVEMENT must be between 0 and 1")
        if self.eval_min_holdout_cases > self.eval_max_cases:
            raise ValueError("CAPYREVIEW_EVAL_MIN_HOLDOUT_CASES cannot exceed CAPYREVIEW_EVAL_MAX_CASES")
        if not 0.0 <= self.eval_max_metric_regression <= 1.0:
            raise ValueError("CAPYREVIEW_EVAL_MAX_METRIC_REGRESSION must be between 0 and 1")
        if self.agent_max_workers < 1:
            raise ValueError("CAPYREVIEW_AGENT_MAX_WORKERS must be at least 1")
        if self.agent_retries < 0:
            raise ValueError("CAPYREVIEW_AGENT_RETRIES cannot be negative")
        if self.agent_loop_max_steps < 1:
            raise ValueError("CAPYREVIEW_AGENT_LOOP_MAX_STEPS must be at least 1")
        if self.context_max_tokens < 512:
            raise ValueError("CAPYREVIEW_CONTEXT_MAX_TOKENS must be at least 512")
        if not 0 <= self.context_reserved_tokens < self.context_max_tokens:
            raise ValueError(
                "CAPYREVIEW_CONTEXT_RESERVED_TOKENS must be smaller than the context budget"
            )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("CAPYREVIEW_HOST", "127.0.0.1"),
            port=_int("CAPYREVIEW_PORT", 8080),
            max_diff_bytes=_int("CAPYREVIEW_MAX_DIFF_BYTES", 1024 * 1024),
            max_steps=_int("CAPYREVIEW_MAX_STEPS", 8),
            timeout_seconds=_int("CAPYREVIEW_TIMEOUT_SECONDS", 120),
            deepseek_api_key=(
                os.getenv("DEEPSEEK_API_KEY")
                or os.getenv("CAPYREVIEW_DEEPSEEK_API_KEY", "")
            ),
            deepseek_model=(
                os.getenv("DEEPSEEK_MODEL")
                or os.getenv("CAPYREVIEW_LLM_MODEL", "deepseek-chat")
            ),
            github_webhook_secret=os.getenv("CAPYREVIEW_GITHUB_WEBHOOK_SECRET", ""),
            github_token=os.getenv("CAPYREVIEW_GITHUB_TOKEN", ""),
            auto_post_review=_bool("CAPYREVIEW_AUTO_POST_REVIEW"),
            database_url=os.getenv("CAPYREVIEW_DATABASE_URL", ""),
            redis_url=os.getenv("CAPYREVIEW_REDIS_URL", ""),
            agent_max_workers=_int("CAPYREVIEW_AGENT_MAX_WORKERS", 2),
            agent_retries=_non_negative_int("CAPYREVIEW_AGENT_RETRIES", 1),
            agent_loop_max_steps=_int("CAPYREVIEW_AGENT_LOOP_MAX_STEPS", 4),
            agent_loop_timeout_seconds=_int("CAPYREVIEW_AGENT_LOOP_TIMEOUT_SECONDS", 45),
            context_max_tokens=_int("CAPYREVIEW_CONTEXT_MAX_TOKENS", 12000),
            context_reserved_tokens=_non_negative_int(
                "CAPYREVIEW_CONTEXT_RESERVED_TOKENS", 2500
            ),
            memory_enabled=_bool("CAPYREVIEW_MEMORY_ENABLED", True),
            memory_recall_limit=_int("CAPYREVIEW_MEMORY_RECALL_LIMIT", 6),
            memory_working_ttl_seconds=_int(
                "CAPYREVIEW_MEMORY_WORKING_TTL_SECONDS", 86400
            ),
            eval_max_cases=_int("CAPYREVIEW_EVAL_MAX_CASES", 5),
            eval_min_cases=_int("CAPYREVIEW_EVAL_MIN_CASES", 3),
            eval_min_improvement=float(os.getenv("CAPYREVIEW_EVAL_MIN_IMPROVEMENT", "0.01")),
            eval_min_holdout_cases=_non_negative_int("CAPYREVIEW_EVAL_MIN_HOLDOUT_CASES", 2),
            eval_max_metric_regression=float(
                os.getenv("CAPYREVIEW_EVAL_MAX_METRIC_REGRESSION", "0")
            ),
            webhook_max_age_seconds=_int("CAPYREVIEW_WEBHOOK_MAX_AGE_SECONDS", 600),
            queue_max_attempts=_int("CAPYREVIEW_QUEUE_MAX_ATTEMPTS", 3),
            queue_lease_seconds=_int("CAPYREVIEW_QUEUE_LEASE_SECONDS", 60),
        )
