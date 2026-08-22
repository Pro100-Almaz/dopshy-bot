"""
Flask webhook server for WhatsApp Cloud API.

Endpoints:
  GET  /webhook  — Meta verification handshake
  POST /webhook  — Incoming messages from WhatsApp
  POST /admin/ingest — Re-index knowledge base documents (admin use)
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, jsonify, abort
from flask_cors import CORS

import config
from integrations import client_notify
from handlers.message_batcher import enqueue_incoming_message
from integrations.providers.meta import parse_meta, WhatsappPayloadParserError
from integrations.providers.payload import OutboundChannel
from integrations.providers.ycloud import parser_ycloud
from integrations.repo import postgres
from integrations.repo.bot_pause_repo import set_bot_paused
from integrations.sheets.booking_sheets import refresh_week_sheet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# Apply database migrations on startup (idempotent; no-op if already up to date)
if config.POSTGRES_DSN:
    try:
        from scripts.migrate import migrate as _pg_migrate
        _pg_migrate()
    except Exception as _e:
        logger.warning("PostgreSQL migrations skipped: %s", _e)

app = Flask(__name__)

# Manager API (Google Apps Script → backend)
from blueprints.manager_api import manager_api  # noqa: E402
app.register_blueprint(manager_api)
from blueprints.manager_boxing_api import manager_boxing_api  # noqa: E402
app.register_blueprint(manager_boxing_api)

# ApiPay.kz payment webhook (POST /webhooks/apipay) — self-disables when the
# APIPAY_* env vars are absent.
from blueprints import apipay_webhook as _apipay_webhook  # noqa: E402
_apipay_webhook.register(app)

# CORS — only the manager API is browser-facing; webhooks/admin are server-to-server.
# Origins come from config (CORS_ALLOWED_ORIGINS env, default "*"). The custom
# X-API-Key header must be allow-listed so browsers don't strip it on preflight.
CORS(
    app,
    resources={r"/api/manager/*": {"origins": config.CORS_ALLOWED_ORIGINS}},
    allow_headers=["Content-Type", "X-API-Key"],
    methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    max_age=86400,
)

# ---------------------------------------------------------------------------
# Scheduler: refresh Google Sheet every Monday 06:00 Almaty time
# ---------------------------------------------------------------------------

def _scheduled_sheet_refresh():
    if not config.GOOGLE_SPREADSHEET_ID:
        return
    try:
        from integrations.sheets.booking_sheets import refresh_all_bookings
        refresh_all_bookings()
        refresh_week_sheet()
        logger.info("Scheduled sheet refresh complete.")
    except Exception as exc:
        logger.error("Scheduled sheet refresh failed: %s", exc)


def _cancel_expired_bookings():
    """Cancel expired awaiting_payment reservations + abandoned drafts, notify users."""
    if not config.POSTGRES_DSN:
        return
    try:
        from integrations.repo import booking_repo
        from integrations import booking_service
        from integrations.sheets import booking_sheets as sheets
        from handlers.whatsapp_client import send_text_message

        expired = booking_repo.get_expired_bookings(config.BOOKING_SESSION_TTL)
        if not expired:
            return

        # Cancel the ApiPay invoices BEFORE releasing the slots — otherwise the
        # client could still confirm the payment in their Kaspi app for a
        # booking that no longer exists.
        #
        # Done in bulk here, deliberately: cancel_booking_trial below also
        # cancels invoices, but re-issues one for any booking of the batch that
        # is still awaiting payment. In a TTL sweep the whole batch expires
        # together, so the per-booking path would raise a new invoice for a
        # booking that is about to expire microseconds later — a pointless Kaspi
        # push to the client. Cancelling up front makes those calls no-ops.
        from integrations import apipay_service
        apipay_service.cancel_invoices_for_bookings(
            [b["id"] for b in expired], reason="ttl_expired")

        for b in expired:
            target = "unpaid" if b["state"] == "awaiting_payment" else "cancelled"
            postgres.cancel_booking_trial(
                config.BOT_CONFIGS[config.WHATSAPP_PHONE_NUMBER_ID_BOT_1]['name'], b["id"],
                actor_type="system", reason="ttl_expired", target_state=target,
            )

        # Only awaiting_payment expiries are user-facing (drafts never reserved a slot).
        to_notify = [b for b in expired if b["state"] == "awaiting_payment"]
        logger.info(
            "[PAYMENT] Auto-cancelled %d expired booking(s): ids=%s (notify=%d)",
            len(expired), [b["id"] for b in expired], len(to_notify),
        )

        if to_notify:
            try:
                sheets.refresh_all_bookings()
                sheets.refresh_week_sheet()
            except Exception as exc:
                logger.error("[PAYMENT] Sheet refresh failed: %s", exc)

        for b in to_notify:
            try:
                ts = str(b["time_start"])[:5]
                te = str(b["time_end"])[:5]
                send_text_message(
                    OutboundChannel(provider="ycloud", phone_number_id=config.WHATSAPP_PHONE_NUMBER_ID_BOT_1),
                    # A booking phone is not an inbound id: manager-entered rows
                    # hold '8…' or '+7 700 …', and YCloud takes E.164 only.
                    client_notify.normalize_recipient(b["phone"]),
                    f"К сожалению, ваша бронь на {b['date']} ({ts}–{te}, {b.get('format', '')}) "
                    f"была отменена — оплата не поступила в течении 20 минут.\n"
                    f"Хотите забронировать снова? Просто напишите нам!\n\n"
                    f"Өкінішке орай, брондауыңыз {b['date']} ({ts}–{te}, {b.get('format', '')}) "
                    f"20 минут ішінде төленбегендіктен жойылды.\n"
                    f"Қайта брондау үшін жазыңыз!",
                )
            except Exception as exc:
                logger.error("[PAYMENT] Failed to notify %s about cancelled booking %d: %s",
                             b["phone"], b["id"], exc)

    except Exception as exc:
        logger.error("[PAYMENT] Auto-cancel job failed: %s", exc)


def _sweep_apipay_outbox():
    """Re-send invoices committed but never delivered to ApiPay.

    Without this a crash or an ApiPay outage between the booking commit and the
    send would leave a client holding a reserved slot they were never asked to
    pay for — until the TTL silently dropped it.
    """
    if not config.APIPAY_ENABLED or not config.POSTGRES_DSN:
        return
    try:
        from integrations import apipay_service
        apipay_service.sweep_pending_sends()
    except Exception as exc:
        logger.error("[APIPAY] Outbox sweep failed: %s", exc)


def _reconcile_apipay():
    """Pull the status of invoices whose webhook never arrived.

    The webhook is the fast path, not the only one: a deploy, a certificate or
    DNS problem, or an outage longer than ApiPay's ~2h of retries silently
    swallows a delivery. Without this, a paid invoice would sit 'processing'
    until the TTL released the slot — the client pays, loses the booking, and
    nothing in the system knows.
    """
    if not config.APIPAY_ENABLED or not config.POSTGRES_DSN:
        return
    try:
        from integrations import apipay_service
        apipay_service.reconcile_open_invoices()
    except Exception as exc:
        logger.error("[APIPAY] Reconciliation failed: %s", exc)


_scheduler = BackgroundScheduler(timezone=config.BOOKING_TIMEZONE)
_scheduler.add_job(
    _scheduled_sheet_refresh,
    trigger="cron",
    day_of_week="mon",
    hour=6,
    minute=0,
)
_scheduler.add_job(
    _cancel_expired_bookings,
    trigger="interval",
    minutes=5,
)
# Tighter than the TTL sweep: an unsent invoice is a client waiting for a
# payment request that never arrived, inside a 20-minute reservation window.
_scheduler.add_job(
    _sweep_apipay_outbox,
    trigger="interval",
    minutes=1,
)
# Each pass only looks at invoices quiet for 5+ minutes, so a paid-but-lost
# webhook is found well inside the 30-minute reservation window — the client
# keeps the slot they paid for instead of it being released under them.
_scheduler.add_job(
    _reconcile_apipay,
    trigger="interval",
    minutes=2,
)
_scheduler.start()


# ---------------------------------------------------------------------------
# Webhook verification (GET)
# ---------------------------------------------------------------------------

@app.get("/webhook")
def verify_webhook():
    """
    Meta sends a GET request with three query params to verify the webhook URL.
    We must echo back the hub.challenge value if the token matches.
    """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == config.WHATSAPP_VERIFY_TOKEN:
        logger.info("Webhook verified successfully.")
        return challenge, 200

    logger.warning("Webhook verification failed. Token mismatch.")
    abort(403)


# ---------------------------------------------------------------------------
# Incoming messages (POST)
# ---------------------------------------------------------------------------

@app.post("/webhook")
def receive_message():


    # return jsonify({"status": "ok"}), 200
    """
    Receive WhatsApp message events from Meta.
    Process each message in a background thread so we return 200 fast
    (Meta requires a 200 response within 20 seconds or it retries).
    """
    payload = request.get_json(silent=True)
    if not payload:
        abort(400)

    # Confirm this is a WhatsApp Business Account event
    if payload.get("object") != "whatsapp_business_account":
        return jsonify({"status": "ignored"}), 200

    try:
        data = parse_meta(payload)
    except WhatsappPayloadParserError:
        logger.error({'Failed to parse Meta webhook'})
        return jsonify({"status": "ignored"}), 200

    if data is None:
        return jsonify({"status": "ignored"}), 200

    # Buffer + debounce: fragments sent in quick succession are combined into a
    # single message before hitting the pipeline. Non-blocking — returns at once.
    enqueue_incoming_message(data)

    return jsonify({"status": "ok"}), 200

@app.post("/webhook/ycloud")
def receive_ycloud_message():
    """
    Receive WhatsApp message events from YCloud Provider.
    Process each message in a background thread so we return 200 fast
    (Meta requires a 200 response within 20 seconds or it retries).
    """
    if not postgres.is_ycloud_enabled():
        return jsonify({"status": "ignored"}), 200

    payload = request.get_json(silent=True)
    logger.info("[YCLOUD] webhook received type=%s", (payload or {}).get("type"))
    if not payload:
        abort(400)

    # A human agent replied from the WhatsApp Business app — YCloud echoes it
    # back here. Auto-pause the bot for that customer so it stops replying
    # until a manager turns it back on from the UI.
    if payload.get("type") == "whatsapp.smb.message.echoes":
        customer_phone = (payload.get("whatsappMessage") or {}).get("to")
        if customer_phone:
            try:
                set_bot_paused(customer_phone, True, reason="auto")
                logger.info("[AUTO-PAUSE] Manager replied to %s — bot paused", customer_phone)
            except Exception:
                logger.exception("Failed to auto-pause bot for %s", customer_phone)
        return jsonify({"status": "ok"}), 200

    # Confirm this is a WhatsApp Business Account event
    if payload.get("type") != "whatsapp.inbound_message.received":
        logger.info("[YCLOUD] ignored webhook type=%s", payload.get("type"))
        return jsonify({"status": "ignored"}), 200

    try:
        data = parser_ycloud(payload)
        logger.info(
            "[YCLOUD] parsed inbound message type=%s from=%s to=%s",
            data.message_type,
            data.customer.phone,
            data.business.phone,
        )
        # if data.customer.phone not in ['+77476740954', '+77072479672', '+77076599990']:
        # if data.customer.phone not in ['+77072479672']:
        #     logger.info({f'IGNORED phone number {data.customer.phone}'})
        #     return jsonify({"status": "ignored"}), 200

    except WhatsappPayloadParserError:
        logger.exception("Failed to parse YCloud webhook")
        return jsonify({"status": "ignored"}), 200

    if data is None:
        logger.info("[YCLOUD] parser returned no message")
        return jsonify({"status": "ignored"}), 200

    # Buffer + debounce: fragments sent in quick succession are combined into a
    # single message before hitting the pipeline. Non-blocking — returns at once.
    enqueue_incoming_message(data)

    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Admin: re-ingest documents
# ---------------------------------------------------------------------------

@app.post("/admin/ingest")
def admin_ingest():
    """
    Trigger re-indexing of the knowledge base documents.
    Protect this endpoint with a simple token header in production.
    """
    auth = request.headers.get("X-Admin-Token", "")
    if auth != config.WHATSAPP_VERIFY_TOKEN:
        abort(403)

    from rag.vector_store import ingest_documents
    from rag.retriever import invalidate_cache

    try:
        count = ingest_documents()
        invalidate_cache()
        return jsonify({"status": "ok", "chunks_indexed": count}), 200
    except Exception as exc:
        logger.exception("Ingest failed: %s", exc)
        return jsonify({"status": "error", "detail": str(exc)}), 500


# ---------------------------------------------------------------------------
# Admin: apply Google Sheet template (run once after sheet creation)
# ---------------------------------------------------------------------------

@app.post("/admin/setup-sheet")
def admin_setup_sheet():
    auth = request.headers.get("X-Admin-Token", "")
    if auth != config.WHATSAPP_VERIFY_TOKEN:
        abort(403)

    from integrations.sheets.booking_sheets import refresh_all_bookings
    try:
        refresh_all_bookings()
        refresh_week_sheet()
        return jsonify({"status": "ok"}), 200
    except Exception as exc:
        logger.exception("Sheet setup failed: %s", exc)
        return jsonify({"status": "error", "detail": str(exc)}), 500


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return jsonify({"status": "healthy"}), 200


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
