"""PostgreSQL integration — connection pool, schema init, booking CRUD."""

import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

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
                    minconn=1, maxconn=10, dsn=config.POSTGRES_DSN
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

def init_schema() -> None:
    """Create tables if they do not exist. Called once at app startup."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bookings (
                    id            SERIAL PRIMARY KEY,
                    phone         VARCHAR(20)   NOT NULL,
                    customer_name VARCHAR(100),
                    date          DATE          NOT NULL,
                    time_start    TIME          NOT NULL,
                    time_end      TIME          NOT NULL,
                    field         SMALLINT      NOT NULL,
                    format        VARCHAR(5)    NOT NULL,
                    players       SMALLINT,
                    status        VARCHAR(20)   NOT NULL DEFAULT 'awaiting_payment',
                    sheet_row     INTEGER,
                    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
                    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
                    notes         TEXT,
                    UNIQUE (date, time_start, field)
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bookings_phone  ON bookings (phone)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bookings_date   ON bookings (date)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings (status)"
            )

            cur.execute("""
                CREATE TABLE IF NOT EXISTS booking_sessions (
                    chat_id    TEXT        PRIMARY KEY,
                    state      VARCHAR(20) NOT NULL,
                    params     JSONB       NOT NULL DEFAULT '{}',
                    booking_id INTEGER     REFERENCES bookings(id),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMPTZ NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS sheets_sync_state (
                    id         SERIAL      PRIMARY KEY,
                    week_start DATE        NOT NULL UNIQUE,
                    synced_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
    logger.info("PostgreSQL schema ready.")


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

def get_booked_slots(week_start: str, week_end: str) -> list[dict]:
    """Return all non-cancelled bookings in the given date range."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT date, time_start, time_end, field, format, status,
                       customer_name, phone, players
                FROM bookings
                WHERE date BETWEEN %s AND %s
                  AND status != 'cancelled'
                ORDER BY date, time_start, field
            """, (week_start, week_end))
            return [dict(r) for r in cur.fetchall()]


def get_user_upcoming_bookings(phone: str) -> list[dict]:
    """Return upcoming (today or later) non-cancelled bookings for a phone number."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, date, time_start, time_end, field, format, players,
                       customer_name, status, notes
                FROM bookings
                WHERE phone = %s
                  AND date >= CURRENT_DATE
                  AND status != 'cancelled'
                ORDER BY date, time_start
            """, (phone,))
            return [dict(r) for r in cur.fetchall()]


def get_awaiting_payment_booking(phone: str) -> dict | None:
    """Return the most recent awaiting_payment booking for this phone."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, date, time_start, time_end, field, format, players,
                       customer_name, status, sheet_row
                FROM bookings
                WHERE phone = %s AND status = 'awaiting_payment'
                ORDER BY created_at DESC
                LIMIT 1
            """, (phone,))
            row = cur.fetchone()
            return dict(row) if row else None


def create_booking(
    phone: str,
    customer_name: str,
    date: str,
    time_start: str,
    time_end: str,
    field: int,
    format_: str,
    players: int,
) -> int:
    """Insert a new booking and return its id."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bookings
                    (phone, customer_name, date, time_start, time_end, field, format, players)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (phone, customer_name, date, time_start, time_end, field, format_, players))
            return cur.fetchone()[0]


def update_booking_status(booking_id: int, status: str) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bookings SET status = %s, updated_at = NOW() WHERE id = %s",
                (status, booking_id),
            )


def cancel_expired_bookings(ttl_seconds: int) -> list[dict]:
    """
    Cancel all awaiting_payment bookings older than ttl_seconds.
    Returns the cancelled rows so callers can notify users and refresh Sheets.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=ttl_seconds)
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                UPDATE bookings
                SET status = 'cancelled', updated_at = NOW()
                WHERE status = 'awaiting_payment'
                  AND created_at < %s
                RETURNING id, date, field, format, time_start, time_end, phone, customer_name
            """, (cutoff,))
            return [dict(r) for r in cur.fetchall()]


def set_booking_sheet_row(booking_id: int, row: int) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bookings SET sheet_row = %s WHERE id = %s",
                (row, booking_id),
            )


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
    expires_at = datetime.utcnow() + timedelta(seconds=config.BOOKING_SESSION_TTL)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO booking_sessions (chat_id, state, params, booking_id, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (chat_id) DO UPDATE SET
                    state      = EXCLUDED.state,
                    params     = EXCLUDED.params,
                    booking_id = EXCLUDED.booking_id,
                    updated_at = NOW(),
                    expires_at = EXCLUDED.expires_at
            """, (chat_id, state, json.dumps(params, ensure_ascii=False, default=str),
                  booking_id, expires_at))


def delete_session(chat_id: str) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM booking_sessions WHERE chat_id = %s", (chat_id,))


# ---------------------------------------------------------------------------
# Sheets sync state
# ---------------------------------------------------------------------------

def is_week_synced_to_sheets(week_start: str) -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM sheets_sync_state WHERE week_start = %s", (week_start,)
            )
            return cur.fetchone() is not None


def mark_week_synced_to_sheets(week_start: str) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sheets_sync_state (week_start)
                VALUES (%s)
                ON CONFLICT (week_start) DO UPDATE SET synced_at = NOW()
            """, (week_start,))
