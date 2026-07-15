"""Per-contact bot pause state (bot_paused_contacts table).

The bot checks is_bot_paused() on every inbound message and stays silent when a
contact is paused. The manager UI toggles state through set_bot_paused(); the
whatsapp.smb.message.echoes webhook flips it automatically (reason='auto') when
a human agent replies on WhatsApp.
"""
import psycopg2.extras

from integrations.repo.postgres import _conn


def is_bot_paused(phone: str) -> bool:
    """True if the bot should stay silent for this phone number."""
    if not phone:
        return False
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT paused FROM bot_paused_contacts WHERE phone = %s",
                (phone,),
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
                (phone, paused, reason, paused_by),
            )
    return {"phone": phone, "paused": paused, "paused_reason": reason if paused else None}


def get_statuses(phones: list[str]) -> dict[str, dict]:
    """Map each requested phone to its {paused, paused_reason}.

    Phones with no row default to paused=False so the frontend can annotate a
    whole customer list in one call.
    """
    result = {p: {"paused": False, "paused_reason": None} for p in phones}
    if not phones:
        return result
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT phone, paused, reason FROM bot_paused_contacts WHERE phone = ANY(%s)",
                (phones,),
            )
            for row in cur.fetchall():
                result[row["phone"]] = {
                    "paused": bool(row["paused"]),
                    "paused_reason": row["reason"] if row["paused"] else None,
                }
    return result
