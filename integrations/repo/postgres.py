"""PostgreSQL integration — connection pool, schema init, booking CRUD."""

import json
import logging
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool

import config
from integrations.booking_service import _record_event, _ok, _err

logger = logging.getLogger(__name__)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1, maxconn=config.POSTGRES_MAX_CONN, dsn=config.POSTGRES_DSN
                )
    return _pool


@contextmanager
def _conn():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_ACADEMY_DRAFT_FIELDS = {
    "trial_day", "start_time", "end_time", "notes", "group_id", "user_id",
    "state", "language", "group_id", "user_id"
}

_FOOTBALL_DRAFT_FIELDS = {
    "date", "time_start", "time_end", "field", "format",
    "players", "customer_name", "notes",
    "phone", "state", "client_token", "source", "client_token"
}

draft_types = {"academy": _ACADEMY_DRAFT_FIELDS, "football": _FOOTBALL_DRAFT_FIELDS}


_DRAFTS_BY_BOTS = {
    "dopsy_bot": "football",
    "chatbot_2": "academy",
    "dopsy_boxing": "academy",
}


def create_draft(bot_name: str, **fields) -> dict:
    """Create (or return existing) DRAFT booking/trial. Idempotent on client_token."""
    patch = {k: v for k, v in fields.items() if k in draft_types[_DRAFTS_BY_BOTS[bot_name]]}
    cols, vals = list(patch.keys()), list(patch.values())
    placeholders = ", ".join(["%s"] * len(vals))
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            table_name = "bookings" if bot_name == "dopsy_bot" else "academy_trials"
            cur.execute(
                f"INSERT INTO {table_name} ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT (client_token) DO NOTHING RETURNING id",
                vals,
            )
            row = cur.fetchone()
            if row:
                object = row["id"]
                # _record_event(cur, booking_id, "draft_created", "whatsapp", chat_id)
            else:
                cur.execute(
                    f"SELECT id FROM {table_name} WHERE client_token = %s", (patch['client_token'],)
                )
                object_id = cur.fetchone()["id"]
    return _ok({"object_id": object_id})


def update_draft(bot_name: str, object_id: int, **patch) -> dict:
    """Patch a DRAFT booking's collected fields. Rejects if not in DRAFT."""
    fields = {k: v for k, v in patch.items() if k in draft_types[_DRAFTS_BY_BOTS[bot_name]]}
    if not fields:
        return _ok({"object_id": object_id})

    set_clause = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = NOW()"
    vals = list(fields.values()) + [object_id]
    table_name = "bookings" if bot_name == "dopsy_bot" else "academy_trials"
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"UPDATE {table_name} SET {set_clause} "
                f"WHERE id = %s AND state = 'draft' RETURNING id",
                vals,
            )
            if cur.fetchone():
                # _record_event(cur, object_id, "draft_updated", "whatsapp")
                return _ok({"object_id": object_id})

            cur.execute("SELECT state FROM bookings WHERE id = %s", (object_id,))
            row = cur.fetchone()
    if not row:
        return _err("NOT_FOUND", "Запись не найдена.")
    return _err("BOOKING_WRONG_STATE", "Эту запись уже нельзя изменить.")


def cancel_booking_trial(bot_name:str, object_id: int, actor_type: str = "whatsapp",
                   actor_id: str | None = None, reason: str | None = None) -> dict:
    """Cancel a booking (DRAFT or AWAITING_PAYMENT or CONFIRMED). Releases the slot
    and clears any conversation session still referencing it."""
    table_name = "bookings" if bot_name == "dopsy_bot" else "academy_trials"
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if bot_name == "dopsy_bot":
                extra = """
                    OR group_transition = (
                        SELECT group_transition
                        FROM bookings
                        WHERE id = %s
                    )
                """
                params = (object_id, object_id)
            else:
                extra = ""
                params = (object_id,)
            cur.execute(
                f"UPDATE {table_name} SET state = 'cancelled', updated_at = NOW() "
                f"WHERE (id = %s ) {extra}"
                "AND state NOT IN ('cancelled', 'failed') RETURNING id",
                params,
            )
            if cur.fetchone():
                # _record_event(cur, object_id, "cancelled", actor_type, actor_id, reason)
                table_del = "booking_sessions" if bot_name == "dopsy_bot" else "trials_session"
                cur.execute(
                    "DELETE FROM booking_sessions WHERE booking_id = %s", (object_id,)
                )
                return _ok({"object_id": object_id})
            cur.execute(f"SELECT state FROM {table_name} WHERE id = %s", (object_id,))
            row = cur.fetchone()
    if not row:
        return _err("NOT_FOUND", "Запись не найдена.")
    return _ok({"object_id": object_id}, message="Запись уже была отменена.")





# ---------------------------------------------------------------------------
#  Booking/Trial sessions
# ---------------------------------------------------------------------------

def get_active_session(bot_name: str, chat_id: str) -> dict | None:
    """Return the active (non-expired) session for chat_id, or None."""
    table_name = "booking_sessions" if bot_name == "dopsy_bot" else "trial_sessions"

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT chat_id, state, params, booking_id
                FROM {table_name}
                WHERE chat_id = %s AND expires_at > NOW()
            """, (chat_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def upsert_session(
    bot_name: str,
    chat_id: str,
    state: str,
    params: dict,
    object_id: int | None = None,
) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            table_name = "booking_sessions" if bot_name == "dopsy_bot" else "trial_sessions"
            id_object = "booking_id" if table_name == "booking_session" else "trial_id"

            cur.execute(f"""
                INSERT INTO {table_name} (chat_id, state, params, {id_object}, expires_at)
                VALUES (%s, %s, %s, %s, NOW() + make_interval(secs => %s))
                ON CONFLICT (chat_id) DO UPDATE SET
                    state      = EXCLUDED.state,
                    params     = EXCLUDED.params,
                    {id_object} = EXCLUDED.{id_object},
                    updated_at = NOW(),
                    expires_at = EXCLUDED.expires_at
            """, (chat_id, state, json.dumps(params, ensure_ascii=False, default=str),
                  object_id, config.BOOKING_SESSION_TTL))


def delete_session(bot_name: str, chat_id: str) -> None:
    table_name = "booking_sessions" if bot_name == "dopsy_bot" else "trial_sessions"
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table_name} WHERE chat_id = %s", (chat_id,))
