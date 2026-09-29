from contextlib import contextmanager
import re
import threading

import psycopg2
import psycopg2.extras
import psycopg2.pool

import config

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def normalize_phone(phone: str | None) -> str:
    """Canonical digits-only phone representation used across repositories."""
    return re.sub(r"\D", "", phone or "")


def phone_variants(phone: str | None) -> list[str]:
    """Every spelling a stored phone may have: as given, digits-only, '+digits'.

    New rows are saved normalized, but rows written before that (and callers
    passing the raw WhatsApp '+7...' form) must still match, so lookups compare
    with `phone = ANY(%s)` against this list instead of a single value.
    """
    raw = (phone or "").strip()
    digits = normalize_phone(raw)
    candidates = (raw, digits, f"+{digits}" if digits else "")
    return list(dict.fromkeys(c for c in candidates if c))


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


def _ok(data: dict | None = None, code: str = "OK", message: str = "") -> dict:
    return {"ok": True, "code": code, "data": data, "message": message}


def _err(code: str, message: str) -> dict:
    return {"ok": False, "code": code, "data": None, "message": message}

