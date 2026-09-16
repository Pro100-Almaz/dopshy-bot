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

# Bot name → the env key holding its phone_number_id.
BOT_NAMES = ("dopsy_bot", "dopsy_fs_school", "dopsy_boxing")


def phone_number_id_for(bot_name: str) -> str | None:
    for phone_number_id, cfg in config.BOT_CONFIGS.items():
        if cfg["name"] == bot_name:
            return phone_number_id
    return None


def is_test_phone(phone: str | None) -> bool:
    """True for a console phone, in any of the forms the normalizers produce.

    Keyed on the NATIONAL part, because apipay_client.normalize_phone rewrites a
    leading 7 to 8. Matches +77000000001, 77000000001, 87000000001, 7000000001
    and any spacing/bracket noise.
    """
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    return len(digits) == 10 and digits.startswith(config.CONSOLE_TEST_NATIONAL_PREFIX)


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
    untrustworthy. Every statement is scoped by BOTH `is_test` and the phone, so
    a bug in one predicate can never reach production rows.
    """
    deleted: dict[str, int] = {}
    booking_ids_sql = "SELECT id FROM bookings WHERE phone = %s AND is_test"
    trial_ids_sql = "SELECT id FROM academy_trials WHERE phone = %s AND is_test"

    with _conn() as conn:
        with conn.cursor() as cur:
            for table, sql, params in (
                ("payments", f"DELETE FROM payments WHERE booking_id IN ({booking_ids_sql})", (test_phone,)),
                ("booking_events", f"DELETE FROM booking_events WHERE booking_id IN ({booking_ids_sql})", (test_phone,)),
                ("booking_history", f"DELETE FROM booking_history WHERE booking_id IN ({booking_ids_sql})", (test_phone,)),
                ("booking_sessions", f"DELETE FROM booking_sessions WHERE booking_id IN ({booking_ids_sql})", (test_phone,)),
                ("trial_sessions", f"DELETE FROM trial_sessions WHERE trial_id IN ({trial_ids_sql})", (test_phone,)),
                ("bookings", "DELETE FROM bookings WHERE phone = %s AND is_test", (test_phone,)),
                ("academy_trials_unlink", "UPDATE academy_trials SET user_id = NULL WHERE phone = %s AND is_test", (test_phone,)),
                ("academy_trials", "DELETE FROM academy_trials WHERE phone = %s AND is_test", (test_phone,)),
                ("academy_users", "DELETE FROM academy_users WHERE parent_phone = %s AND is_test", (test_phone,)),
                ("bot_paused_contacts", "DELETE FROM bot_paused_contacts WHERE phone = %s", (test_phone,)),
            ):
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
    interval = f"{int(older_than_days)} days"
    stale_bookings = (
        "SELECT id FROM bookings WHERE is_test AND created_at < NOW() - %s::interval"
    )
    stale_trials = (
        "SELECT id FROM academy_trials WHERE is_test AND created_at < NOW() - %s::interval"
    )
    with _conn() as conn:
        with conn.cursor() as cur:
            for table, sql in (
                ("payments", f"DELETE FROM payments WHERE booking_id IN ({stale_bookings})"),
                ("booking_events", f"DELETE FROM booking_events WHERE booking_id IN ({stale_bookings})"),
                ("booking_history", f"DELETE FROM booking_history WHERE booking_id IN ({stale_bookings})"),
                ("booking_sessions", f"DELETE FROM booking_sessions WHERE booking_id IN ({stale_bookings})"),
                ("trial_sessions", f"DELETE FROM trial_sessions WHERE trial_id IN ({stale_trials})"),
                ("bookings", "DELETE FROM bookings WHERE is_test AND created_at < NOW() - %s::interval"),
                ("academy_trials", "DELETE FROM academy_trials WHERE is_test AND created_at < NOW() - %s::interval"),
                # academy_trials.user_id REFERENCES academy_users(id) with no ON
                # DELETE, so a surviving test trial would block its user's delete.
                ("academy_trials_unlink",
                 "UPDATE academy_trials SET user_id = NULL WHERE user_id IN "
                 "(SELECT id FROM academy_users WHERE is_test "
                 " AND created_at < NOW() - %s::interval)"),
                ("academy_users", "DELETE FROM academy_users WHERE is_test AND created_at < NOW() - %s::interval"),
                # Pause flags are keyed by bare digits; clear any left on a
                # console phone, or that contact stays silently muted forever.
                ("bot_paused_contacts",
                 "DELETE FROM bot_paused_contacts "
                 "WHERE regexp_replace(phone, '\\D', '', 'g') LIKE %s"),
            ):
                params = (
                    (f"%{config.CONSOLE_TEST_NATIONAL_PREFIX}%",)
                    if table == "bot_paused_contacts"
                    else (interval,)
                )
                _execute_isolated(cur, table, sql, params, deleted)
    return deleted
