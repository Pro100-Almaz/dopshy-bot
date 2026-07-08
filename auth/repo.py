import threading

import psycopg2
import psycopg2.extras
import psycopg2.pool
import config

from zoneinfo import ZoneInfo
from contextlib import contextmanager

from auth.query_models import row_to_qm, UserQm
from auth.models import UserModel

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()

ALMATY_TZ = ZoneInfo("Asia/Almaty")

def _get_pool():
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



def insert_user(email: str, phone_number: str, first_name: str, last_name: str, password_hash: str, role: str):
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO auth_users (email, password_hash, phone_number, first_name, last_name, role)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (email) DO NOTHING
                """, (email, password_hash, phone_number, first_name, last_name, role)
            )

def get_user_by_email(email: str) -> UserQm | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, email, phone_number, role, first_name, last_name, is_active, updated_at from auth_users
                WHERE email = %s
                """, (email,)
            )
            row = cur.fetchone()
            return row_to_qm(row) if row else None

def get_user_credentials(email: str) -> UserModel | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, email, role, password_hash, phone_number, first_name, last_name, created_at, updated_at from auth_users
                WHERE email = %s
                """, (email,)
            )
            row = cur.fetchone()
            return UserModel(**row) if row else None


def has_no_duplicate_phone(phone_number: str) -> bool:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT 1 from auth_users
                WHERE phone_number = %s
                """, (phone_number,)
            )
            return cur.fetchone() is None

def has_no_duplicate_email(email: str) -> bool:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT 1 from auth_users
                WHERE email = %s
                """, (email,)
            )
            return cur.fetchone() is None

