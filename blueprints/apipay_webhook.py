"""ApiPay.kz webhook receiver — POST /webhooks/apipay.

Register this URL in the ApiPay dashboard (Settings → key card → "Изменить" →
"Адрес для уведомлений"). It must be a real HTTPS domain in production.

Contract notes that shape this file:
  * ApiPay expects a 2xx within 5 SECONDS, else it retries (11 attempts over
    ~2 hours). Booking state is written synchronously (fast, and correctness
    depends on it); Google Sheets sync — slow and best-effort — is pushed to a
    background thread so the response is never late.
  * The HMAC is computed over the RAW body, so `request.get_data()` is read
    before any JSON parsing.
  * Redelivery is expected: `apipay_service.apply_webhook_status` claims the
    transition and applies it in ONE transaction, returning a result only on a
    genuine status change — so repeated deliveries are no-ops, and a failure
    leaves nothing claimed for the next retry to mistake for a duplicate.
"""

import logging
import threading

from flask import Blueprint, jsonify, request

import config
from integrations import apipay_service
from integrations.apipay_client import STATUS_REFUNDED, verify_webhook_signature

logger = logging.getLogger(__name__)

apipay_webhook = Blueprint("apipay_webhook", __name__)


@apipay_webhook.post("/webhooks/apipay")
def receive_apipay_webhook():
    raw = request.get_data()  # RAW bytes — signature is computed over these
    signature = request.headers.get("X-Webhook-Signature")

    if not verify_webhook_signature(raw, signature):
        logger.warning("[APIPAY] Отклонён вебхук с неверной подписью.")
        return jsonify({"status": "invalid_signature"}), 401

    payload = request.get_json(silent=True) or {}
    event = payload.get("event")

    # Connectivity check from the dashboard ("Проверить уведомления").
    if event == "webhook.test":
        logger.info("[APIPAY] Тестовый вебхук получен — подпись верна.")
        return jsonify({"status": "ok"}), 200

    invoice = payload.get("invoice") or {}
    invoice_id = invoice.get("id")
    status = invoice.get("status")
    if not invoice_id or not status:
        logger.warning("[APIPAY] Вебхук без invoice.id/status: event=%s", event)
        return jsonify({"status": "ignored"}), 200

    if event == "invoice.refunded":
        status = STATUS_REFUNDED

    # Identifiers only — the payload also carries client_name/client_phone, and
    # there is no reason to spill those into the log on every delivery.
    logger.info("[APIPAY] Вебхук: event=%s invoice=%s status=%s kaspi=%s amount=%s",
                event, invoice_id, status, invoice.get("kaspi_invoice_id"),
                invoice.get("amount"))

    # Claim + apply in one transaction; only the first delivery of a given
    # transition gets a result back. Fields beyond these (external_order_id,
    # client_name/phone, description, timestamp) duplicate what we already hold
    # against the booking.
    result = apipay_service.apply_webhook_status(
        invoice_id, status,
        paid_at=invoice.get("paid_at"),
        error_code=invoice.get("error_code"),
        error_message=invoice.get("error_message"),
        kaspi_invoice_id=invoice.get("kaspi_invoice_id"),
        # Resolved on FIRST — ours, and already committed even when this
        # delivery beats our own record of ApiPay's invoice id.
        external_order_id=invoice.get("external_order_id"),
    )
    if result is None:
        return jsonify({"status": "ok", "duplicate": True}), 200

    changed = result["changed"]
    # A refund moves no booking — the slot was settled long before the money
    # went back — so it would never reach the client under a `changed`-only
    # gate, while it is exactly the kind of thing they are waiting to hear.
    # 'processing' → 'pending' stays out: nothing to tell, and a needless
    # week-sheet repaint on the way.
    if changed or status == STATUS_REFUNDED:
        # Off the 5-second budget. Shared with the reconciliation poller, so a
        # payment found by polling notifies and syncs identically.
        threading.Thread(target=apipay_service.after_transition,
                         args=(result["invoice"], changed, result["released"],
                               result["paid"], result["status"]),
                         daemon=True).start()

    return jsonify({"status": "ok", "bookings": changed}), 200


def register(app) -> None:
    """Attach the webhook only when ApiPay is configured."""
    if not config.APIPAY_ENABLED:
        logger.info("[APIPAY] APIPAY_API_KEY/APIPAY_WEBHOOK_SECRET не заданы — "
                    "приём онлайн-платежей выключен.")
        return
    app.register_blueprint(apipay_webhook)
    logger.info("[APIPAY] Вебхук зарегистрирован: POST /webhooks/apipay")
