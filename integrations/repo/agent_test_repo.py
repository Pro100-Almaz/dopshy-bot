"""Storage for agent-test console sessions.

Session metadata lives in PostgreSQL rather than the SQLite conversation DB
because gunicorn runs several workers: the sessions must be visible to whichever
worker handles the next turn. The conversation *history* itself still lives in
`chat/conversation.py` — the console deliberately uses the pipeline's own
storage rather than a parallel transcript, so what the tester sees is exactly
what the agent sees.
"""

import logging
import re

import psycopg2
import psycopg2.extras

import config
from integrations.repo.utils import _conn

logger = logging.getLogger(__name__)

# Every bot the console can drive. Deliberately not derived from
# config.BOT_CONFIGS: a name stays valid (and gets its own "not configured on
# this server" error from phone_number_id_for) even when its env vars are unset.
BOT_NAMES = ("dopsy_bot", "dopsy_fs_school", "dopsy_boxing")


def phone_number_id_for(bot_name: str) -> str | None:
    for phone_number_id, cfg in config.BOT_CONFIGS.items():
        if cfg["name"] == bot_name:
            return phone_number_id
    return None


# What counts as a console phone. Keyed on the NATIONAL part because
# apipay_client.normalize_phone rewrites a leading 7 to 8, so this matches
# +77000000001, 77000000001, 87000000001, 7000000001 and any spacing noise.
#
# Shared verbatim with the SQL purge in purge_orphans so the two can never
# drift. That sharing is only sound on DIGITS-ONLY input: Python's `$` also
# matches before a trailing newline while Postgres' does not, so both call
# sites must strip non-digits first (they do). `\Z` is not an option —
# Postgres regexes do not support it.
#
# The serial width is derived rather than hard-coded: a national number is 10
# digits, so widening CONSOLE_TEST_NATIONAL_PREFIX must narrow the serial, not
# silently make this pattern reject every live console phone — which would
# un-gate outbound sends to them.
_CONSOLE_SERIAL_DIGITS = 10 - len(config.CONSOLE_TEST_NATIONAL_PREFIX)
CONSOLE_PHONE_REGEX = (
    rf"^[78]?{config.CONSOLE_TEST_NATIONAL_PREFIX}[0-9]{{{_CONSOLE_SERIAL_DIGITS}}}$"
)


def is_test_phone(phone: str | None) -> bool:
    """True for a console phone, in any of the forms the normalizers produce."""
    return bool(re.match(CONSOLE_PHONE_REGEX, re.sub(r"\D", "", phone or "")))


def _row_to_session(row: dict) -> dict:
    session = dict(row)
    session["chat_id"] = f"{session['phone_number_id']}:{session['test_phone']}"
    return session


def create_session(bot_name: str, title: str | None, created_by: str | None) -> dict:
    """Allocate the next synthetic phone and open a session for `bot_name`."""
    phone_number_id = phone_number_id_for(bot_name)
    if not phone_number_id:
        raise ValueError(f"Unknown bot_name: {bot_name}")

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT nextval('console_phone_seq') AS n")
            serial = int(cur.fetchone()["n"])
            if serial > 9999:
                # Wrapping with % 10000 would reuse a retired phone, colliding
                # with the UNIQUE column and inheriting the old session's history.
                # Fail loudly instead; the block holds 10 000 sessions.
                raise RuntimeError(
                    "Console phone block 7%s0000-9999 is exhausted (%d sessions). "
                    "Widen CONSOLE_TEST_NATIONAL_PREFIX or reset console_phone_seq "
                    "after purging old sessions." % (config.CONSOLE_TEST_NATIONAL_PREFIX, serial)
                )
            test_phone = f"7{config.CONSOLE_TEST_NATIONAL_PREFIX}{serial:04d}"
            cur.execute(
                """
                INSERT INTO agent_test_sessions
                    (bot_name, phone_number_id, test_phone, title, created_by)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (bot_name, phone_number_id, test_phone, title, created_by),
            )
            return _row_to_session(cur.fetchone())


def list_sessions(limit: int = 100) -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM agent_test_sessions ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            return [_row_to_session(r) for r in cur.fetchall()]


def get_session(session_id: int) -> dict | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM agent_test_sessions WHERE id = %s", (session_id,))
            row = cur.fetchone()
            return _row_to_session(row) if row else None


def touch_session(session_id: int) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_test_sessions SET updated_at = NOW() WHERE id = %s",
                (session_id,),
            )


def delete_session_row(session_id: int) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM agent_test_sessions WHERE id = %s", (session_id,))


# Child rows hanging off a sandbox booking / trial, in FK-safe delete order.
# Both purges below run this same cascade and differ only in how they select the
# parent rows, so the table list lives in exactly one place. Each statement
# carries exactly one placeholder — the caller's scope parameter.
_CHILD_CASCADE = (
    ("payments", "DELETE FROM payments WHERE booking_id IN ({bookings})"),
    ("booking_events", "DELETE FROM booking_events WHERE booking_id IN ({bookings})"),
    ("booking_history", "DELETE FROM booking_history WHERE booking_id IN ({bookings})"),
    ("booking_sessions", "DELETE FROM booking_sessions WHERE booking_id IN ({bookings})"),
    ("trial_sessions", "DELETE FROM trial_sessions WHERE trial_id IN ({trials})"),
)


def _child_statements(bookings_sql: str, trials_sql: str, params: tuple) -> tuple:
    return tuple(
        (table, sql.format(bookings=bookings_sql, trials=trials_sql), params)
        for table, sql in _CHILD_CASCADE
    )


def _execute_isolated(cur, table: str, sql: str, params: tuple, deleted: dict) -> None:
    """Run one cleanup statement inside a SAVEPOINT.

    `_conn()` commits once at the end, so a bare rollback here would discard
    every delete already made in this transaction. A savepoint confines the
    failure to the one statement that failed.
    """
    cur.execute(f"SAVEPOINT sp_{table}")
    try:
        cur.execute(sql, params)
        deleted[table] = cur.rowcount
        cur.execute(f"RELEASE SAVEPOINT sp_{table}")
    except psycopg2.Error as exc:
        cur.execute(f"ROLLBACK TO SAVEPOINT sp_{table}")
        # -1 is not > 0, so a silently-failing purge would never be logged.
        logger.warning("[AGENT-TEST] Cleanup of %s failed: %s", table, exc)
        deleted[table] = -1


def purge_test_data(test_phone: str) -> dict:
    """Hard-delete every sandbox row this session created.

    Test rows carry no audit value, and leaving cancelled ones behind would make
    `get_existing_draft` / `has_active_trial` behave differently after a reset
    than on a fresh session — exactly the kind of drift that makes a test console
    untrustworthy. Every statement over a table that HAS `is_test` is scoped by
    both it and the phone, so a bug in one predicate cannot reach production
    rows. `bot_paused_contacts` has no such column and is scoped by exact phone
    match alone — keep it that way; a LIKE there would hit real subscribers.
    """
    deleted: dict[str, int] = {}
    booking_ids_sql = "SELECT id FROM bookings WHERE phone = %s AND is_test"
    trial_ids_sql = "SELECT id FROM academy_trials WHERE phone = %s AND is_test"
    scope = (test_phone,)

    statements = _child_statements(booking_ids_sql, trial_ids_sql, scope) + (
        ("bookings", "DELETE FROM bookings WHERE phone = %s AND is_test", scope),
        ("academy_trials_unlink",
         "UPDATE academy_trials SET user_id = NULL WHERE phone = %s AND is_test", scope),
        ("academy_trials", "DELETE FROM academy_trials WHERE phone = %s AND is_test", scope),
        ("academy_users", "DELETE FROM academy_users WHERE parent_phone = %s AND is_test", scope),
        ("bot_paused_contacts", "DELETE FROM bot_paused_contacts WHERE phone = %s", scope),
    )

    with _conn() as conn:
        with conn.cursor() as cur:
            for table, sql, params in statements:
                # A table may be absent in some environment; cleaning the rest
                # still matters more than that one failure.
                _execute_isolated(cur, table, sql, params, deleted)
    return deleted


def purge_orphans(older_than_days: int = 7) -> dict:
    """Sweep sandbox rows left behind by sessions that were never deleted.

    Test bookings are excluded from `get_expired_bookings`, so the normal TTL
    sweeper never reaches them — without this they would live forever.
    """
    deleted: dict[str, int] = {}
    stale = "is_test AND created_at < NOW() - %s::interval"
    scope = (f"{int(older_than_days)} days",)
    stale_bookings = f"SELECT id FROM bookings WHERE {stale}"
    stale_trials = f"SELECT id FROM academy_trials WHERE {stale}"

    statements = _child_statements(stale_bookings, stale_trials, scope) + (
        ("bookings", f"DELETE FROM bookings WHERE {stale}", scope),
        ("academy_trials", f"DELETE FROM academy_trials WHERE {stale}", scope),
        # academy_trials.user_id REFERENCES academy_users(id) with no ON DELETE,
        # so a surviving test trial would block its user's delete.
        ("academy_trials_unlink",
         f"UPDATE academy_trials SET user_id = NULL "
         f"WHERE user_id IN (SELECT id FROM academy_users WHERE {stale})", scope),
        ("academy_users", f"DELETE FROM academy_users WHERE {stale}", scope),
        # Pause flags are keyed by bare digits; clear any left on a console
        # phone, or that contact stays silently muted forever. The pattern is
        # anchored: a substring match on the block also hits real subscribers
        # (77017000001 contains "700000") and would un-mute a paused customer.
        ("bot_paused_contacts",
         "DELETE FROM bot_paused_contacts "
         "WHERE regexp_replace(phone, '\\D', '', 'g') ~ %s", (CONSOLE_PHONE_REGEX,)),
    )

    with _conn() as conn:
        with conn.cursor() as cur:
            for table, sql, params in statements:
                _execute_isolated(cur, table, sql, params, deleted)
    return deleted
