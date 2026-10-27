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


# Kazakh letters and Latin spellings folded to plain Cyrillic, so «Әлихан»,
# «Алихан» and «Alikhan» compare as one name.
_NAME_FOLD = (
    ("kh", "х"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("ya", "я"), ("yu", "ю"),
    ("ә", "а"), ("і", "и"), ("ң", "н"), ("ғ", "г"), ("ү", "у"), ("ұ", "у"),
    ("қ", "к"), ("ө", "о"), ("һ", "х"), ("ё", "е"), ("й", "и"), ("ы", "и"),
    ("a", "а"), ("b", "б"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"),
    ("h", "х"), ("i", "и"), ("j", "ж"), ("k", "к"), ("l", "л"), ("m", "м"),
    ("n", "н"), ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"), ("s", "с"),
    ("t", "т"), ("u", "у"), ("v", "в"), ("y", "и"), ("z", "з"),
)


def _fold_name(name: str | None) -> str:
    value = " ".join((name or "").lower().split())
    for source, target in _NAME_FOLD:
        value = value.replace(source, target)
    return value


def same_child_name(a: str | None, b: str | None) -> bool:
    """True when two spellings are one child's name: equal once Kazakh and
    Latin letters are folded, or one letter apart («Айсултан» / «Айсултанн»).
    Short names must match exactly — «Али» and «Аля» are different children.
    """
    a, b = _fold_name(a), _fold_name(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    # One edit (substitution, insertion or deletion) apart.
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i + (len(a) == len(b)):] == b[i + 1:]


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

