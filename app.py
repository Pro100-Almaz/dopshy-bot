"""
Flask webhook server for WhatsApp Cloud API.

Endpoints:
  GET  /webhook  — Meta verification handshake
  POST /webhook  — Incoming messages from WhatsApp
  POST /admin/ingest — Re-index knowledge base documents (admin use)
"""

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, jsonify, abort

import config
from handlers.message_handler import handle_incoming_message
from integrations.sheets import refresh_week_sheet

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

# ---------------------------------------------------------------------------
# Scheduler: refresh Google Sheet every Monday 06:00 Almaty time
# ---------------------------------------------------------------------------

def _scheduled_sheet_refresh():
    if not config.GOOGLE_SPREADSHEET_ID:
        return
    try:
        from integrations.sheets import refresh_all_bookings
        refresh_all_bookings()
        logger.info("Scheduled sheet refresh complete.")
    except Exception as exc:
        logger.error("Scheduled sheet refresh failed: %s", exc)


def _cancel_expired_bookings():
    """Cancel expired awaiting_payment reservations + abandoned drafts, notify users."""
    if not config.POSTGRES_DSN:
        return
    try:
        from integrations.repo import booking_repo
        from integrations import booking_service, sheets
        from handlers.whatsapp_client import send_text_message

        expired = booking_repo.get_expired_bookings(config.BOOKING_SESSION_TTL)
        if not expired:
            return

        # Cancel each through the service layer so every transition is audited.
        for b in expired:
            booking_service.cancel_booking(
                b["id"], actor_type="system", reason="ttl_expired"
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
            except Exception as exc:
                logger.error("[PAYMENT] Sheet refresh failed: %s", exc)

        for b in to_notify:
            try:
                ts = str(b["time_start"])[:5]
                te = str(b["time_end"])[:5]
                send_text_message(
                    config.WHATSAPP_PHONE_NUMBER_ID_BOT_1,
                    b["phone"],
                    f"К сожалению, ваша бронь на {b['date']} ({ts}–{te}, Поле {b['field']}) "
                    f"была отменена — оплата не поступила в течение 1 часа.\n"
                    f"Хотите забронировать снова? Просто напишите нам!\n\n"
                    f"Өкінішке орай, брондауыңыз {b['date']} ({ts}–{te}, Поле {b['field']}) "
                    f"1 сағат ішінде төленбегендіктен жойылды.\n"
                    f"Қайта брондау үшін жазыңыз!",
                )
            except Exception as exc:
                logger.error("[PAYMENT] Failed to notify %s about cancelled booking %d: %s",
                             b["phone"], b["id"], exc)

    except Exception as exc:
        logger.error("[PAYMENT] Auto-cancel job failed: %s", exc)


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

    # Process in background so the webhook response is immediate
    thread = threading.Thread(
        target=handle_incoming_message,
        args=(payload,),
        daemon=True,
    )
    thread.start()

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

    from integrations.sheets import setup_sheet_template, refresh_all_bookings
    try:
        setup_sheet_template()
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
