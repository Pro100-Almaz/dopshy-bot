"""Pytest fixtures for DB-backed tests.

Tests require POSTGRES_DSN to point at a DISPOSABLE database (tables are
truncated between tests). Set it before invoking pytest, e.g.:

    POSTGRES_DSN=postgresql://dopshy:changeme@localhost:55432/dopshy poetry run pytest

If the DSN is unset or unreachable, DB tests are skipped.
"""

import os
import tempfile

# Redirect the SQLite conversation store to a disposable temp file BEFORE config
# is imported. chat.conversation bootstraps this DB at import time, and the real
# ./data dir may be missing or not writable in test runs (e.g. it's root-owned
# when created by the Docker bind-mount). setdefault so an explicit env still wins.
os.environ.setdefault(
    "CONVERSATION_DB_PATH",
    os.path.join(tempfile.gettempdir(), "dopshy_test_conversations.db"),
)

import pytest

import config


@pytest.fixture(scope="session", autouse=True)
def _migrated_schema():
    if not config.POSTGRES_DSN:
        pytest.skip("POSTGRES_DSN not set — skipping DB tests")
    from scripts.migrate import migrate
    try:
        migrate()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"test database unreachable: {exc}")


@pytest.fixture(autouse=True)
def clean_db(_migrated_schema):
    from integrations.repo.postgres import _conn
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE bookings, booking_events, payments, booking_sessions "
                "RESTART IDENTITY CASCADE"
            )
    yield
