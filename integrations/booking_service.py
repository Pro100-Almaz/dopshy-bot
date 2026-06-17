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
from integrations.repo.utils import _conn, _err, _ok

logger = logging.getLogger(__name__)



# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------

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


def request_payment(booking_id: int, client_token: str) -> dict:
    """Transition DRAFT → AWAITING_PAYMENT: reserve the slot and start the TTL.

    The EXCLUDE constraint atomically rejects a slot already held by another
    awaiting_payment/confirmed booking → SLOT_TAKEN.

    TRANSITIVE BOOKING: if time_start > time_end (day transition, e.g. 23:00→01:00),
    the draft is split into two bookings linked by group_transition UUID:
      - first:  date, time_start → 23:59:59 (with reserved_until)
      - second: date+1, 00:00 → original time_end (NO reserved_until)
    Both are transitioned to awaiting_payment atomically.
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

                # TRANSITIVE BOOKING: split if time_start > time_end (day transition)
                is_transitive = row["time_start"] > row["time_end"]
                if is_transitive:
                    group_transition = str(uuid.uuid4())
                    original_time_end = row["time_end"]
                    next_day = row["date"] + timedelta(days=1)

                    # Update first booking: end at 23:59:59, set group_transition
                    cur.execute(
                        "UPDATE bookings SET "
                        "  time_end = '23:59:59'::time, "
                        "  group_transition = %s "
                        "WHERE id = %s",
                        (group_transition, booking_id),
                    )

                    # Create second booking: 00:00 → original end, next day, NO reserved_until
                    cur.execute(
                        "INSERT INTO bookings "
                        "  (phone, customer_name, date, time_start, time_end, field, format, "
                        "   players, notes, state, source, client_token, group_transition) "
                        "SELECT phone, customer_name, %s, '00:00'::time, %s::time, "
                        "  field, format, players, notes, 'draft', source, gen_random_uuid(), %s "
                        "FROM bookings WHERE id = %s",
                        (str(next_day), str(original_time_end)[:5], group_transition, booking_id),
                    )

                # Transition to awaiting_payment — handles both normal and transitive
                # TRANSITIVE BOOKING: CASE skips reserved_until for the latter booking
                cur.execute(
                    "UPDATE bookings SET "
                    "  state = 'awaiting_payment', "
                    "  reserved_until = CASE "
                    "    WHEN id = %s THEN NOW() + make_interval(secs => %s) "
                    "    ELSE reserved_until "
                    "  END, "
                    "  start_at = (date + time_start) AT TIME ZONE %s, "
                    "  end_at   = (date + time_end)   AT TIME ZONE %s, "
                    "  price_total = (SELECT price_per_hour FROM fields WHERE id = bookings.field) "
                    "                * (EXTRACT(EPOCH FROM ((date + time_end) - (date + time_start))) / 3600.0), "
                    "  updated_at = NOW() "
                    "WHERE id = %s OR group_transition = (SELECT group_transition FROM bookings WHERE id = %s)",
                    (booking_id, config.PAYMENT_TTL_SECONDS, config.BOOKING_TIMEZONE,
                     config.BOOKING_TIMEZONE, booking_id, booking_id),
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
                    "UPDATE bookings SET state = 'confirmed', updated_at = NOW() "
                    "WHERE id = %s OR group_transition = (SELECT group_transition FROM bookings WHERE id = %s)",
                    (booking_id, booking_id,),
                )
                _record_event(cur, booking_id, "payment_received", "whatsapp",
                              note=parsed.get("ref"))
    except psycopg2.errors.UniqueViolation:
        logger.warning("[BOOKING_SERVICE] duplicate receipt ref=%s for booking %d",
                       parsed.get("ref"), booking_id)
        return _err("PAYMENT_DUPLICATE", "Этот чек уже был использован.")
    return _ok({"booking_id": booking_id})


def get_payments() -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM payments WHERE status = 'accepted'",
            )
            return [dict(r) for r in cur.fetchall()]


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


def get_payment_recipients() -> list[dict]:
    """Active acceptable payment recipients (for receipt validation)."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT bank, bin, name, phone FROM payment_recipients WHERE active = TRUE"
            )
            return [dict(r) for r in cur.fetchall()]


def cancel_all_bookings(booking_id: int, actor_type: str = "whatsapp",
                   actor_id: str | None = None, reason: str | None = None) -> dict:
    """Cancel a booking (DRAFT or AWAITING_PAYMENT or CONFIRMED). Releases the slot
    and clears any conversation session still referencing it."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "UPDATE bookings SET state = 'cancelled', updated_at = NOW() FROM bookings "
                "target WHERE target.id = %s AND "
                "(bookings.id = target.id "
                "OR bookings.group_repetition = target.group_repetition "
                "OR bookings.group_transition = target.group_transition) "
                "AND bookings.date >= target.date AND bookings.state NOT IN ('cancelled', 'failed') "
                "RETURNING bookings.id",
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

    TRANSITIVE BOOKING: if the booking is part of a transitive pair (group_transition),
    both bookings are cancelled and new ones are created. The logical time range is
    reconstructed from first.time_start + second.time_end. If the new range is also
    transitive, two new bookings are created; otherwise a single one.

    Rules:
      - state ∈ {awaiting_payment, confirmed}                  → else INVALID_STATE
      - start_at > NOW() + 48 hours                            → else EDIT_WINDOW_CLOSED
      - predecessor_booking_id IS NULL on the source row       → else ALREADY_EDITED
      - At least one field in `patch` actually changes a value → else NO_CHANGE
      - New (field, time-range) doesn't clash with another     → else SLOT_TAKEN

    On success: old row(s) → state=cancelled + client_edited_at=NOW();
    new row(s) → state preserved from old (so paid bookings stay paid),
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
                    "       group_transition, "
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

                # TRANSITIVE BOOKING: if part of a pair, find the partner
                # and reconstruct the logical time range
                paired_row = None
                logical_time_end = str(row["time_end"])[:5]
                if row["group_transition"]:
                    cur.execute(
                        "SELECT id, date, time_start, time_end FROM bookings "
                        "WHERE group_transition = %s AND id != %s FOR UPDATE",
                        (row["group_transition"], booking_id),
                    )
                    paired_row = cur.fetchone()
                    if paired_row:
                        # Second booking (later date) has the actual end time
                        if row["date"] <= paired_row["date"]:
                            logical_time_end = str(paired_row["time_end"])[:5]
                        else:
                            logical_time_end = str(row["time_end"])[:5]

                # Merge the diff onto the current row to compute the new slot.
                new_date    = diff.get("date",          str(row["date"]))
                new_ts      = diff.get("time_start",    str(row["time_start"])[:5])
                # TRANSITIVE BOOKING: use logical end time (from second booking) as the base
                new_te      = diff.get("time_end",      logical_time_end)
                new_field   = int(diff.get("field",     row["field"]))
                new_players = diff.get("players",       row["players"])
                new_name    = diff.get("customer_name", row["customer_name"])

                old_snap = {
                    "date":          str(row["date"]),
                    "time_start":    str(row["time_start"])[:5],
                    "time_end":      logical_time_end,
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

                # 1) Cancel old booking(s). Drops them out of the EXCLUDE
                #    constraint's WHERE clause so new INSERTs can take the same slot.
                if paired_row:
                    # TRANSITIVE BOOKING: cancel both halves
                    cur.execute(
                        "UPDATE bookings SET state = 'cancelled', "
                        "  client_edited_at = NOW(), updated_at = NOW() "
                        "WHERE group_transition = %s",
                        (row["group_transition"],),
                    )
                else:
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

                # 2) Insert new booking(s) with state preserved.
                # TRANSITIVE BOOKING: if new range crosses midnight, create two bookings
                new_is_transitive = new_ts > new_te

                if new_is_transitive:
                    new_group_transition = str(uuid.uuid4())
                    next_day = str(datetime.strptime(new_date, "%Y-%m-%d").date() + timedelta(days=1))

                    # First booking: date, time_start → 23:59:59 (with reserved_until)
                    cur.execute(
                        "INSERT INTO bookings "
                        "  (phone, customer_name, date, time_start, time_end, field, format, "
                        "   players, notes, state, source, client_token, predecessor_booking_id, "
                        "   reserved_until, start_at, end_at, price_total, group_transition) "
                        "VALUES "
                        "  (%s, %s, %s::date, %s::time, '23:59:59'::time, %s, %s, "
                        "   %s, %s, %s, %s, gen_random_uuid(), %s, "
                        "   %s, "
                        "   (%s::date + %s::time) AT TIME ZONE %s, "
                        "   (%s::date + '23:59:59'::time) AT TIME ZONE %s, "
                        "   (SELECT price_per_hour FROM fields WHERE id = %s) "
                        "   * (EXTRACT(EPOCH FROM ('23:59:59'::time - %s::time)) / 3600.0), "
                        "   %s) "
                        "RETURNING id",
                        (row["phone"], new_name, new_date, new_ts,
                         new_field, new_format,
                         new_players, row["notes"], row["state"], row["source"], booking_id,
                         row["reserved_until"],
                         new_date, new_ts, config.BOOKING_TIMEZONE,
                         new_date, config.BOOKING_TIMEZONE,
                         new_field, new_ts,
                         new_group_transition),
                    )
                    new_id = cur.fetchone()["id"]

                    # Second booking: date+1, 00:00 → time_end (NO reserved_until)
                    cur.execute(
                        "INSERT INTO bookings "
                        "  (phone, customer_name, date, time_start, time_end, field, format, "
                        "   players, notes, state, source, client_token, "
                        "   start_at, end_at, price_total, group_transition) "
                        "VALUES "
                        "  (%s, %s, %s::date, '00:00'::time, %s::time, %s, %s, "
                        "   %s, %s, %s, %s, gen_random_uuid(), "
                        "   (%s::date + '00:00'::time) AT TIME ZONE %s, "
                        "   (%s::date + %s::time) AT TIME ZONE %s, "
                        "   (SELECT price_per_hour FROM fields WHERE id = %s) "
                        "   * (EXTRACT(EPOCH FROM (%s::time - '00:00'::time)) / 3600.0), "
                        "   %s) "
                        "RETURNING id",
                        (row["phone"], new_name, next_day, new_te,
                         new_field, new_format,
                         new_players, row["notes"], row["state"], row["source"],
                         next_day, config.BOOKING_TIMEZONE,
                         next_day, new_te, config.BOOKING_TIMEZONE,
                         new_field, new_te,
                         new_group_transition),
                    )
                else:
                    # Non-transitive: single new booking (original logic)
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

                # 3) Re-point payment rows onto the new primary booking
                cur.execute(
                    "UPDATE payments SET booking_id = %s WHERE booking_id = %s",
                    (new_id, booking_id),
                )
                # TRANSITIVE BOOKING: also re-point payments from the paired booking
                if paired_row:
                    cur.execute(
                        "UPDATE payments SET booking_id = %s WHERE booking_id = %s",
                        (new_id, paired_row["id"]),
                    )

                cur.execute(
                    "SELECT id, phone, customer_name, date, time_start, time_end, "
                    "       field, format, players, state, price_total "
                    "FROM bookings WHERE id = %s",
                    (new_id,),
                )
                new_booking = dict(cur.fetchone())
                # TRANSITIVE BOOKING: show logical end time in the response, not 23:59:59
                if new_is_transitive:
                    new_booking["time_end"] = new_te

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


_MANAGER_PATCH_FIELDS = {"customer_name", "notes", "price_total", "state", "source",}


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
                f"UPDATE bookings SET {set_clause} WHERE id = %s AND state NOT IN ('draft') RETURNING id", vals
            )
            if cur.fetchone():
                _record_event(cur, booking_id, "manager_updated", fields.get("source", "manager"), actor_id)
                return _ok({"booking_id": booking_id})
    return _err("NOT_FOUND", "Бронь не найдена.")


def manager_create_booking(field: int, date: str, time_start: str, time_end: str,
                           end_date: str, repeat: str = 'none',
                           customer: str | None = None, phone: str | None = None,
                           notes: str | None = None, price_total=None,
                           actor_id: str | None = None,
                           client_token: str | None = None,
                           format_: str | None = None, reserved_until: int = 30,
                           updated_by: str = 'manager') -> dict:
    """Manager-created booking: DRAFT is skipped, goes straight to CONFIRMED."""
    if format_ is None:
        conf = next((f for f in config.BOOKING_FIELDS if f["id"] == int(field)), None)
        format_ = conf["format"] if conf else ""
    try:
        dates = list(generate_dates(date, end_date, time_start, time_end, repeat))
        is_repetitive = repeat != "none"
        group_repetition = str(uuid.uuid4())
        exec_string = """INSERT INTO bookings
                         (phone, customer_name, date, time_start, time_end, field, format,
                          notes, price_total, state, source, client_token, start_at, end_at,
                          group_repetition, group_transition, repeat, reserved_until)
                         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'awaiting_payment', %s,
                                 COALESCE(%s, gen_random_uuid()),
                                 (%s::date + %s::time) AT TIME ZONE %s,
                                 (%s::date + %s::time) AT TIME ZONE %s,
                                 %s, %s, %s,
                                 CASE
                                     WHEN %s THEN NULL
                                     ELSE NOW() + make_interval(mins => %s)
                                     END)
                         RETURNING id"""

        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                for d, st, et, group_transition, is_latter in dates:
                    d_str = datetime.strftime(d, format="%Y-%m-%d")
                    # TRANSITIVE BOOKING: skip reserved_until for the latter booking
                    skip_reserved = is_repetitive or is_latter
                    cur.execute(
                        exec_string,
                        (phone, customer, d_str, st, et, int(field), format_,
                         notes, price_total, updated_by, client_token,
                         d_str, st, config.BOOKING_TIMEZONE,
                         d_str, et, config.BOOKING_TIMEZONE,
                         group_repetition, group_transition,
                         is_repetitive, skip_reserved, reserved_until),
                    )
                    client_token = str(uuid.uuid4())
                    booking_id = cur.fetchone()["id"]
                    _record_event(cur, booking_id, "manager_created", updated_by, actor_id)
    except psycopg2.errors.ExclusionViolation:
        return _err("SLOT_TAKEN", "Это поле уже забронировано на это время.")
    except psycopg2.errors.UniqueViolation:
        # Idempotent retry on the same client_token.
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT id FROM bookings WHERE client_token = %s", (client_token,))
                row = cur.fetchone()
        if row:
            return _ok({"booking_id": row["id"], "status": "ОЖИДАНИЕ"})
        raise
    return _ok({"booking_id": booking_id, "status": "ОЖИДАНИЕ"})


def generate_dates(start_date, end_date, start_time, end_time, repeat):
    """Yield (date, start_time, end_time, group_transition, is_latter) tuples.

    TRANSITIVE BOOKING: when start_time > end_time (day transition), yields two
    entries per iteration — first half (start→23:59:59, is_latter=False) and
    second half (00:00→end, is_latter=True). The latter booking should not get
    a reserved_until value since payments are checked on the former.
    """
    current = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    transitive = (datetime.strptime(start_time, "%H:%M") > datetime.strptime(end_time, "%H:%M"))

    while current <= end:
        group_transition = str(uuid.uuid4())
        if transitive:
            yield current, start_time, "23:59:59", group_transition, False
            yield current + timedelta(days=1), "00:00", end_time, group_transition, True
        else:
            yield current, start_time, end_time, group_transition, False

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
