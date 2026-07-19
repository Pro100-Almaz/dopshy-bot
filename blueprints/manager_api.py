"""Manager API blueprint — endpoints the Google Apps Script manager UI calls.

Auth: X-API-Key header matched against config.X_SERVICE_TOKEN.
Rate limit: config.MANAGER_RATE_LIMIT requests/min per client IP.

All responses use the service envelope: {"ok": bool, "data"/"code"/"message"}.

POST body contract (simpler than spec-02's ISO form; the Apps Script in this
repo emits it):
    {field: int, date: "YYYY-MM-DD", time_start: "HH:MM", time_end: "HH:MM",
     customer: str, notes: str, client_token: str}
"""

import logging
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from flask import Blueprint, jsonify, request

import config
from integrations import booking_service
from integrations.repo.academy_repo import deactivate_group_repo, setting_training_time, get_group_by_id, \
    create_or_update_group, on_manual_group_edit
from integrations.sheets.booking_sheets import refresh_week_sheet, _single_table_write, _single_table_erase, \
    upsert_booking_row
from integrations.repo import booking_repo as repo, postgres
from integrations.repo.bot_pause_repo import get_statuses, normalize_phone, set_bot_paused
from chat.conversation import list_contacts as _list_conversation_contacts
from integrations.sheets.trial_sheets import refresh_all_trials, refresh_all_groups

logger = logging.getLogger(__name__)

manager_api = Blueprint("manager_api", __name__)

# States in which a booking no longer occupies a slot on the week sheet.
_TERMINAL_BOOKING_STATES = {"cancelled", "unpaid", "failed"}

_rate_lock = threading.Lock()
_rate_hits: dict[str, list[float]] = {}


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(ip, []) if now - t < 60]
        if len(hits) >= config.MANAGER_RATE_LIMIT:
            _rate_hits[ip] = hits
            return True
        hits.append(now)
        _rate_hits[ip] = hits
    return False


def _serialize(b: dict) -> dict:
    out = dict(b)
    for k, v in out.items():
        if isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, uuid.UUID):  # e.g. group_transition
            out[k] = str(v)
        elif hasattr(v, "isoformat"):  # time
            out[k] = str(v)[:5]
        elif v is None:
            out[k] = ""
    return out


@manager_api.before_request
def _authenticate():
    if request.method == "OPTIONS":
        return None

    if not config.X_SERVICE_TOKEN:
        return jsonify({"ok": False, "code": "NOT_CONFIGURED",
                        "message": "Manager API is not configured."}), 503
    if request.headers.get("X-API-Key", "") != config.X_SERVICE_TOKEN:
        return jsonify({"ok": False, "code": "UNAUTHORIZED", "message": "Bad API key."}), 401
    if _rate_limited(request.remote_addr or "unknown"):
        return jsonify({"ok": False, "code": "RATE_LIMITED",
                        "message": "Too many requests."}), 429
    return None


def _combine_bookings_payments(bookings: list[dict], payments: list[dict]) -> list[dict]:
    for booking in bookings:
        booking.setdefault("last_receipt_date", None)
        for payment in payments:
            if payment['booking_id'] == booking["id"]:
                booking["paid_bot"] = payment.get("amount") or 0
                rd = payment.get("receipt_date")
                if rd and (booking["last_receipt_date"] is None or rd > booking["last_receipt_date"]):
                    booking["last_receipt_date"] = rd
        for p in ("paid_kaspi_qr", "paid_cash", "paid_avans"):
            booking[p] = booking.get(p) or 0
    return bookings


def _api_key_actor() -> str:
    return "manager:" + (config.X_SERVICE_TOKEN[:6] if config.X_SERVICE_TOKEN else "?")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@manager_api.get("/api/manager/bookings")
def list_bookings():
    today = date.today()
    start = request.args.get("from", str(today))
    end = request.args.get("to", str(today + timedelta(days=30)))
    rows = repo.get_bookings_in_range(
        start, end, states=("draft", "awaiting_payment", "confirmed", "unpaid")
    )
    payments = booking_service.get_payments()
    rows = _combine_bookings_payments(rows, payments)
    return jsonify({"ok": True, "data": [_serialize(r) for r in rows]}), 200


@manager_api.get("/api/manager/bookings/all")
def list_all_bookings():
    """Every booking (all non-terminal states), deduped, with aggregated
    payment info. Consumed by the backend's staff `GET /bookings`
    (bookings:list-all). Mirrors `list_bookings` but without a date window.
    """
    rows = repo.get_all_bookings()
    payments = booking_service.get_payments()
    rows = _combine_bookings_payments(rows, payments)
    return jsonify({"ok": True, "data": [_serialize(r) for r in rows]}), 200


@manager_api.get("/api/manager/bookings/<int:booking_id>")
def get_booking(booking_id: int):
    row = repo.get_booking(booking_id)
    if not row:
        return jsonify({"ok": False, "code": "NOT_FOUND", "message": "Бронь не найдена."}), 404
    return jsonify({"ok": True, "data": _serialize(row)}), 200


@manager_api.get("/api/manager/bookings/range/<string:start_date>/<string:end_date>/<int:field>")
def get_bookings_in_range(start_date: str, end_date: str, field: int):
    rows = repo.get_bookings_in_range(
        start_date, end_date, states=("draft", "awaiting_payment", "confirmed", "unpaid"), field=field
    )
    payments = booking_service.get_payments()
    rows = _combine_bookings_payments(rows, payments)
    return jsonify({
        "ok": True,
        "data": [_serialize(r) for r in rows]
    }), 200


@manager_api.get("/api/manager/fields")
def get_fields_info():
    prices = repo.get_field_prices()  # list of BotPriceRow
    fields = repo.get_fields_info()   # list of BotFieldRow
    return jsonify({"ok": True, "data": {
        "prices": [_serialize(p) for p in prices],
        "fields": [_serialize(f) for f in fields],
    }}), 200


@manager_api.post("/api/manager/bookings")
def create_booking():
    body = request.get_json(silent=True) or {}
    required = ("field", "date", "time_start", "time_end")
    repeat = body.get("repeat", "none")
    end_date = body.get("end_date")
    if not all(body.get(k) for k in required) or (repeat != "none" and not end_date):
        return jsonify({"ok": False, "code": "INVALID",
                        "message": "field, date, time_start, time_end are required."}), 400

    res = booking_service.manager_create_booking(
        field=int(body["field"]),
        repeat=repeat,
        date=body["date"],
        end_date=end_date or body["date"],
        time_start=body["time_start"],
        time_end=body["time_end"],
        customer=body.get("customer"),
        phone=body.get("phone"),
        notes=body.get("notes"),
        price_total=body.get("price_total"),
        client_token=body.get("client_token"),
        actor_id=_api_key_actor(),
        reserved_until=body.get("reserved_until"),
        updated_by=body.get("updated_by", "Неизвестен")
    )

    if res["ok"] and res.get("data", {}).get("booking_id"):
        booking_row = repo.get_booking(res["data"]["booking_id"])
        _single_table_write(booking_row)

    return jsonify(res), (200 if res["ok"] else 409)


@manager_api.post("/api/manager/bookings/batch")
def create_bookings_batch():
    """Create bookings for a single customer from a list of slots.

    Body: {slots: [{field, date, time_start, time_end}, ...],
           customer?, phone?, notes?, price_total?, reserved_until?, updated_by?}
    Overlapping/adjacent slots on the same field are merged (across midnight
    too) before creation. See booking_service.manager_create_bookings_batch.
    """
    body = request.get_json(silent=True) or {}
    slots = body.get("slots")
    if not isinstance(slots, list) or not slots:
        return jsonify({"ok": False, "code": "INVALID",
                        "message": "slots must be a non-empty list."}), 400
    for s in slots:
        if not isinstance(s, dict) or not all(s.get(k) for k in ("field", "date", "time_start", "time_end")):
            return jsonify({"ok": False, "code": "INVALID",
                            "message": "each slot needs field, date, time_start, time_end."}), 400

    res = booking_service.manager_create_bookings_batch(
        slots,
        customer=body.get("customer"),
        phone=body.get("phone"),
        notes=body.get("notes"),
        price_total=body.get("price_total"),
        prepayment=body.get("prepayment"),
        actor_id=_api_key_actor(),
        reserved_until=body.get("reserved_until", 30),
        updated_by=body.get("updated_by", "Неизвестен"),
    )

    if res["ok"]:
        for r in res.get("data", {}).get("created", []):
            if r.get("booking_id"):
                booking_row = repo.get_booking(r["booking_id"])
                _single_table_write(booking_row)

    return jsonify(res), (200 if res["ok"] else 409)


@manager_api.patch("/api/manager/bookings/<int:booking_id>")
def patch_booking(booking_id: int):
    body = request.get_json(silent=True) or {}
    patch = {}
    if "source" in body:
        patch["source"] = body["source"]
    if "customer" in body:
        patch["customer_name"] = body["customer"]
    if "customer_name" in body:
        patch["customer_name"] = body["customer_name"]
    if "notes" in body:
        patch["notes"] = body["notes"]
    if "price_total" in body:
        patch["price_total"] = body["price_total"]
    if "status" in body:
        patch["state"] = body["status"]
    if "paid_kaspi_qr" in body:
        patch["paid_kaspi_qr"] = body["paid_kaspi_qr"]
    if "paid_cash" in body:
        patch["paid_cash"] = body["paid_cash"]
    if "paid_avans" in body:
        patch["paid_avans"] = body["paid_avans"]
    if "time_start" in body:
        patch["time_start"] = body["time_start"]
    if "time_end" in body:
        patch["time_end"] = body["time_end"]
    if "date" in body:
        patch["date"] = body["date"]
    if "end_date" in body:
        patch["end_date"] = body["end_date"]
    if "field_id" in body:
        patch["field"] = body["field_id"]
    if "updated_by" in body:
        patch["updated_by"] = body["updated_by"]

    res = booking_service.manager_update_booking(booking_id, actor_id=_api_key_actor(), **patch)

    if res["ok"]:
        booking_row = repo.get_booking(booking_id)
        if booking_row:
            upsert_booking_row(booking_row)
            # Sheet is a best-effort view; never fail the committed DB mutation on it.
            try:
                if booking_row.get("state") in _TERMINAL_BOOKING_STATES:
                    # Booking left the board. Rebuild the week sheet so this row —
                    # and any transitive partner the DB trigger moved to the same
                    # state — disappear, rather than repainting it as an active slot
                    # (which would also leave a stale merge behind).
                    refresh_week_sheet()
                else:
                    _single_table_write(booking_row)
            except Exception as exc:
                logger.error("[MANAGER_API] sheet sync failed for booking %s: %s", booking_id, exc)

    return jsonify(res), (200 if res["ok"] else 404)


@manager_api.delete("/api/manager/bookings/<int:booking_id>")
def delete_booking(object_id: int):
    res = postgres.cancel_booking_trial(bot_name="dopsy_bot",
        object_id=object_id, actor_type="manager", actor_id=_api_key_actor(), reason="manager_cancel"
    )
    if res["ok"]:
        booking_row = repo.get_booking(object_id)
        if booking_row:
            upsert_booking_row(booking_row)
        _single_table_erase(booking_row)

    return jsonify(res), (200 if res["ok"] else 404)

@manager_api.delete("/api/manager/bookings/all/<int:booking_id>")
def delete_repetitive_booking(booking_id: int):
    res = booking_service.cancel_all_bookings(
        booking_id, actor_type="manager", actor_id=_api_key_actor(), reason="manager_cancel"
    )
    if res["ok"]:
        booking_row = repo.get_booking(booking_id)
        if booking_row:
            upsert_booking_row(booking_row)

    refresh_week_sheet()
    return jsonify(res), (200 if res["ok"] else 404)

@manager_api.post("/api/manager/bookings/daily_refresh")
def daily_refresh():
    refresh_week_sheet()
    return jsonify({"ok": True}), 200

# --------------GROUPS

@manager_api.post("/api/manager/academy_groups/refresh_all")
def refresh_academy_groups():
    refresh_all_groups()
    return jsonify({"ok": True}), 200


@manager_api.post("/api/manager/academy_groups")
def create_academy_group_with_time():
    # make current capacity 0 by default
    # this function should receive the payload --> create a grouping and schedule row. Schedule should reveive
    # this group's id as a Foreign Key

    body = request.get_json(silent=True) or {}
    required = ("group_type", "group_name",  "time_start", "time_end", "training_day", "max_cap")

    if not all(body.get(k) for k in required):
        return jsonify({"ok": False, "code": "INVALID",
                        "message": "group_type, group_name, max_cap, training_day, time_start, time_end are required."}), 400

    training_day = int(body["training_day"])
    time_start = body["time_start"]
    time_end = body["time_end"]

    group_id = create_or_update_group(
        group_name = body['group_name'],
        group_type = body['group_type'],
        max_cap = body['max_cap'],
        is_active = body.get('is_active', True)
    )

    if not group_id:
        return jsonify({
            "ok" : False,
            'code': 'CREATE_FAILED',
            'message': "Could not create group."
        }), 409

    scheduled_time_id = setting_training_time(group_id, training_day, time_start, time_end)

    group_row = get_group_by_id(group_id)
    if group_row:
        group_row['training_day'] = training_day
        group_row['time_start'] = time_start
        group_row['time_end'] = time_end

    refresh_all_groups()

    return jsonify({
        'ok' : True,
        'data' : {
            'group_id' : group_id,
            "schedule_id": scheduled_time_id
        }
    }), 201


@manager_api.patch("/api/manager/academy_groups/<int:group_id>")
def edit_academy_group(group_id: int):
    body = request.get_json(silent=True) or {}

    max_cap = body.get("max_cap")
    group_name = body.get("group_name")

    if max_cap is not None:
        max_cap = int(max_cap)

    res = on_manual_group_edit(
        group_id=group_id,
        group_name=str(group_name),
        max_cap=max_cap
    )

    if res["ok"]:
        refresh_all_groups()
    return jsonify(res), 200 if res["ok"] else 404


@manager_api.post("/api/manager/academy_groups/<int:group_id>")
def delete_academy_group(group_id: int):
    res = deactivate_group_repo(group_id)
    if res["ok"]:
        refresh_all_groups()

    return jsonify(res), (200 if res["ok"] else 404)

# ------------TRIALS

@manager_api.get("/api/manager/academy_trials/refresh_all_trials")
def refresh_academy_trials():
    refresh_all_trials()
    return jsonify({"ok": True}), 200


# ------------BOT PAUSE (per-contact on/off switch)

@manager_api.get("/api/manager/bot_status/<string:phone>")
def bot_status(phone: str):
    status = get_statuses([phone])[phone]
    return jsonify({"phone": phone, **status}), 200


@manager_api.post("/api/manager/bot_status/batch")
def bot_status_batch():
    body = request.get_json(silent=True) or {}
    phones = body.get("phones")
    if not isinstance(phones, list):
        return jsonify({"ok": False, "code": "INVALID",
                        "message": "phones must be a list."}), 400
    return jsonify({"statuses": get_statuses(phones)}), 200


@manager_api.post("/api/manager/bot_status/<string:phone>/pause")
def bot_pause(phone: str):
    set_bot_paused(phone, True, reason="manual", paused_by=_api_key_actor())
    return jsonify({"phone": phone, "paused": True}), 200


@manager_api.post("/api/manager/bot_status/<string:phone>/resume")
def bot_resume(phone: str):
    set_bot_paused(phone, False, reason="manual", paused_by=_api_key_actor())
    return jsonify({"phone": phone, "paused": False}), 200


# ------------CONTACTS (unified customer list — WhatsApp texters + bookers)

@manager_api.get("/api/manager/contacts")
def list_contacts():
    """Every customer the bot knows, deduped by normalized phone.

    Merges two sources so the manager UI can see WhatsApp texters who have not
    booked yet (the primary case) alongside booking customers:
      - conversations (SQLite): anyone who has messaged the bot
      - bookings (Postgres): anyone with a booking on record
    Each contact carries its live pause status so the UI can render the toggle
    without a second round trip.
    """
    contacts: dict[str, dict] = {}

    def _touch(phone: str) -> dict | None:
        key = normalize_phone(phone)
        if not key:
            return None
        entry = contacts.get(key)
        if entry is None:
            entry = {
                "phone": key,
                "name": "",
                "texted": False,
                "has_booking": False,
                "last_activity": None,
            }
            contacts[key] = entry
        return entry

    def _bump_activity(entry: dict, ts) -> None:
        if not ts:
            return
        ts = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        if entry["last_activity"] is None or ts > entry["last_activity"]:
            entry["last_activity"] = ts

    # WhatsApp texters — sender phone is the part after the last ':' in chat_id.
    for row in _list_conversation_contacts():
        sender = str(row.get("chat_id", "")).rsplit(":", 1)[-1]
        entry = _touch(sender)
        if entry is None:
            continue
        entry["texted"] = True
        _bump_activity(entry, row.get("updated_at"))

    # Booking customers.
    for row in repo.get_booking_customers():
        entry = _touch(row.get("phone"))
        if entry is None:
            continue
        entry["has_booking"] = True
        if not entry["name"] and row.get("customer_name"):
            entry["name"] = row["customer_name"]
        _bump_activity(entry, row.get("last_at"))

    statuses = get_statuses(list(contacts.keys()))
    result = []
    for key, entry in contacts.items():
        status = statuses.get(key, {"paused": False, "paused_reason": None})
        entry["paused"] = status["paused"]
        entry["paused_reason"] = status["paused_reason"]
        result.append(entry)

    result.sort(key=lambda c: (c["last_activity"] or ""), reverse=True)
    return result