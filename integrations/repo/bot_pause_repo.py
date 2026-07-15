"""Per-contact bot pause state (bot_paused_contacts table).

The bot checks is_bot_paused() on every inbound message and stays silent when a
contact is paused. The manager UI toggles state through set_bot_paused(); the
whatsapp.smb.message.echoes webhook flips it automatically (reason='auto') when
a human agent replies on WhatsApp.
"""
import re

import psycopg2.extras

from integrations.repo.postgres import _conn


def _normalize(phone: str | None) -> str:
    """Canonical digits-only form so lookups match regardless of formatting.

    WhatsApp/YCloud deliver the same number inconsistently — with or without a
    leading '+', sometimes with spaces (e.g. '+7 707 000 00 00'). We key the
    pause table on digits only so a contact paused once stays paused no matter
    which formatting an inbound message arrives with.
    """
    return re.sub(r"\D", "", phone or "")


# Public alias — other modules (e.g. the contacts endpoint) normalize phones
# the same way before merging/deduping them.
normalize_phone = _normalize


def is_bot_paused(phone: str) -> bool:
    """True if the bot should stay silent for this phone number."""
    key = _normalize(phone)
    if not key:
        return False
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT paused FROM bot_paused_contacts WHERE phone = %s",
                (key,),
            )
            row = cur.fetchone()
    return bool(row and row[0])


def set_bot_paused(
    phone: str,
    paused: bool,
    reason: str = "manual",
    paused_by: str | None = None,
) -> dict:
    """Upsert the pause state for a phone number. Returns the new state."""
    key = _normalize(phone)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_paused_contacts (phone, paused, reason, paused_by, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (phone) DO UPDATE SET
                    paused     = EXCLUDED.paused,
                    reason     = EXCLUDED.reason,
                    paused_by  = EXCLUDED.paused_by,
                    updated_at = NOW()
                """,
                (key, paused, reason, paused_by),
            )
    return {"phone": phone, "paused": paused, "paused_reason": reason if paused else None}


def get_statuses(phones: list[str]) -> dict[str, dict]:
    """Map each requested phone to its {paused, paused_reason}.

    Phones with no row default to paused=False so the frontend can annotate a
    whole customer list in one call.
    """
    # Keep the response keyed by exactly what the caller passed, but match on
    # the normalized form so '+7...' and '7...' resolve to the same row.
    result = {p: {"paused": False, "paused_reason": None} for p in phones}
    if not phones:
        return result
    by_key: dict[str, list[str]] = {}
    for p in phones:
        by_key.setdefault(_normalize(p), []).append(p)
    keys = [k for k in by_key if k]
    if not keys:
        return result
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT phone, paused, reason FROM bot_paused_contacts WHERE phone = ANY(%s)",
                (keys,),
            )
            for row in cur.fetchall():
                status = {
                    "paused": bool(row["paused"]),
                    "paused_reason": row["reason"] if row["paused"] else None,
                }
                for original in by_key.get(row["phone"], []):
                    result[original] = status
    return result
