"""Shared persistence helpers.

The only production store is :class:`capyreview.postgres_store.PostgresTaskStore`.
Test doubles live under ``tests`` and are never selected by application startup.
"""

from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
