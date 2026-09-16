"""DB-backed coverage for the agent-test sandbox purges.

These need a real database because the thing worth protecting is the SQL, not a
Python predicate: `purge_orphans` decides which `bot_paused_contacts` rows to
delete with a Postgres regex, and the failure mode is deleting a REAL customer's
pause row — which silently un-mutes a contact a manager deliberately paused.

Lives outside tests/test_agent_test_console.py because that module is marked
`no_db` wholesale.
"""

import pytest

import config
from integrations.repo import agent_test_repo
from integrations.repo.utils import _conn

# A real Almaty mobile whose digits CONTAIN the console block "700000" at an
# offset. The original predicate was an unanchored LIKE '%700000%', so this
# number was collateral damage of every nightly sweep.
REAL_PHONE = "77017000001"
CONSOLE_PHONE = f"7{config.CONSOLE_TEST_NATIONAL_PREFIX}0001"


@pytest.fixture
def paused_contacts():
    """Pause both numbers, and clean them up afterwards.

    conftest's TRUNCATE does not cover bot_paused_contacts, so this fixture owns
    the rows it creates rather than relying on the global reset.
    """
    def _clear():
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM bot_paused_contacts WHERE phone IN (%s, %s)",
                        (REAL_PHONE, CONSOLE_PHONE))

    _clear()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bot_paused_contacts (phone, paused) VALUES (%s, true), (%s, true)",
            (REAL_PHONE, CONSOLE_PHONE),
        )
    yield
    _clear()


def _paused_phones() -> list[str]:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT phone FROM bot_paused_contacts WHERE phone IN (%s, %s) ORDER BY phone",
            (REAL_PHONE, CONSOLE_PHONE),
        )
        return [row[0] for row in cur.fetchall()]


def test_orphan_purge_spares_a_real_phone_containing_the_console_block(paused_contacts):
    """The regression this file exists for.

    With the old `LIKE '%<prefix>%'` predicate REAL_PHONE was deleted here.
    """
    agent_test_repo.purge_orphans(older_than_days=0)

    assert _paused_phones() == [REAL_PHONE], (
        "purge_orphans deleted a real customer's pause row — the console-phone "
        "predicate is matching on a substring again"
    )


def test_orphan_purge_runs_every_statement_without_error(paused_contacts):
    """No statement may fail its SAVEPOINT.

    `_execute_isolated` swallows a failing statement and records -1, so a purge
    whose SQL has gone stale against the schema still returns successfully. This
    asserts every table was actually swept — the only thing that distinguishes a
    working purge from a silently broken one.
    """
    deleted = agent_test_repo.purge_orphans(older_than_days=0)

    failed = {table: n for table, n in deleted.items() if n < 0}
    assert not failed, f"statements failed against the live schema: {failed}"
    assert deleted["bot_paused_contacts"] == 1


def test_session_purge_runs_every_statement_without_error():
    """Same guarantee for the per-session reset path."""
    deleted = agent_test_repo.purge_test_data(CONSOLE_PHONE)

    failed = {table: n for table, n in deleted.items() if n < 0}
    assert not failed, f"statements failed against the live schema: {failed}"
