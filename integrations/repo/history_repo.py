"""Booking change history — templated descriptions + read/write helpers.

`booking_history` rows carry a human-readable `description` rendered from the
dynamic templates in integrations/repo/history_descriptions.json, and a `source` that is
either the literal 'whatsapp' (bot-driven change) or the manager's email
address (manager-driven change).

Write path:
    - _record_history(cur, ...)  runs on an existing cursor, for use inside a
      booking_service transaction alongside _record_event (mirrors that helper).
    - record_history(...)        opens its own pooled connection.
Both accept either a ready `description` or a template `key` + params.

Read path: get_all_history / get_history_between / get_history_for_booking /
get_history_by_source (+ whatsapp/manager channel helpers).
"""

import json
import os

import psycopg2.extras

from integrations.repo.utils import _conn

# Bot-driven changes use this literal source; manager-driven changes use the
# manager's email address, so `source != WHATSAPP_SOURCE` means "a manager".
WHATSAPP_SOURCE = "whatsapp"

# Colocated with this module (NOT under data/, which is a persistent named
# volume in docker-compose that would shadow the image-shipped file).
_DESCRIPTIONS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "history_descriptions.json",
)

_templates: dict | None = None


def _load_templates() -> dict:
    """Load (and cache) the description templates from disk."""
    global _templates
    if _templates is None:
        print("Loading description templates...", _DESCRIPTIONS_PATH)
        with open(_DESCRIPTIONS_PATH, encoding="utf-8") as fh:
            _templates = {k: v for k, v in json.load(fh).items()
                          if not k.startswith("_")}
    return _templates


def render_description(key: str, **params) -> str:
    """Render a template by key, substituting [UPPERCASE] tokens from params.

    A param `old_status=...` fills the `[OLD_STATUS]` token. Unknown tokens are
    left untouched. Raises KeyError if the template key does not exist.
    """
    template = _load_templates()[key]
    for name, value in params.items():
        template = template.replace(f"[{name.upper()}]", str(value))
    return template


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def _record_history(cur, booking_id: int, source: str,
                    description: str | None = None,
                    key: str | None = None, **params) -> None:
    """Insert a history row on an existing cursor (transaction-local).

    Pass either a ready `description` or a template `key` (+ token params).
    """
    if description is None:
        if key is None:
            raise ValueError("record_history requires either description or key")
        description = render_description(key, **params)
    cur.execute(
        "INSERT INTO booking_history (booking_id, source, description) "
        "VALUES (%s, %s, %s)",
        (booking_id, source, description),
    )


def record_history(booking_id: int, source: str,
                   description: str | None = None,
                   key: str | None = None, **params) -> None:
    """Standalone insert (own pooled connection). See _record_history."""
    with _conn() as conn:
        with conn.cursor() as cur:
            _record_history(cur, booking_id, source, description, key, **params)


# ---------------------------------------------------------------------------
# Read
#
# Every reader returns (rows, total): `rows` is the (optionally paginated) page
# and `total` is the unpaginated match count. When `limit` is None the whole
# result set is returned and total == len(rows); when `limit` is set a
# COUNT(*) OVER() window yields the full count in the same round trip.
# ---------------------------------------------------------------------------

_COLUMNS = "id, booking_id, source, description, created_at"


def _fetch(where: str, params: tuple, order: str,
           limit: int | None = None, offset: int = 0) -> tuple[list[dict], int]:
    count_col = ", COUNT(*) OVER() AS _total" if limit is not None else ""
    sql = f"SELECT {_COLUMNS}{count_col} FROM booking_history"
    if where:
        sql += f" WHERE {where}"
    sql += f" ORDER BY {order}"
    args = list(params)
    if limit is not None:
        sql += " LIMIT %s OFFSET %s"
        args += [limit, offset]

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()]

    if limit is None:
        return rows, len(rows)
    total = rows[0]["_total"] if rows else 0
    for r in rows:
        r.pop("_total", None)
    return rows, total


def get_all_history(limit: int | None = None, offset: int = 0) -> tuple[list[dict], int]:
    """Every history row, newest first."""
    return _fetch("", (), "created_at DESC", limit, offset)


def get_history_between(start, end, limit: int | None = None,
                        offset: int = 0) -> tuple[list[dict], int]:
    """History rows with created_at in [start, end] (inclusive), newest first.

    `start`/`end` accept anything psycopg2 adapts to timestamptz (datetime or
    'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS' strings).
    """
    return _fetch("created_at BETWEEN %s AND %s", (start, end),
                  "created_at DESC", limit, offset)


def get_history_for_booking(booking_id: int, limit: int | None = None,
                            offset: int = 0) -> tuple[list[dict], int]:
    """History rows for one booking, oldest first (chronological timeline)."""
    return _fetch("booking_id = %s", (booking_id,), "created_at ASC", limit, offset)


def get_history_by_source(source: str, limit: int | None = None,
                          offset: int = 0) -> tuple[list[dict], int]:
    """History rows from an exact source ('whatsapp' or a manager email)."""
    return _fetch("source = %s", (source,), "created_at DESC", limit, offset)


def get_whatsapp_history(limit: int | None = None,
                         offset: int = 0) -> tuple[list[dict], int]:
    """All bot-driven (WhatsApp) history rows, newest first."""
    return get_history_by_source(WHATSAPP_SOURCE, limit, offset)


def get_manager_history(email: str | None = None, limit: int | None = None,
                        offset: int = 0) -> tuple[list[dict], int]:
    """Manager-driven history rows (source is an email, not 'whatsapp').

    Pass `email` to scope to one manager; omit for every manager.
    """
    if email is not None:
        return get_history_by_source(email, limit, offset)
    return _fetch("source <> %s", (WHATSAPP_SOURCE,), "created_at DESC", limit, offset)
