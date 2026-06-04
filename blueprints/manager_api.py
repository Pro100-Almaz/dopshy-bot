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
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from flask import Blueprint, jsonify, request

import config
from integrations import booking_service, postgres, sheets
from integrations.sheets import refresh_week_sheet

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
    rows = postgres.get_bookings_in_range(
        start, end, states=("draft", "awaiting_payment", "confirmed")
    )
    return jsonify({"ok": True, "data": [_serialize(r) for r in rows]}), 200


@manager_api.get("/api/manager/bookings/<int:booking_id>")
def get_booking(booking_id: int):
    row = postgres.get_booking(booking_id)
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
        booking_row = postgres.get_booking(res["data"]["booking_id"])
        if booking_row:
            sheets.upsert_booking_row(booking_row)
        refresh_week_sheet()

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
    res = booking_service.manager_update_booking(booking_id, actor_id=_api_key_actor(), **patch)

    if res["ok"]:
        booking_row = postgres.get_booking(booking_id)
        if booking_row:
            sheets.upsert_booking_row(booking_row)

    refresh_week_sheet()

    return jsonify(res), (200 if res["ok"] else 404)


@manager_api.delete("/api/manager/bookings/<int:booking_id>")
def delete_booking(booking_id: int):
    res = booking_service.cancel_booking(
        booking_id, actor_type="manager", actor_id=_api_key_actor(), reason="manager_cancel"
    )
    if res["ok"]:
        booking_row = postgres.get_booking(booking_id)
        if booking_row:
            sheets.upsert_booking_row(booking_row)

    refresh_week_sheet()
    return jsonify(res), (200 if res["ok"] else 404)


@manager_api.post("/api/manager/bookings/daily_refresh")
def daily_refresh():
    refresh_week_sheet()
    return jsonify({"ok": True}), 200