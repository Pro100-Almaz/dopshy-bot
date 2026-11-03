"""Booking refunds through ApiPay (Kaspi).

Only money that came in through an ApiPay invoice can go back this way — ApiPay
refunds its own invoices, to the Kaspi account that paid them. Cash, Kaspi QR
and a hand-entered avans are returned outside the system.

A refund is asynchronous. `POST /invoices/{id}/refund` answers "accepted"
(pending); the outcome is the `invoice.refunded` webhook, or what
`GET /invoices/{id}/refunds` says. Every refund is a `booking_refunds` row moving
through a state machine (see migration 067):

    requested ──► processing ──► refunded
        │              │    └──► failed
        │              └───────► checking  (outcome uncertain — never retried)
        └ no answer / 5xx / 429: stays requested, resent with the SAME key

Idempotency:
  * `request_key`  — one per press of the manager's button. A double click, or
    the frontend resending after a dropped connection, finds the existing row.
  * `idempotency_key` — committed with the row BEFORE ApiPay is called, so a
    resend after a timeout is the same refund on ApiPay's side, not a second.
  * a unique index allows one unfinished refund per invoice; ApiPay refuses a
    second one anyway (`qr_refund_link_invoice_busy`).
  * webhook redelivery is a no-op: terminal rows are never moved again.

The 48-hour window (from bookings.first_paid_at) is information for the
manager, not a gate: past it a refund is allowed, just not owed.
"""

import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import psycopg2
import psycopg2.errors
import psycopg2.extras

import config
from integrations import apipay_client, client_notify
from integrations.apipay_client import ApiPayError
from integrations.repo.history_repo import _record_history
from integrations.repo.utils import _conn, _err, _ok

logger = logging.getLogger(__name__)

INFLIGHT = ("requested", "processing", "checking")
DONE = ("recorded", "refunded")
# Amounts that are gone or on their way out — both reserve the refundable sum.
COUNTED = DONE + INFLIGHT

_KEY_PREFIX = "dopshy-refund-"
_HISTORY_SOURCE = "bot:ApiPay"
# Before payment, paid_avans is only the avans the booking expects.
_UNPAID_STATES = {"draft", "awaiting_consent", "awaiting_payment", "unpaid"}
# Kaspi asks the buyer to confirm, or refuses this operation: we have no
# buyer-link flow, so the manager finishes it in the ApiPay cabinet.
_BUYER_CONFIRMATION = {"refund_requires_buyer_confirmation", "refund_rejected_by_kaspi"}

# Operator text by ApiPay's error_code — never ApiPay's own `message`.
ERROR_TEXT = {
    "refund_insufficient_funds": "На Kaspi-счёте не хватает денег. Пополните счёт и повторите возврат.",
    "refund_window_expired": "Срок возврата в Kaspi истёк. Верните деньги клиенту другим способом.",
    "refund_requires_buyer_confirmation": "Kaspi требует подтверждения покупателя — оформите ссылку-возврат в кабинете ApiPay.",
    "refund_rejected_by_kaspi": "Kaspi отклонил возврат — оформите ссылку-возврат в кабинете ApiPay.",
    "kaspi_throttled": "Kaspi временно ограничил запросы. Повторите возврат позже.",
    "network_unavailable": "Kaspi временно недоступен. Повторите возврат позже.",
    "session_transient": "Временный сбой сессии Kaspi. Повторите возврат позже.",
    "refund_resolved_not_executed": "Возврат не был выполнен. Можно повторить.",
    "refund_resolved_executed": "Деньги вернулись другим возвратом ApiPay.",
    "qr_refund_link_invoice_busy": "По этому счёту уже идёт возврат — дождитесь его результата.",
    "idempotency_key_conflict": "Конфликт ключа возврата — обновите окно и попробуйте снова.",
    "tariff_inactive": "Тариф ApiPay не оплачен — возвраты недоступны.",
    "hold_released": "Возврат снят сотрудником в кабинете ApiPay.",
    "http_400": "ApiPay отклонил возврат: сумма больше доступной или счёт уже возвращён.",
    "not_found": "Счёт не найден в ApiPay.",
}


def error_text(code: str | None) -> str:
    if not code:
        return ""
    return ERROR_TEXT.get(code, f"ApiPay отклонил возврат (код {code}).")


def _fmt(value) -> str:
    d = Decimal(str(value or 0))
    return f"{d.to_integral_value():f}" if d == d.to_integral_value() else f"{d.normalize():f}"


def _dec(value) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def refund_window_open(first_paid_at, now: datetime | None = None) -> bool | None:
    """True inside the refund window, False past it, None if never paid / unknown."""
    if first_paid_at is None:
        return None
    now = now or datetime.now(first_paid_at.tzinfo)
    return now - first_paid_at <= timedelta(hours=config.REFUND_WINDOW_HOURS)


def _received_avans(state, paid_avans, paid_api: Decimal, first_paid_at) -> Decimal:
    """The part of paid_avans that is money actually received.

    paid_avans is set at creation to the avans the booking EXPECTS; once the
    client pays through the bot that same money also arrives as a `payments`
    row (paid_api), so it counts only beyond that — and not at all when no
    payment was ever recorded or the booking is still waiting for one.
    """
    if first_paid_at is None or state in _UNPAID_STATES:
        return Decimal(0)
    return max(_dec(paid_avans) - paid_api, Decimal(0))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _refund_rows(cur, booking_id: int) -> list[dict]:
    cur.execute(
        "SELECT id, booking_id, amount, comment, within_window, created_by, created_at, "
        "       method, state, invoice_id, apipay_refund_id, error_code, updated_at "
        "  FROM booking_refunds WHERE booking_id = %s ORDER BY id",
        (booking_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["error_message"] = error_text(r.get("error_code"))
    return rows


def _summary(cur, booking_id: int, lock: bool = False) -> dict | None:
    """Everything a refund is decided on, for one booking.

    `refundable` is what can go back RIGHT NOW in one ApiPay refund:
    min(price_total, paid via ApiPay) − refunds done or in flight, and no more
    than one free invoice's share (a refund is per invoice, and an invoice with
    a refund in flight cannot take another).
    """
    cur.execute(
        "SELECT id, phone, date, time_start, time_end, field, price_total, state, "
        "       paid_kaspi_qr, paid_cash, paid_avans, first_paid_at "
        "  FROM bookings WHERE id = %s" + (" FOR UPDATE" if lock else ""),
        (booking_id,),
    )
    booking = cur.fetchone()
    if not booking:
        return None
    booking = dict(booking)

    cur.execute(
        "SELECT method, transaction_ref, amount FROM payments "
        " WHERE booking_id = %s AND status = 'accepted'",
        (booking_id,),
    )
    paid_api = Decimal(0)
    shares: dict[int, Decimal] = {}
    for p in cur.fetchall():
        amount = _dec(p["amount"])
        paid_api += amount
        ref = p["transaction_ref"] or ""
        if p["method"] == "apipay" and ref.startswith("apipay:"):
            try:
                invoice_id = int(ref.split(":")[1])
            except (IndexError, ValueError):
                continue
            shares[invoice_id] = shares.get(invoice_id, Decimal(0)) + amount

    phones: dict[int, str] = {}
    if shares:
        cur.execute("SELECT invoice_id, phone FROM apipay_invoices WHERE invoice_id = ANY(%s)",
                    (list(shares),))
        phones = {r["invoice_id"]: r["phone"] for r in cur.fetchall()}

    refunds = _refund_rows(cur, booking_id)
    apipay_rows = [r for r in refunds if r["method"] == "apipay" and r["state"] in COUNTED]
    refunded_total = sum((_dec(r["amount"]) for r in refunds if r["state"] in DONE), Decimal(0))
    pending_total = sum((_dec(r["amount"]) for r in refunds if r["state"] in INFLIGHT), Decimal(0))
    apipay_paid = sum(shares.values(), Decimal(0))
    apipay_counted = sum((_dec(r["amount"]) for r in apipay_rows), Decimal(0))
    price_total = _dec(booking["price_total"])
    total_cap = max(min(price_total, apipay_paid) - apipay_counted, Decimal(0))

    busy = {r["invoice_id"] for r in refunds if r["state"] in INFLIGHT}
    best_invoice, best_available = None, Decimal(0)
    for invoice_id, share in shares.items():
        if invoice_id in busy:
            continue
        used = sum((_dec(r["amount"]) for r in apipay_rows if r["invoice_id"] == invoice_id),
                   Decimal(0))
        available = share - used
        if available > best_available:
            best_invoice, best_available = invoice_id, available
    # ApiPay refunds whole tenge.
    refundable = int(min(total_cap, best_available)) if best_invoice else 0

    if refundable > 0:
        blocked = None
    elif apipay_paid <= 0:
        blocked = "no_apipay"
    elif busy:
        blocked = "in_progress"
    else:
        blocked = "nothing_left"

    paid_total = (paid_api + _dec(booking["paid_kaspi_qr"]) + _dec(booking["paid_cash"])
                  + _received_avans(booking["state"], booking["paid_avans"], paid_api,
                                    booking["first_paid_at"]))
    return {
        "booking": booking,
        "price_total": price_total,
        "paid_total": paid_total,
        "apipay_paid": apipay_paid,
        "refunded_total": refunded_total,
        "pending_total": pending_total,
        "refundable": refundable,
        "blocked_reason": blocked,
        "invoice_id": best_invoice,
        "refund_phone": phones.get(best_invoice) if best_invoice else
                        (phones.get(next(iter(shares))) if shares else None),
        "first_paid_at": booking["first_paid_at"],
        "within_window": refund_window_open(booking["first_paid_at"]),
        "window_hours": config.REFUND_WINDOW_HOURS,
        "refunds": refunds,
    }


def _public(summary: dict) -> dict:
    out = {k: v for k, v in summary.items() if k != "booking"}
    return out


def get_summary(booking_id: int) -> dict:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            summary = _summary(cur, booking_id)
    if summary is None:
        return _err("NOT_FOUND", "Бронь не найдена.")
    return _ok(_public(summary))


# ---------------------------------------------------------------------------
# Requesting
# ---------------------------------------------------------------------------

_BLOCKED = {
    "no_apipay": ("NO_APIPAY_PAYMENT",
                  "По брони нет оплат через Kaspi (ApiPay) — вернуть через систему нечего."),
    "in_progress": ("REFUND_IN_PROGRESS",
                    "По брони уже идёт возврат — дождитесь его результата."),
    "nothing_left": ("NOTHING_LEFT", "Вся сумма, оплаченная через Kaspi, уже возвращена."),
}


def _parse_amount(amount) -> int | None:
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value <= 0 or value != value.to_integral_value():
        return None
    return int(value)


def request_refund(booking_id: int, amount, comment: str | None = None,
                   request_key: str | None = None, actor: str | None = None) -> dict:
    """Ask ApiPay to return `amount` ₸ of this booking's Kaspi payment.

    Returns `_ok(summary + {"refund": row})` once the row exists — even when
    ApiPay could not be reached (the row stays `requested` and is resent), so
    the caller always shows the row's real state. A refusal by ApiPay comes
    back as `REFUND_FAILED` with the failed row in `data`.
    """
    if not config.APIPAY_ENABLED:
        return _err("NOT_CONFIGURED", "ApiPay не настроен — возврат через Kaspi недоступен.")
    value = _parse_amount(amount)
    if value is None:
        return _err("INVALID_AMOUNT", "Сумма возврата — целое число тенге больше нуля.")
    request_key = (request_key or "").strip()[:100] or None
    comment = (comment or "").strip()[:500] or None
    actor = actor or "manager:Unknown"

    row, replay = None, False
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if request_key:
                    row = _row_by_request_key(cur, request_key)
                    if row is not None:
                        if row["booking_id"] != booking_id:
                            return _err("INVALID", "Ключ запроса принадлежит другой брони.")
                        replay = True
                if row is None:
                    summary = _summary(cur, booking_id, lock=True)
                    if summary is None:
                        return _err("NOT_FOUND", "Бронь не найдена.")
                    if summary["refundable"] <= 0:
                        return _err(*_BLOCKED[summary["blocked_reason"]])
                    if value > summary["refundable"]:
                        return _err("AMOUNT_TOO_LARGE",
                                    f"Можно вернуть не больше {_fmt(summary['refundable'])}₸.")
                    within = summary["within_window"]
                    cur.execute(
                        "INSERT INTO booking_refunds (booking_id, amount, comment, within_window, "
                        "  created_by, method, state, invoice_id, idempotency_key, request_key) "
                        "VALUES (%s, %s, %s, %s, %s, 'apipay', 'requested', %s, %s, %s) "
                        "RETURNING *",
                        (booking_id, value, comment, within, actor, summary["invoice_id"],
                         _KEY_PREFIX + uuid.uuid4().hex, request_key),
                    )
                    row = dict(cur.fetchone())
                    _record_history(
                        cur, booking_id, actor, key="apipay_refund_requested",
                        amount=_fmt(value), invoice_id=summary["invoice_id"],
                        window=(f"в течение {config.REFUND_WINDOW_HOURS} ч после оплаты" if within
                                else f"позже {config.REFUND_WINDOW_HOURS} ч после оплаты"
                                if within is False else "время оплаты неизвестно"),
                        comment=f" Комментарий: {comment}" if comment else "",
                    )
    except psycopg2.errors.UniqueViolation:
        # A concurrent press: either the same request_key (→ replay it) or a
        # second refund on an invoice that already has one in flight.
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                row = _row_by_request_key(cur, request_key) if request_key else None
        if row is None:
            return _err(*_BLOCKED["in_progress"])
        replay = True

    if not replay or row["state"] == "requested":
        _send(row)
    return _result(booking_id, row["id"])


def _row_by_request_key(cur, request_key: str) -> dict | None:
    cur.execute("SELECT * FROM booking_refunds WHERE request_key = %s", (request_key,))
    found = cur.fetchone()
    return dict(found) if found else None


def _result(booking_id: int, refund_id: int) -> dict:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            summary = _summary(cur, booking_id)
    data = _public(summary)
    data["refund"] = next(r for r in summary["refunds"] if r["id"] == refund_id)
    if data["refund"]["state"] == "failed":
        res = _err("REFUND_FAILED", data["refund"]["error_message"] or "Возврат не прошёл.")
        res["data"] = data
        return res
    return _ok(data)


def _send(row: dict) -> None:
    """POST the refund to ApiPay with the row's own Idempotency-Key.

    Safe to call again for a `requested` row: the same key and body make ApiPay
    return the refund it already created rather than a second one.
    """
    try:
        resp = apipay_client.create_refund(
            row["invoice_id"], int(_dec(row["amount"])), row["idempotency_key"],
            reason=f"Возврат по брони №{row['booking_id']}",
        )
    except ApiPayError as exc:
        status = exc.status_code
        if status is None or status >= 500 or status == 429:
            logger.warning("[REFUND] Возврат %s не отправлен (%s) — повторим с тем же ключом.",
                           row["id"], exc)
            return
        payload = exc.payload or {}
        code = (payload.get("error_code") or payload.get("code")
                or (payload.get("error") if isinstance(payload.get("error"), str) else None)
                or f"http_{status}")
        logger.error("[REFUND] ApiPay отклонил возврат %s: %s", row["id"], code)
        _apply(row_id=row["id"], status=apipay_client.REFUND_FAILED, error_code=code)
        return

    refund = resp.get("refund") or {}
    refund_id = refund.get("id")
    logger.info("[REFUND] apipay.refund_created id=%s invoice=%s booking=%s replay=%s",
                refund_id, row["invoice_id"], row["booking_id"], resp.get("idempotent_replay"))
    _apply(row_id=row["id"], apipay_refund_id=refund_id, status=refund.get("status"),
           error_code=refund.get("error_code"), operation_state=refund.get("operation_state"))


# ---------------------------------------------------------------------------
# Outcome — shared by the send response, the webhook and the sweeper
# ---------------------------------------------------------------------------

def _target_state(current: str, status: str | None, error_code: str | None,
                  operation_state: str | None) -> tuple[str, str | None]:
    if operation_state == "completed" or status == apipay_client.REFUND_COMPLETED:
        return "refunded", None
    if operation_state == "uncertain" or error_code == "refund_execution_uncertain":
        return "checking", error_code or "refund_execution_uncertain"
    if operation_state == "released":
        return "failed", "hold_released"
    if status == apipay_client.REFUND_FAILED or operation_state == "failed":
        return "failed", error_code or "kaspi_error"
    # pending / processing / awaiting_buyer — accepted, still running.
    return ("processing" if current == "requested" else current), None


def _apply(row_id: int, status: str | None = None, error_code: str | None = None,
           operation_state: str | None = None, apipay_refund_id=None) -> str | None:
    """Move one row according to what ApiPay reported. Idempotent: a terminal
    row is never touched again, so a redelivered webhook changes nothing.
    Returns the new state when it changed."""
    notify = None
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM booking_refunds WHERE id = %s FOR UPDATE", (row_id,))
            row = cur.fetchone()
            if not row or row["state"] not in INFLIGHT:
                return None
            if apipay_refund_id and row["apipay_refund_id"] is None:
                cur.execute("UPDATE booking_refunds SET apipay_refund_id = %s WHERE id = %s",
                            (int(apipay_refund_id), row_id))
            new_state, code = _target_state(row["state"], status, error_code, operation_state)
            if new_state == row["state"] and not (apipay_refund_id and row["apipay_refund_id"] is None):
                return None
            cur.execute(
                "UPDATE booking_refunds SET state = %s, error_code = COALESCE(%s, error_code), "
                "updated_at = NOW() WHERE id = %s",
                (new_state, code, row_id),
            )
            if new_state != row["state"]:
                _history(cur, row, new_state, code)
                logger.info("[REFUND] apipay.refund_result id=%s status=%s code=%s",
                            apipay_refund_id or row["apipay_refund_id"], new_state, code or "-")
                if new_state == "refunded":
                    notify = row
    if notify:
        _notify_client(notify)
    return new_state


def _history(cur, row: dict, state: str, code: str | None) -> None:
    if state == "refunded":
        _record_history(cur, row["booking_id"], _HISTORY_SOURCE, key="apipay_refund_done",
                        amount=_fmt(row["amount"]), invoice_id=row["invoice_id"])
    elif state == "failed":
        _record_history(cur, row["booking_id"], _HISTORY_SOURCE, key="apipay_refund_failed",
                        amount=_fmt(row["amount"]), reason=error_text(code))
    elif state == "checking":
        _record_history(cur, row["booking_id"], _HISTORY_SOURCE,
                        description=f"Исход возврата {_fmt(row['amount'])}тг не подтверждён — "
                                    "проверяется в кабинете ApiPay. Не повторяйте возврат.")


def _notify_client(row: dict) -> None:
    try:
        from integrations.repo import booking_repo
        booking = booking_repo.get_booking(row["booking_id"])
        client_notify.notify_booking(booking, "apipay_refunded",
                                     amount=client_notify.fmt_amount(row["amount"]))
    except Exception:  # a notification never breaks the refund it reports
        logger.exception("[REFUND] Не удалось уведомить клиента о возврате %s", row["id"])


# ---------------------------------------------------------------------------
# Webhook: invoice.refunded
# ---------------------------------------------------------------------------

def apply_refund_event(payload: dict) -> bool:
    """Apply an `invoice.refunded` delivery. Returns False when the invoice is
    not a booking invoice (contract installments keep their own handling)."""
    invoice = payload.get("invoice") or {}
    refund = payload.get("refund") or {}
    invoice_id = invoice.get("id")
    refund_id = refund.get("id")
    if not invoice_id or not refund_id:
        return False

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT contract_installment_id FROM apipay_invoices WHERE invoice_id = %s",
                        (int(invoice_id),))
            inv = cur.fetchone()
            if inv is None or inv.get("contract_installment_id"):
                return False
            row_id = _match_row(cur, int(invoice_id), refund)
            if row_id is None:
                _import_foreign(cur, int(invoice_id), refund)
                return True
    _apply(row_id, status=refund.get("status"), error_code=refund.get("error_code"),
           operation_state=refund.get("operation_state"), apipay_refund_id=refund_id)
    return True


def _match_row(cur, invoice_id: int, refund: dict) -> int | None:
    """Our row for this ApiPay refund: by its id, by our idempotency key, or —
    when the webhook beat the response to our POST — the one unacknowledged
    `requested` row on this invoice (the in-flight index makes it unique)."""
    cur.execute("SELECT id FROM booking_refunds WHERE apipay_refund_id = %s", (int(refund["id"]),))
    found = cur.fetchone()
    if found:
        return found["id"]
    key = refund.get("idempotency_key")
    if key:
        cur.execute("SELECT id FROM booking_refunds WHERE idempotency_key = %s", (key,))
        found = cur.fetchone()
        if found:
            return found["id"]
        if not key.startswith(_KEY_PREFIX):
            return None  # someone else's refund — never attach it to ours
    cur.execute(
        "SELECT id FROM booking_refunds WHERE invoice_id = %s AND state = 'requested' "
        "  AND apipay_refund_id IS NULL",
        (invoice_id,),
    )
    found = cur.fetchone()
    return found["id"] if found else None


def _import_foreign(cur, invoice_id: int, refund: dict) -> None:
    """A completed refund made outside the manager UI (ApiPay cabinet, or the
    successor of a resolved one). Record it so the refundable sum stays right
    — only when the invoice paid for exactly one booking, otherwise the money
    cannot be attributed and a human has to look."""
    if refund.get("status") != apipay_client.REFUND_COMPLETED and \
            refund.get("operation_state") != "completed":
        return
    cur.execute(
        "SELECT DISTINCT booking_id FROM payments WHERE method = 'apipay' "
        " AND status = 'accepted' AND transaction_ref LIKE %s",
        (f"apipay:{invoice_id}:%",),
    )
    bookings = [r["booking_id"] for r in cur.fetchall()]
    if len(bookings) != 1:
        logger.warning("[REFUND] apipay.refund_foreign id=%s invoice=%s — счёт на %d броней, "
                       "возврат не привязан.", refund.get("id"), invoice_id, len(bookings))
        return
    amount = _dec(refund.get("amount"))
    if amount <= 0:
        return
    cur.execute(
        "INSERT INTO booking_refunds (booking_id, amount, created_by, method, state, "
        "  invoice_id, apipay_refund_id) "
        "VALUES (%s, %s, 'ApiPay (кабинет)', 'apipay', 'refunded', %s, %s) "
        "ON CONFLICT (apipay_refund_id) WHERE apipay_refund_id IS NOT NULL DO NOTHING "
        "RETURNING *",
        (bookings[0], amount, invoice_id, int(refund["id"])),
    )
    inserted = cur.fetchone()
    if inserted:
        logger.info("[REFUND] apipay.refund_foreign id=%s invoice=%s booking=%s",
                    refund.get("id"), invoice_id, bookings[0])
        _history(cur, dict(inserted), "refunded", None)


# ---------------------------------------------------------------------------
# Sweeper — the safety net under the webhook
# ---------------------------------------------------------------------------

_RESEND_AFTER = timedelta(seconds=20)
_CHECK_AFTER = timedelta(seconds=60)


def sweep(booking_id: int | None = None, resend_after: timedelta = _RESEND_AFTER,
          check_after: timedelta = _CHECK_AFTER) -> int:
    """Resend unacknowledged refunds and pull the outcome of the rest.

    `requested` rows are resent with their own key (same refund on ApiPay's
    side). `processing` / `checking` rows are looked up in
    GET /invoices/{id}/refunds — lost webhooks and uncertain outcomes resolve
    here. Each row is handled on its own: one failure does not stop the rest.
    """
    if not config.APIPAY_ENABLED:
        return 0
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM booking_refunds WHERE state IN ('requested', 'processing', 'checking') "
                "  AND (%s::int IS NULL OR booking_id = %s) ORDER BY id",
                (booking_id, booking_id),
            )
            rows = [dict(r) for r in cur.fetchall()]
    now = datetime.now(tz=rows[0]["updated_at"].tzinfo) if rows else None
    handled = 0
    by_invoice: dict[int, list[dict]] = {}
    for row in rows:
        age = now - row["updated_at"]
        if row["state"] == "requested" and row["apipay_refund_id"] is None:
            if age >= resend_after:
                try:
                    _send(row)
                    handled += 1
                except Exception:
                    logger.exception("[REFUND] apipay.refund_reconcile_failed refund=%s", row["id"])
        elif age >= check_after:
            by_invoice.setdefault(row["invoice_id"], []).append(row)

    for invoice_id, inv_rows in by_invoice.items():
        try:
            listing = apipay_client.get_refunds(invoice_id)
        except ApiPayError as exc:
            logger.warning("[REFUND] apipay.refund_reconcile_failed invoice=%s code=%s",
                           invoice_id, exc.status_code)
            continue
        remote = {r.get("id"): r for r in listing.get("refunds") or []}
        for row in inv_rows:
            r = remote.get(row["apipay_refund_id"])
            if r is None:
                continue
            _apply(row["id"], status=r.get("status"), error_code=r.get("error_code"),
                   operation_state=r.get("operation_state"))
            handled += 1
        # Completed refunds we did not make (cabinet, resolved successors).
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                for r in remote.values():
                    if r.get("id") and _match_row(cur, invoice_id, r) is None:
                        _import_foreign(cur, invoice_id, r)
    return handled
