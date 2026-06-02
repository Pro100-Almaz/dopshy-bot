"""PostgreSQL integration — connection pool, schema init, booking CRUD."""

import json
import logging
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool

import config

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

# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

def get_booked_slots(week_start: str, week_end: str) -> list[dict]:
    """Return slot-holding bookings (awaiting_payment + confirmed) in a date range."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, date, time_start, time_end, field, format, state,
                       customer_name, phone, players, notes
                FROM bookings
                WHERE date BETWEEN %s AND %s
                  AND state IN ('awaiting_payment', 'confirmed')
                ORDER BY date, time_start, field
            """, (week_start, week_end))
            return [dict(r) for r in cur.fetchall()]


def get_user_upcoming_bookings(phone: str) -> list[dict]:
    """Return upcoming (today or later) non-cancelled bookings for a phone number."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, date, time_start, time_end, field, format, players,
                       customer_name, state, notes
                FROM bookings
                WHERE phone = %s
                  AND date >= CURRENT_DATE
                  AND state IN ('awaiting_payment', 'confirmed')
                ORDER BY date, time_start
            """, (phone,))
            return [dict(r) for r in cur.fetchall()]


def get_user_editable_bookings(phone: str) -> list[dict]:
    """Bookings this client could potentially edit (future, slot-holding state).

    The 48h window + once-only checks live in booking_service.client_edit_booking
    so callers see *all* editable-shaped rows here, including ones that the
    service layer will then reject. This avoids the handler having to duplicate
    policy when it disambiguates between multiple bookings.
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, date, time_start, time_end, field, format, players,
                       customer_name, state, start_at, client_edited_at,
                       predecessor_booking_id
                FROM bookings
                WHERE phone = %s
                  AND state IN ('awaiting_payment', 'confirmed')
                  AND start_at > NOW()
                ORDER BY start_at
            """, (phone,))
            return [dict(r) for r in cur.fetchall()]


def get_awaiting_payment_booking(phone: str) -> dict | None:
    """Return the most recent awaiting_payment booking for this phone."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, date, time_start, time_end, field, format, players,
                       customer_name, state, sheet_row, client_token, price_total
                FROM bookings
                WHERE phone = %s AND state = 'awaiting_payment'
                ORDER BY created_at DESC
                LIMIT 1
            """, (phone,))
            row = cur.fetchone()
            return dict(row) if row else None


def get_bookings_for_sheet() -> list[dict]:
    """All slot-holding bookings (awaiting_payment + confirmed) for the flat Sheet view."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, field, date, time_start, time_end, customer_name,
                       phone, notes, state
                FROM bookings
                WHERE state IN ('awaiting_payment', 'confirmed')
                ORDER BY date, time_start, field
            """)
            return [dict(r) for r in cur.fetchall()]


def get_bookings_in_range(start: str, end: str, states: tuple = ("awaiting_payment", "confirmed")) -> list[dict]:
    """Bookings between two dates (inclusive) for the manager API list view."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, field, date, time_start, time_end, customer_name,
                       phone, notes, state, price_total, source
                FROM bookings
                WHERE date BETWEEN %s AND %s AND state = ANY(%s)
                ORDER BY date, time_start, field
            """, (start, end, list(states)))
            return [dict(r) for r in cur.fetchall()]


def get_booking(booking_id: int) -> dict | None:
    """Return a single booking with full detail, or None."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, field, date, time_start, time_end, customer_name,
                       phone, notes, state, price_total, source, created_at
                FROM bookings WHERE id = %s
            """, (booking_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def get_payment_recipients() -> list[dict]:
    """Active acceptable payment recipients (for receipt validation)."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT bank, bin, name, phone FROM payment_recipients WHERE active = TRUE"
            )
            return [dict(r) for r in cur.fetchall()]


def get_expired_bookings(session_ttl_seconds: int) -> list[dict]:
    """
    Return bookings whose reservation/draft window has elapsed:
      - awaiting_payment past their reserved_until
      - draft rows older than session_ttl_seconds (abandoned flows)

    Read-only — the caller cancels each through booking_service for the audit log.
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, state, date, field, format, time_start, time_end, phone, customer_name
                FROM bookings
                WHERE (state = 'awaiting_payment' AND reserved_until < NOW())
                   OR (state = 'draft' AND created_at < NOW() - make_interval(secs => %s))
            """, (session_ttl_seconds,))
            return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Booking sessions
# ---------------------------------------------------------------------------

def get_active_session(chat_id: str) -> dict | None:
    """Return the active (non-expired) session for chat_id, or None."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT chat_id, state, params, booking_id
                FROM booking_sessions
                WHERE chat_id = %s AND expires_at > NOW()
            """, (chat_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def upsert_session(
    chat_id: str,
    state: str,
    params: dict,
    booking_id: int | None = None,
) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO booking_sessions (chat_id, state, params, booking_id, expires_at)
                VALUES (%s, %s, %s, %s, NOW() + make_interval(secs => %s))
                ON CONFLICT (chat_id) DO UPDATE SET
                    state      = EXCLUDED.state,
                    params     = EXCLUDED.params,
                    booking_id = EXCLUDED.booking_id,
                    updated_at = NOW(),
                    expires_at = EXCLUDED.expires_at
            """, (chat_id, state, json.dumps(params, ensure_ascii=False, default=str),
                  booking_id, config.BOOKING_SESSION_TTL))


def delete_session(chat_id: str) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM booking_sessions WHERE chat_id = %s", (chat_id,))
