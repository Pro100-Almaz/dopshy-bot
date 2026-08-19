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
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request

import config
from integrations import apipay_client, apipay_service, booking_service, client_notify
from integrations.apipay_client import ApiPayError, KaspiClientMissing
from integrations.repo.academy_repo import deactivate_group_repo, setting_training_time, get_group_by_id, \
    create_or_update_group, on_manual_group_edit, on_manual_group_schedule_edit
from integrations.sheets.booking_sheets import refresh_week_sheet, _single_table_write, _single_table_erase, \
    upsert_booking_row
from integrations.repo import booking_repo as repo, postgres, history_repo
from integrations.repo.bot_pause_repo import get_statuses, normalize_phone, set_bot_paused
from chat.conversation import list_contacts as _list_conversation_contacts
from integrations.sheets.trial_sheets import refresh_all_trials, refresh_all_groups

logger = logging.getLogger(__name__)

_LOCAL_TZ = ZoneInfo(config.BOOKING_TIMEZONE)

manager_api = Blueprint("manager_api", __name__)

# States in which a booking no longer occupies a slot on the week sheet.
_TERMINAL_BOOKING_STATES = {"cancelled", "unpaid", "failed"}

# Manager status changes the client is told about, and what they are told.
# 'failed' and 'draft' are absent on purpose: the first is our own bookkeeping
# for a booking that never became one, the second was never visible to anybody.
_STATE_NOTIFICATIONS = {
    "confirmed": "manager_confirmed",
    "cancelled": "manager_cancelled",
    "unpaid": "manager_unpaid",
}

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


def _manager_request_token() -> str:
    bearer = request.headers.get("Authorization", "")
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    return request.headers.get("X-API-Key", "")


@manager_api.before_request
def _authenticate():
    if request.method == "OPTIONS":
        return None

    if not config.X_SERVICE_TOKEN:
        return jsonify({"ok": False, "code": "NOT_CONFIGURED",
                        "message": "Manager API is not configured."}), 503
    if _manager_request_token() != config.X_SERVICE_TOKEN:
        return jsonify({"ok": False, "code": "UNAUTHORIZED", "message": "Bad API key."}), 401
    if _rate_limited(request.remote_addr or "unknown"):
        return jsonify({"ok": False, "code": "RATE_LIMITED",
                        "message": "Too many requests."}), 429
    return None


def _combine_bookings_payments(bookings: list[dict], payments: list[dict]) -> list[dict]:
    """Attach aggregated payment info to each booking row.

    `paid_api` is the SUM of every accepted payment on the booking, matched on
    payments.booking_id — WhatsApp receipts and ApiPay avans alike. A booking
    settled in several parts reports its full total, and one with no payments
    at all reports 0 rather than omitting the key.
    """
    totals: dict[int, Decimal] = {}
    latest_receipt: dict[int, datetime] = {}
    for payment in payments:
        bid = payment["booking_id"]
        totals[bid] = totals.get(bid, Decimal(0)) + Decimal(str(payment.get("amount") or 0))
        rd = payment.get("receipt_date")
        if rd and (latest_receipt.get(bid) is None or rd > latest_receipt[bid]):
            latest_receipt[bid] = rd

    for booking in bookings:
        bid = booking["id"]
        booking["paid_api"] = totals.get(bid, Decimal(0))
        booking["last_receipt_date"] = latest_receipt.get(bid)
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
        # start, end, states=("draft", "awaiting_payment", "confirmed", "unpaid")
        start, end, states=("awaiting_payment", "confirmed", "unpaid")
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
    page = request.args.get("page", type=int)
    search = request.args.get("search", type=str)
    if page is not None and page < 1:
        return jsonify({
            "ok": False,
            "code": "INVALID",
            "message": "page must be a positive integer."
        }), 400

    rows = repo.get_all_bookings(page=page, search=search)
    payments = booking_service.get_payments()
    rows = _combine_bookings_payments(rows, payments)
    return jsonify({"ok": True, "data": [_serialize(r) for r in rows]}), 200


@manager_api.get("/api/manager/bookings/<int:booking_id>")
def get_booking(booking_id: int):
    """Full detail for a single booking, with aggregated payment info.

    Mirrors the list endpoints: the raw booking row is enriched with the total
    of all accepted payments (`paid_api`), the latest receipt date
    (`last_receipt_date`) and the manual payment buckets, so a manager can see
    everything about one booking in a single round trip.
    """
    row = repo.get_booking(booking_id)
    if not row:
        return jsonify({"ok": False, "code": "NOT_FOUND", "message": "Бронь не найдена."}), 404
    payments = booking_service.get_payments()
    row = _combine_bookings_payments([row], payments)[0]
    return jsonify({"ok": True, "data": _serialize(row)}), 200


@manager_api.get("/api/manager/bookings/range/<string:start_date>/<string:end_date>")
def get_bookings_in_range(start_date: str, end_date: str):
    # field omitted from the URL → None → all fields in the range.
    page = request.args.get("page", type=int)
    field = request.args.get("field", type=int)
    search = request.args.get("search", type=str)

    if page is not None and page < 1:
        return jsonify({
            "ok": False,
            "code": "INVALID",
            "message": "page must be a positive integer."
        }), 400


    if field is not None and not 1 <= field <= 3:
        return jsonify({
            "ok": False,
            "code": "INVALID_FIELD",
            "message": "field must be between 1 and 3.",
        }), 400

    rows = repo.get_bookings_in_range(
        start_date, end_date, states=("draft", "awaiting_payment", "confirmed", "unpaid"),
        field=field, page=page, search=search
    )
    payments = booking_service.get_payments()
    rows = _combine_bookings_payments(rows, payments)
    return jsonify({
        "ok": True,
        "data": [_serialize(r) for r in rows]
    }), 200


def _page_args() -> tuple[int, int, int]:
    """Parse ?page and ?page_size into (page, page_size, offset).

    page is 1-based; page_size defaults to config.PAGE_SIZE and is capped at 100.
    Invalid values fall back to defaults rather than erroring.
    """
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.args.get("page_size", config.PAGE_SIZE))
    except (TypeError, ValueError):
        page_size = config.PAGE_SIZE
    page_size = max(1, min(page_size, 100))
    return page, page_size, (page - 1) * page_size


def _paginated(rows: list[dict], total: int, page: int, page_size: int):
    return jsonify({
        "ok": True,
        "data": [_serialize(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
    }), 200


@manager_api.get("/api/manager/history")
def list_history():
    """Booking change history (newest first), paginated.

    Optional filters:
      ?source=<exact>            exact source (a bot source such as 'whatsapp',
                                 'chatbot:Бот' or 'bot:ApiPay', or a manager id/email)
      ?channel=whatsapp|manager  bot-driven (incl. 'bot:*') vs manager-driven
    Pagination: ?page=<1-based> &page_size=<n> (default config.PAGE_SIZE, max 100).
    Without a filter, returns every history row.
    """
    page, page_size, offset = _page_args()
    source = request.args.get("source")
    channel = request.args.get("channel")
    if source:
        rows, total = history_repo.get_history_by_source(source, page_size, offset)
    elif channel == "whatsapp":
        rows, total = history_repo.get_whatsapp_history(page_size, offset)
    elif channel == "manager":
        rows, total = history_repo.get_manager_history(limit=page_size, offset=offset)
    else:
        rows, total = history_repo.get_all_history(page_size, offset)
    return _paginated(rows, total, page, page_size)


@manager_api.get("/api/manager/history/range/<string:start_date>/<string:end_date>")
def get_history_in_range(start_date: str, end_date: str):
    """History rows with created_at in [start_date, end_date] (inclusive), paginated.

    Bare dates ('YYYY-MM-DD') expand the end to end-of-day so the whole
    end date is included; full timestamps are passed through unchanged.
    Pagination: ?page=<1-based> &page_size=<n> (default config.PAGE_SIZE, max 100).
    """
    page, page_size, offset = _page_args()
    end = end_date if len(end_date) > 10 else f"{end_date} 23:59:59.999999"
    rows, total = history_repo.get_history_between(start_date, end, page_size, offset)
    return _paginated(rows, total, page, page_size)


@manager_api.get("/api/manager/history/source/<string:source>")
def get_history_by_source(source: str):
    """History rows from one exact source (a bot source such as 'whatsapp',
    'chatbot:Бот' or 'bot:ApiPay', or a manager id/email), newest first, paginated.

    Pagination: ?page=<1-based> &page_size=<n> (default config.PAGE_SIZE, max 100).
    """
    page, page_size, offset = _page_args()
    rows, total = history_repo.get_history_by_source(source, page_size, offset)
    return _paginated(rows, total, page, page_size)


@manager_api.get("/api/manager/bookings/<int:booking_id>/history")
def get_booking_history(booking_id: int):
    """Chronological change history for a single booking (oldest first), paginated.

    Pagination: ?page=<1-based> &page_size=<n> (default config.PAGE_SIZE, max 100).
    """
    page, page_size, offset = _page_args()
    rows, total = history_repo.get_history_for_booking(booking_id, page_size, offset)
    return _paginated(rows, total, page, page_size)


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
    """Create bookings for a single customer from a list of (repeating) slots.

    Body: {slots: [{field, date, time_start, time_end,
                    repeat_mode?, repeat_until?}, ...],
           customer?, phone?, notes?, prepayment?, reserved_until?, updated_by?}
    Each slot is one merged interval; the backend expands `repeat_mode`
    (none|daily|weekly|monthly, until `repeat_until`) into one booking per
    occurrence. Creation is atomic: a single conflict rejects the whole batch.
    See booking_service.manager_create_bookings_batch.

    AVANS: when ApiPay is configured, ONE invoice is created for the whole
    batch — APIPAY_AVANS_PER_BOOKING (10 000 ₸) x the number of non-repeating
    slots — and pushed to `phone`'s Kaspi app. Repeating slots carry no avans.
    The invoice row is committed WITH the bookings and sent immediately after,
    so a reservation never exists without a record of what it owes. A send that
    fails leaves the batch created and the invoice queued for retry, reported
    as `invoice.status = "created"` — not as an error.
    """
    body = request.get_json(silent=True) or {}
    slots = body.get("slots")
    if not isinstance(slots, list) or not slots:
        return jsonify({"ok": False, "code": "INVALID",
                        "message": "slots must be a non-empty list."}), 400

    valid_modes = ("none", "daily", "weekly", "monthly")
    total_occurrences = 0
    for s in slots:
        if not isinstance(s, dict) or not all(s.get(k) for k in ("field", "date", "time_start", "time_end")):
            return jsonify({"ok": False, "code": "INVALID",
                            "message": "each slot needs field, date, time_start, time_end."}), 400
        mode = s.get("repeat_mode") or "none"
        if mode not in valid_modes:
            return jsonify({"ok": False, "code": "INVALID",
                            "message": f"repeat_mode must be one of {valid_modes}."}), 400
        until = s.get("repeat_until")
        if mode != "none":
            if not until:
                return jsonify({"ok": False, "code": "INVALID",
                                "message": "repeat_until is required when repeat_mode != none."}), 400
            if str(until) < str(s["date"]):
                return jsonify({"ok": False, "code": "INVALID",
                                "message": "repeat_until must be >= date."}), 400
        try:
            total_occurrences += len(booking_service.occurrence_dates(
                str(s["date"]), str(until or s["date"]), mode))
        except (ValueError, TypeError):
            return jsonify({"ok": False, "code": "INVALID",
                            "message": "invalid date or repeat_until."}), 400

    if total_occurrences > 1000:
        return jsonify({"ok": False, "code": "INVALID",
                        "message": "too many occurrences (max 1000); shorten the repeat range."}), 400

    # ── Avans invoice (ApiPay) ────────────────────────────────────────────────
    # Only non-repeating slots are charged, so a batch of pure repeats needs no
    # phone. Validate the number here, before anything is inserted: a phone
    # ApiPay would reject with a 422 must not cost us a rolled-back batch.
    phone = body.get("phone")
    chargeable_slots = sum(1 for s in slots if (s.get("repeat_mode") or "none") == "none")
    invoice_hook = None
    avans_per_booking = None
    if config.APIPAY_ENABLED and chargeable_slots:
        if not phone:
            return jsonify({"ok": False, "code": "INVALID",
                            "message": "phone обязателен: на него выставляется аванс."}), 400
        try:
            apipay_client.normalize_phone(phone)
        except ApiPayError as exc:
            return jsonify({"ok": False, "code": "INVALID", "message": str(exc)}), 400
        # And ask Kaspi whether it knows the number at all. An invoice to an
        # unknown one is accepted and only dies later as a webhook, so the
        # manager would get a 200 here and never hear that it failed — while the
        # dead invoice still counts against the daily creation quota.
        try:
            apipay_service.ensure_kaspi_client(phone)
        except KaspiClientMissing as exc:
            logger.warning("[MANAGER_API] Пакет отклонён: %s", exc)
            return jsonify({"ok": False, "code": "NO_KASPI", "error": "no_kaspi_client",
                            "message": f"{exc}"}), 400
        except ApiPayError as exc:
            logger.error("[MANAGER_API] Проверка номера в Kaspi не удалась: %s", exc)
            return jsonify({"ok": False, "code": "PAYMENT_PROVIDER_ERROR",
                            "error": "apipay_error", "error_message": str(exc),
                            "message": f"Не удалось проверить номер в Kaspi: {exc}. "
                                       "Брони не созданы, попробуйте ещё раз."}), 502
        invoice_hook = apipay_service.batch_invoice_hook(phone, body.get("customer"))
        avans_per_booking = config.APIPAY_AVANS_PER_BOOKING

    try:
        res = booking_service.manager_create_bookings_batch(
            slots,
            customer=body.get("customer"),
            phone=phone,
            notes=body.get("notes"),
            price_total=body.get("price_total"),
            prepayment=body.get("prepayment"),
            actor_id=_api_key_actor(),
            reserved_until=body.get("reserved_until", 30),
            updated_by=body.get("source", "Неизвестен"),
            on_created=invoice_hook,
            avans_per_booking=avans_per_booking,
        )
    except ApiPayError as exc:
        # Only reachable before the invoice row is written (a phone ApiPay
        # rejects outright); the batch transaction was rolled back.
        logger.error("[MANAGER_API] ApiPay отклонил счёт, пакет отменён: %s", exc)
        return jsonify({"ok": False, "code": "PAYMENT_PROVIDER_ERROR",
                        "message": f"Не удалось выставить аванс: {exc}. "
                                   "Брони не созданы, попробуйте ещё раз."}), 502

    if res["ok"]:
        # Bookings and invoice row are committed — now ask ApiPay to push it.
        # No invoice means no bookings: the whole batch is taken back (repeating
        # slots included — one request, one outcome) and the failure is reported
        # with ApiPay's own wording, so the manager can act on it or retry.
        queued = (res.get("data") or {}).get("invoice")
        if queued:
            try:
                res["data"]["invoice"] = apipay_service.send_invoice(queued)
            except ApiPayError as exc:
                cancelled = apipay_service.rollback_failed_send(
                    queued, str(exc),
                    booking_ids=(res.get("data") or {}).get("booking_ids") or [],
                )
                logger.error("[MANAGER_API] ApiPay не выставил счёт, брони %s "
                             "отменены: %s", cancelled, exc)
                return jsonify({
                    "ok": False,
                    "code": "PAYMENT_PROVIDER_ERROR",
                    "error": "apipay_error",
                    "error_message": str(exc),
                    "message": f"Не удалось выставить аванс: {exc}. "
                               "Брони отменены — попробуйте ещё раз.",
                    "cancelled_booking_ids": cancelled,
                }), 502

        for r in res.get("data", {}).get("created", []):
            if r.get("booking_id"):
                booking_row = repo.get_booking(r["booking_id"])
                _single_table_write(booking_row)
        data = res.get("data") or {}
        return jsonify({"ok": True,
                        "created_count": data.get("created_count", 0),
                        "booking_ids": data.get("booking_ids", []),
                        # Present only when an avans was charged; the client
                        # confirms in their Kaspi app, we get a webhook.
                        "invoice": data.get("invoice"),
                        "data": data}), 200

    # Conflict ->  409 with the precise collision list the FE renders.
    if res.get("error") == "conflict":
        return jsonify({"ok": False, "error": "conflict",
                        "conflicts": res.get("conflicts", []),
                        "code": res.get("code"),
                        "message": res.get("message")}), 409

    return jsonify(res), 409


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
            # The client is not in the room for a manager edit, so a status
            # change has to reach them — otherwise "confirmed" and "cancelled"
            # are known to everyone but the person they concern. Only a real
            # transition sends: re-saving the status a booking already had is a
            # manager's bookkeeping, not news. Off-thread, so the manager UI
            # never waits on WhatsApp.
            notification = _STATE_NOTIFICATIONS.get(patch.get("state"))
            if notification and patch["state"] != (res.get("data") or {}).get("old_state"):
                client_notify.notify_booking(booking_row, notification)

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
def delete_booking(booking_id: int):
    # NB: the parameter name must match the <int:booking_id> route variable —
    # Flask passes it by keyword.
    res = postgres.cancel_booking_trial(bot_name="dopsy_bot",
        object_id=booking_id, actor_type="manager", actor_id=_api_key_actor(), reason="manager_cancel"
    )
    if res["ok"]:
        booking_row = repo.get_booking(booking_id)
        if booking_row:
            # Only when THIS call did the cancelling — a repeated DELETE also
            # answers ok, and must not send the client a second cancellation.
            if (res.get("data") or {}).get("cancelled"):
                client_notify.notify_booking(booking_row, "manager_cancelled")
            upsert_booking_row(booking_row)
            _single_table_erase(booking_row)

    return jsonify(res), (200 if res["ok"] else 404)

@manager_api.delete("/api/manager/bookings/all/<int:booking_id>")
def delete_repetitive_booking(booking_id: int):
    res = booking_service.cancel_all_bookings(
        booking_id, actor_type="manager", actor_id=_api_key_actor(), reason="manager_cancel"
    )
    if res["ok"]:
        # Every occurrence that came down, listed in one message — a client with
        # a weekly slot should not get eight separate cancellations.
        cancelled_ids = (res.get("data") or {}).get("cancelled_ids") or []
        rows = repo.get_bookings(cancelled_ids)
        if rows:
            client_notify.notify_bookings(rows, "manager_series_cancelled")

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
    training_day = body.get("training_day")
    previous_training_day = body.get("previous_training_day")
    time_start = body.get("time_start")
    time_end = body.get("time_end")

    if max_cap is not None:
        max_cap = int(max_cap)

    group_res = None
    schedule_res = None

    if group_name is not None or max_cap is not None:
        group_res = on_manual_group_edit(
            group_id=group_id,
            group_name=group_name,
            max_cap=max_cap
        )
        if not group_res["ok"]:
            return jsonify(group_res), 404

    if time_start is not None or time_end is not None or previous_training_day is not None:
        if training_day is None and previous_training_day is None:
            return jsonify({
                "ok": False,
                "code": "INVALID",
                "message": "training_day is required when editing schedule fields."
            }), 400

        lookup_training_day = previous_training_day if previous_training_day is not None else training_day
        new_training_day = training_day if previous_training_day is not None else None
        schedule_res = on_manual_group_schedule_edit(
            group_id=group_id,
            training_day=int(lookup_training_day),
            new_training_day=int(new_training_day) if new_training_day is not None else None,
            time_start=time_start,
            time_end=time_end,
        )
        if not schedule_res["ok"]:
            if schedule_res["code"] in {"AMBIGUOUS_SCHEDULE", "SCHEDULE_CONFLICT"}:
                status = 409
            elif schedule_res["code"] in {"INVALID_TIME", "INVALID_WEEKDAY"}:
                status = 400
            else:
                status = 404
            return jsonify(schedule_res), status

    if group_res is None and schedule_res is None:
        return jsonify({
            "ok": False,
            "code": "NO_FIELDS",
            "message": "No fields to update"
        }), 400

    refresh_all_groups()
    return jsonify({
        "ok": True,
        "group_id": group_id,
        "schedule": _serialize(schedule_res) if schedule_res else None,
    }), 200


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

    def _as_dt(ts) -> datetime | None:
        #Force a timestamp to a tz-aware datetime in BOOKING_TIMEZONE.
        # Because sqlite time format does not include timezone
        try:
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_LOCAL_TZ)
        return ts.astimezone(_LOCAL_TZ)

    def _bump_activity(entry: dict, ts) -> None:
        if not ts:
            return
        dt = _as_dt(ts)
        if dt is None:
            return
        cur = _as_dt(entry["last_activity"]) if entry["last_activity"] else None
        if cur is None or dt > cur:
            entry["last_activity"] = dt.isoformat()

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


# ------------GLOBAL BOT SWITCH

@manager_api.get("/api/manager/is_messaging_enabled")
def is_messaging_enabled():
    return jsonify({"is_enabled": postgres.is_ycloud_enabled()}), 200


@manager_api.post("/api/manager/change_messaging_enabled")
def change_enabledness():
    body = request.get_json(silent=True) or {}
    enabled = body.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        return jsonify({"ok": False, "code": "INVALID",
                        "message": "enabled must be a boolean."}), 400

    new_state = postgres.set_ycloud_enabled(enabled, actor=_api_key_actor())
    logger.info("[BOT SWITCH] messaging %s by %s",
                "enabled" if new_state else "disabled", _api_key_actor())
    return jsonify({"is_enabled": new_state}), 200
