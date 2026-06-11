"""Manager API blueprint — endpoints the Google Apps Script manager UI calls.

Auth: X-API-Key header matched against config.MANAGER_API_KEY.
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
from integrations.sheets.trial_sheets import refresh_all_trials, refresh_all_groups

logger = logging.getLogger(__name__)

manager_api = Blueprint("manager_api", __name__)

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
        elif hasattr(v, "isoformat"):  # time
            out[k] = str(v)[:5]
    return out


@manager_api.before_request
def _authenticate():
    if not config.MANAGER_API_KEY:
        return jsonify({"ok": False, "code": "NOT_CONFIGURED",
                        "message": "Manager API is not configured."}), 503
    if request.headers.get("X-API-Key", "") != config.MANAGER_API_KEY:
        return jsonify({"ok": False, "code": "UNAUTHORIZED", "message": "Bad API key."}), 401
    if _rate_limited(request.remote_addr or "unknown"):
        return jsonify({"ok": False, "code": "RATE_LIMITED",
                        "message": "Too many requests."}), 429
    return None


def _api_key_actor() -> str:
    return "manager:" + (config.MANAGER_API_KEY[:6] if config.MANAGER_API_KEY else "?")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@manager_api.get("/api/manager/bookings")
def list_bookings():
    today = date.today()
    start = request.args.get("from", str(today))
    end = request.args.get("to", str(today + timedelta(days=30)))
    rows = repo.get_bookings_in_range(
        start, end, states=("draft", "awaiting_payment", "confirmed")
    )
    return jsonify({"ok": True, "data": [_serialize(r) for r in rows]}), 200


@manager_api.get("/api/manager/bookings/<int:booking_id>")
def get_booking(booking_id: int):
    row = repo.get_booking(booking_id)
    if not row:
        return jsonify({"ok": False, "code": "NOT_FOUND", "message": "Бронь не найдена."}), 404
    return jsonify({"ok": True, "data": _serialize(row)}), 200


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
    )

    if res["ok"] and res.get("data", {}).get("booking_id"):
        booking_row = repo.get_booking(res["data"]["booking_id"])
        if booking_row:
            upsert_booking_row(booking_row)

        _single_table_write(booking_row)

    return jsonify(res), (200 if res["ok"] else 409)


@manager_api.patch("/api/manager/bookings/<int:booking_id>")
def patch_booking(booking_id: int):
    body = request.get_json(silent=True) or {}
    patch = {}
    if "customer" in body:
        patch["customer_name"] = body["customer"]
    if "notes" in body:
        patch["notes"] = body["notes"]
    if "price_total" in body:
        patch["price_total"] = body["price_total"]
    if "status" in body:
        patch["state"] = body["status"]
    res = booking_service.manager_update_booking(booking_id, actor_id=_api_key_actor(), **patch)

    if res["ok"]:
        booking_row = repo.get_booking(booking_id)
        if booking_row:
            upsert_booking_row(booking_row)

        _single_table_write(booking_row)

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

