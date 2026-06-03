"""Booking service layer — the only entry point for booking mutations.

Every function returns a typed result envelope:
    {"ok": bool, "code": str, "data": dict | None, "message": str}

`code` is machine-readable (see ERROR CODES below); `message` is the
human-friendly RU text the bot echoes to the user.

State machine: draft → awaiting_payment → confirmed
                            ├→ cancelled (TTL expired / user cancelled)
                            └→ failed    (payment rejected)

Slot overlap is enforced by the `bookings_no_overlap` EXCLUDE constraint
(scoped to awaiting_payment + confirmed), not by application checks.
"""

import logging
import uuid
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.errors
import psycopg2.extras

import config
from integrations.postgres import _conn
from integrations.sheets import refresh_all_bookings

logger = logging.getLogger(__name__)

# Patch fields a draft may set via update_draft.
_DRAFT_FIELDS = {
    "date", "time_start", "time_end", "field", "format",
    "players", "customer_name", "notes",
}


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------

def _ok(data: dict | None = None, code: str = "OK", message: str = "") -> dict:
    return {"ok": True, "code": code, "data": data, "message": message}


def _err(code: str, message: str) -> dict:
    return {"ok": False, "code": code, "data": None, "message": message}


def _record_event(cur, booking_id: int, event: str, actor_type: str,
                  actor_id: str | None = None, note: str | None = None) -> None:
    cur.execute(
        "INSERT INTO booking_events (booking_id, event, actor_type, actor_id, note) "
        "VALUES (%s, %s, %s, %s, %s)",
        (booking_id, event, actor_type, actor_id, note),
    )


# ---------------------------------------------------------------------------
# Service functions
# ---------------------------------------------------------------------------

def create_draft(chat_id: str, phone: str, client_token: str, **fields) -> dict:
    """Create (or return existing) DRAFT booking. Idempotent on client_token."""
    patch = {k: v for k, v in fields.items() if k in _DRAFT_FIELDS}
    cols = ["phone", "state", "client_token", "source"] + list(patch.keys())
    vals = [phone, "draft", client_token, "whatsapp"] + list(patch.values())
    placeholders = ", ".join(["%s"] * len(vals))
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"INSERT INTO bookings ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT (client_token) DO NOTHING RETURNING id",
                vals,
            )
            row = cur.fetchone()
            if row:
                booking_id = row["id"]
                _record_event(cur, booking_id, "draft_created", "whatsapp", chat_id)
            else:
                cur.execute(
                    "SELECT id FROM bookings WHERE client_token = %s", (client_token,)
                )
                booking_id = cur.fetchone()["id"]
    return _ok({"booking_id": booking_id})


def update_draft(booking_id: int, **patch) -> dict:
    """Patch a DRAFT booking's collected fields. Rejects if not in DRAFT."""
    fields = {k: v for k, v in patch.items() if k in _DRAFT_FIELDS}
    if not fields:
        return _ok({"booking_id": booking_id})

    set_clause = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = NOW()"
    vals = list(fields.values()) + [booking_id]
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"UPDATE bookings SET {set_clause} "
                f"WHERE id = %s AND state = 'draft' RETURNING id",
                vals,
            )
            if cur.fetchone():
                _record_event(cur, booking_id, "draft_updated", "whatsapp")
                return _ok({"booking_id": booking_id})

            cur.execute("SELECT state FROM bookings WHERE id = %s", (booking_id,))
            row = cur.fetchone()
    if not row:
        return _err("NOT_FOUND", "Бронь не найдена.")
    return _err("BOOKING_WRONG_STATE", "Эту бронь уже нельзя изменить.")


def request_payment(booking_id: int, client_token: str) -> dict:
    """Transition DRAFT → AWAITING_PAYMENT: reserve the slot and start the TTL.

    The EXCLUDE constraint atomically rejects a slot already held by another
    awaiting_payment/confirmed booking → SLOT_TAKEN.
    """
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, state, date, time_start, time_end, field "
                    "FROM bookings WHERE id = %s AND client_token = %s FOR UPDATE",
                    (booking_id, client_token),
                )
                row = cur.fetchone()
                if not row:
                    return _err("NOT_FOUND", "Бронь не найдена.")
                if row["state"] == "awaiting_payment":
                    return _ok({"booking_id": booking_id}, message="Бронь уже ожидает оплаты.")
                if row["state"] != "draft":
                    return _err("BOOKING_WRONG_STATE", "Эту бронь уже нельзя подтвердить.")
                if not (row["date"] and row["time_start"] and row["time_end"] and row["field"]):
                    return _err("INVALID_TIME", "Не все данные брони заполнены.")

                cur.execute(
                    "UPDATE bookings SET "
                    "  state = 'awaiting_payment', "
                    "  reserved_until = NOW() + make_interval(secs => %s), "
                    "  start_at = (date + time_start) AT TIME ZONE %s, "
                    "  end_at   = (date + time_end)   AT TIME ZONE %s, "
                    "  price_total = (SELECT price_per_hour FROM fields WHERE id = bookings.field) "
                    "                * (EXTRACT(EPOCH FROM ((date + time_end) - (date + time_start))) / 3600.0), "
                    "  updated_at = NOW() "
                    "WHERE id = %s",
                    (config.PAYMENT_TTL_SECONDS, config.BOOKING_TIMEZONE,
                     config.BOOKING_TIMEZONE, booking_id),
                )
                _record_event(cur, booking_id, "payment_requested", "whatsapp")
                cur.execute("SELECT reserved_until FROM bookings WHERE id = %s", (booking_id,))
                reserved_until = cur.fetchone()["reserved_until"]
    except psycopg2.errors.ExclusionViolation:
        logger.info("[BOOKING_SERVICE] request_payment id=%d — slot taken (exclusion)", booking_id)
        return _err("SLOT_TAKEN", "К сожалению, этот слот только что заняли.")

    return _ok({"booking_id": booking_id, "reserved_until": reserved_until})


def submit_payment_proof(booking_id: int, parsed: dict | None = None,
                         proof_media_id: str | None = None) -> dict:
    """Record a validated payment receipt and confirm the booking.

    `parsed` is the receipt_parser output (bank, amount, ref, date). The receipt
    number is stored with a UNIQUE index → reused receipts return PAYMENT_DUPLICATE.
    """
    parsed = parsed or {}
    receipt_dt = None
    if parsed.get("date"):
        receipt_dt = parsed["date"].replace(tzinfo=ZoneInfo(config.BOOKING_TIMEZONE))
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, state FROM bookings WHERE id = %s FOR UPDATE", (booking_id,)
                )
                row = cur.fetchone()
                if not row:
                    return _err("NOT_FOUND", "Бронь не найдена.")
                if row["state"] == "confirmed":
                    return _ok({"booking_id": booking_id}, message="Бронь уже подтверждена.")
                if row["state"] != "awaiting_payment":
                    return _err("BOOKING_WRONG_STATE", "Эта бронь не ожидает оплаты.")

                cur.execute(
                    "INSERT INTO payments (booking_id, method, proof_media_id, bank, amount, "
                    "  transaction_ref, receipt_date, status, verified_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'accepted', NOW())",
                    (booking_id, "bank_transfer", proof_media_id, parsed.get("bank"),
                     parsed.get("amount"), parsed.get("ref"), receipt_dt),
                )
                cur.execute(
                    "UPDATE bookings SET state = 'confirmed', updated_at = NOW() WHERE id = %s",
                    (booking_id,),
                )
                _record_event(cur, booking_id, "payment_received", "whatsapp",
                              note=parsed.get("ref"))
    except psycopg2.errors.UniqueViolation:
        logger.warning("[BOOKING_SERVICE] duplicate receipt ref=%s for booking %d",
                       parsed.get("ref"), booking_id)
        return _err("PAYMENT_DUPLICATE", "Этот чек уже был использован.")
    return _ok({"booking_id": booking_id})


def reject_payment(booking_id: int, reason: str, parsed: dict | None = None) -> dict:
    """Log a rejected receipt attempt without changing booking state.

    The booking stays AWAITING_PAYMENT so the user can resubmit a valid receipt
    within the reservation TTL. Reaching FAILED is reserved for manager action
    or repeated abuse (not a single honest mistake)."""
    parsed = parsed or {}
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, state FROM bookings WHERE id = %s FOR UPDATE", (booking_id,)
            )
            row = cur.fetchone()
            if not row:
                return _err("NOT_FOUND", "Бронь не найдена.")
            cur.execute(
                "INSERT INTO payments (booking_id, bank, amount, status, reject_reason, created_at) "
                "VALUES (%s, %s, %s, 'rejected', %s, NOW())",
                (booking_id, parsed.get("bank"), parsed.get("amount"), reason),
            )
            _record_event(cur, booking_id, "payment_rejected", "whatsapp", note=reason)
    return _ok({"booking_id": booking_id})


def cancel_booking(booking_id: int, actor_type: str = "whatsapp",
                   actor_id: str | None = None, reason: str | None = None) -> dict:
    """Cancel a booking (DRAFT or AWAITING_PAYMENT or CONFIRMED). Releases the slot
    and clears any conversation session still referencing it."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "UPDATE bookings SET state = 'cancelled', updated_at = NOW() "
                "WHERE id = %s AND state NOT IN ('cancelled', 'failed') RETURNING id",
                (booking_id,),
            )
            if cur.fetchone():
                _record_event(cur, booking_id, "cancelled", actor_type, actor_id, reason)
                cur.execute(
                    "DELETE FROM booking_sessions WHERE booking_id = %s", (booking_id,)
                )
                return _ok({"booking_id": booking_id})
            cur.execute("SELECT state FROM bookings WHERE id = %s", (booking_id,))
            row = cur.fetchone()
    if not row:
        return _err("NOT_FOUND", "Бронь не найдена.")
    return _ok({"booking_id": booking_id}, message="Бронь уже была отменена.")


_MANAGER_PATCH_FIELDS = {"customer_name", "notes", "price_total"}


def manager_update_booking(booking_id: int, actor_id: str | None = None, **fields) -> dict:
    """Manager edit of free-edit fields (customer_name, notes, price_total)."""
    patch = {k: v for k, v in fields.items() if k in _MANAGER_PATCH_FIELDS}
    if not patch:
        return _ok({"booking_id": booking_id})
    set_clause = ", ".join(f"{k} = %s" for k in patch) + ", updated_at = NOW()"
    vals = list(patch.values()) + [booking_id]
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"UPDATE bookings SET {set_clause} WHERE id = %s RETURNING id", vals
            )
            if cur.fetchone():
                _record_event(cur, booking_id, "manager_updated", "manager", actor_id)
                return _ok({"booking_id": booking_id})
    return _err("NOT_FOUND", "Бронь не найдена.")


def manager_create_booking(field: int, date: str, time_start: str, time_end: str,
                           end_date: str, repeat: str = 'none',
                           customer: str | None = None, phone: str | None = None,
                           notes: str | None = None, price_total=None,
                           actor_id: str | None = None,
                           client_token: str | None = None,
                           format_: str | None = None) -> dict:
    """Manager-created booking: DRAFT is skipped, goes straight to CONFIRMED."""
    if format_ is None:
        conf = next((f for f in config.BOOKING_FIELDS if f["id"] == int(field)), None)
        format_ = conf["format"] if conf else ""
    try:
        dates = list(generate_dates(date, end_date, repeat))
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                for d in dates:
                    d_str = datetime.strftime(d, format="%Y-%m-%d")
                    client_token = str(uuid.uuid4())
                    cur.execute(
                        "INSERT INTO bookings "
                        "  (phone, customer_name, date, time_start, time_end, field, format, "
                        "   notes, price_total, state, source, client_token, start_at, end_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'confirmed', 'manager', "
                        "        COALESCE(%s, gen_random_uuid()), "
                        "        (%s::date + %s::time) AT TIME ZONE %s, "
                        "        (%s::date + %s::time) AT TIME ZONE %s) "
                        "RETURNING id",
                        (phone, customer, d_str, time_start, time_end, int(field), format_,
                         notes, price_total, client_token,
                         d_str, time_start, config.BOOKING_TIMEZONE,
                         d_str, time_end, config.BOOKING_TIMEZONE),
                    )
                    booking_id = cur.fetchone()["id"]
                    _record_event(cur, booking_id, "manager_created", "manager", actor_id)
    except psycopg2.errors.ExclusionViolation:
        return _err("SLOT_TAKEN", "Это поле уже забронировано на это время.")
    except psycopg2.errors.UniqueViolation:
        # Idempotent retry on the same client_token.
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT id FROM bookings WHERE client_token = %s", (client_token,))
                row = cur.fetchone()
        if row:
            return _ok({"booking_id": row["id"], "status": "CONFIRMED"})
        raise
    return _ok({"booking_id": booking_id, "status": "CONFIRMED"})


def generate_dates(start_date, end_date, repeat):
    current = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")

    while current <= end:
        yield current
        if repeat == "none":
            break
        elif repeat == "daily":
            current += timedelta(days=1)
        elif repeat == "weekly":
            current += timedelta(weeks=1)
        elif repeat == "monthly":
            current += relativedelta(months=1)
        else:
            raise ValueError("Unknown repeat type")
