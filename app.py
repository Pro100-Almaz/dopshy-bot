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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# Initialize PostgreSQL schema on startup (no-op if tables already exist)
if config.POSTGRES_DSN:
    try:
        from integrations.postgres import init_schema as _pg_init_schema
        _pg_init_schema()
    except Exception as _e:
        logger.warning("PostgreSQL init skipped: %s", _e)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Scheduler: refresh Google Sheet every Monday 06:00 Almaty time
# ---------------------------------------------------------------------------

def _scheduled_sheet_refresh():
    if not config.GOOGLE_SPREADSHEET_ID:
        return
    try:
        from integrations.sheets import maybe_refresh_week
        maybe_refresh_week(force=True)
        logger.info("Scheduled weekly sheet refresh complete.")
    except Exception as exc:
        logger.error("Scheduled sheet refresh failed: %s", exc)


def _cancel_expired_bookings():
    """Cancel awaiting_payment bookings older than PAYMENT_TTL_SECONDS and notify users."""
    if not config.POSTGRES_DSN:
        return
    try:
        from integrations.postgres import cancel_expired_bookings
        from integrations.sheets import maybe_refresh_week
        from handlers.whatsapp_client import send_text_message

        cancelled = cancel_expired_bookings(config.PAYMENT_TTL_SECONDS)
        if not cancelled:
            return

        logger.info(
            "[PAYMENT] Auto-cancelled %d unpaid booking(s): ids=%s",
            len(cancelled), [b["id"] for b in cancelled],
        )

        # Refresh Sheets for every affected date
        for d in {b["date"] for b in cancelled}:
            try:
                maybe_refresh_week(force=True, target_date=d)
            except Exception as exc:
                logger.error("[PAYMENT] Sheet refresh failed for date %s: %s", d, exc)

        # Notify each user
        for b in cancelled:
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

    from integrations.sheets import setup_sheet_template, maybe_refresh_week
    try:
        setup_sheet_template()
        maybe_refresh_week(force=True)
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
