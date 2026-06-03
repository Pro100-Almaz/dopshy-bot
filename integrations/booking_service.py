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

import json
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


# Fields a client may patch via the self-service edit flow.
_CLIENT_EDIT_FIELDS = {"date", "time_start", "time_end", "field", "players", "customer_name"}
# Edits closer than this to start_at must go through a manager.
_CLIENT_EDIT_WINDOW_HOURS = 48


def client_edit_booking(booking_id: int, actor_id: str | None = None, **patch) -> dict:
    """Client self-service edit of a future awaiting_payment / confirmed booking.

    Implemented as cancel-old + insert-new in one transaction so the existing
    `bookings_no_overlap` EXCLUDE constraint catches slot clashes atomically.

    Rules:
      - state ∈ {awaiting_payment, confirmed}                  → else INVALID_STATE
      - start_at > NOW() + 48 hours                            → else EDIT_WINDOW_CLOSED
      - predecessor_booking_id IS NULL on the source row       → else ALREADY_EDITED
      - At least one field in `patch` actually changes a value → else NO_CHANGE
      - New (field, time-range) doesn't clash with another     → else SLOT_TAKEN

    On success: old row → state=cancelled + client_edited_at=NOW();
    new row → state preserved from old (so paid bookings stay paid),
    predecessor_booking_id = old.id, payment rows re-pointed to the new id,
    price_total recomputed from the new (field × duration).
    """
    diff = {k: v for k, v in patch.items() if k in _CLIENT_EDIT_FIELDS and v is not None}
    if not diff:
        return _err("NO_CHANGE", "Не указано, что именно изменить.")

    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, phone, date, time_start, time_end, field, format, "
                    "       players, customer_name, notes, state, price_total, "
                    "       reserved_until, source, predecessor_booking_id, start_at, "
                    "       start_at > NOW() + make_interval(hours => %s) AS in_window "
                    "FROM bookings WHERE id = %s FOR UPDATE",
                    (_CLIENT_EDIT_WINDOW_HOURS, booking_id),
                )
                row = cur.fetchone()
                if not row:
                    return _err("NOT_FOUND", "Бронь не найдена.")
                if row["state"] not in ("awaiting_payment", "confirmed"):
                    return _err("INVALID_STATE", "Эту бронь уже нельзя изменить.")
                if not row["in_window"]:
                    return _err(
                        "EDIT_WINDOW_CLOSED",
                        "До игры меньше 48 часов — изменить может только администратор.",
                    )
                if row["predecessor_booking_id"] is not None:
                    return _err(
                        "ALREADY_EDITED",
                        "Эту бронь уже один раз меняли. Следующее изменение — через администратора.",
                    )

                # Merge the diff onto the current row to compute the new slot.
                new_date    = diff.get("date",          str(row["date"]))
                new_ts      = diff.get("time_start",    str(row["time_start"])[:5])
                new_te      = diff.get("time_end",      str(row["time_end"])[:5])
                new_field   = int(diff.get("field",     row["field"]))
                new_players = diff.get("players",       row["players"])
                new_name    = diff.get("customer_name", row["customer_name"])

                old_snap = {
                    "date":          str(row["date"]),
                    "time_start":    str(row["time_start"])[:5],
                    "time_end":      str(row["time_end"])[:5],
                    "field":         row["field"],
                    "players":       row["players"],
                    "customer_name": row["customer_name"],
                }
                new_snap = {
                    "date":          new_date,
                    "time_start":    new_ts,
                    "time_end":      new_te,
                    "field":         new_field,
                    "players":       new_players,
                    "customer_name": new_name,
                }
                if old_snap == new_snap:
                    return _err("NO_CHANGE", "Детали брони не изменились.")

                # Format lookup for the (possibly new) field.
                conf = next((f for f in config.BOOKING_FIELDS if f["id"] == new_field), None)
                new_format = conf["format"] if conf else row["format"]

                # 1) Cancel the old row. This drops it out of the EXCLUDE
                #    constraint's WHERE clause (which is scoped to
                #    awaiting_payment + confirmed) so the new INSERT can take
                #    the same slot — unless a third booking already holds it.
                cur.execute(
                    "UPDATE bookings SET state = 'cancelled', "
                    "  client_edited_at = NOW(), updated_at = NOW() "
                    "WHERE id = %s",
                    (booking_id,),
                )
                _record_event(
                    cur, booking_id, "client_edit_cancelled", "whatsapp", actor_id,
                    note=json.dumps({"from": old_snap, "to": new_snap},
                                    ensure_ascii=False, default=str),
                )

                # 2) Insert the new row with state preserved (confirmed →
                #    confirmed, awaiting_payment → awaiting_payment with the
                #    original reserved_until). price_total is recomputed using
                #    the same formula as request_payment so a field-format
                #    change is priced correctly.
                cur.execute(
                    "INSERT INTO bookings "
                    "  (phone, customer_name, date, time_start, time_end, field, format, "
                    "   players, notes, state, source, client_token, predecessor_booking_id, "
                    "   reserved_until, start_at, end_at, price_total) "
                    "VALUES "
                    "  (%s, %s, %s::date, %s::time, %s::time, %s, %s, "
                    "   %s, %s, %s, %s, gen_random_uuid(), %s, "
                    "   %s, "
                    "   (%s::date + %s::time) AT TIME ZONE %s, "
                    "   (%s::date + %s::time) AT TIME ZONE %s, "
                    "   (SELECT price_per_hour FROM fields WHERE id = %s) "
                    "   * (EXTRACT(EPOCH FROM (%s::time - %s::time)) / 3600.0)) "
                    "RETURNING id",
                    (row["phone"], new_name, new_date, new_ts, new_te,
                     new_field, new_format,
                     new_players, row["notes"], row["state"], row["source"], booking_id,
                     row["reserved_until"],
                     new_date, new_ts, config.BOOKING_TIMEZONE,
                     new_date, new_te, config.BOOKING_TIMEZONE,
                     new_field, new_te, new_ts),
                )
                new_id = cur.fetchone()["id"]
                _record_event(
                    cur, new_id, "client_edited", "whatsapp", actor_id,
                    note=json.dumps({"from": old_snap, "to": new_snap,
                                     "predecessor_id": booking_id},
                                    ensure_ascii=False, default=str),
                )

                # 3) Re-point payment rows onto the new booking so a confirmed
                #    edit keeps its payment linkage and the receipt UNIQUE
                #    index still protects against re-use of the same receipt.
                cur.execute(
                    "UPDATE payments SET booking_id = %s WHERE booking_id = %s",
                    (new_id, booking_id),
                )

                cur.execute(
                    "SELECT id, phone, customer_name, date, time_start, time_end, "
                    "       field, format, players, state, price_total "
                    "FROM bookings WHERE id = %s",
                    (new_id,),
                )
                new_booking = dict(cur.fetchone())

    except psycopg2.errors.ExclusionViolation:
        logger.info("[BOOKING_SERVICE] client_edit_booking id=%d — new slot taken", booking_id)
        return _err("SLOT_TAKEN", "Это время уже занято — выберите другое.")

    return _ok({
        "booking_id":     new_id,
        "predecessor_id": booking_id,
        "from":           old_snap,
        "to":             new_snap,
        "new_booking":    new_booking,
    })


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
                    client_token = str(uuid.uuid4())
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
