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


def pytest_configure(config):
    config.addinivalue_line("markers", "no_db: test does not need PostgreSQL")


@pytest.fixture(scope="session")
def _migrated_schema():
    if not config.POSTGRES_DSN:
        pytest.skip("POSTGRES_DSN not set — skipping DB tests")
    from scripts.migrate import migrate
    try:
        migrate()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"test database unreachable: {exc}")


@pytest.fixture(autouse=True)
def clean_rate_limit():
    """Forget the manager API's per-IP request counter between tests.

    It is a process-global dict with a 60-second window, so without this the
    calls of one test file count against the next one — adding a test anywhere
    can push an unrelated file over the limit and turn its assertions into
    unexplained 429s.
    """
    from blueprints.manager_api import _rate_hits
    _rate_hits.clear()
    yield


@pytest.fixture(autouse=True)
def clean_db(request):
    if request.node.get_closest_marker("no_db"):
        yield
        return

    request.getfixturevalue("_migrated_schema")
    from integrations.repo.postgres import _conn
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE contracts, contract_bookings, bookings, booking_events, payments, booking_sessions, "
                "apipay_invoices RESTART IDENTITY CASCADE"
            )
    yield
