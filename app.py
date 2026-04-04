"""
Flask webhook server for WhatsApp Cloud API.

Endpoints:
  GET  /webhook  — Meta verification handshake
  POST /webhook  — Incoming messages from WhatsApp
  POST /admin/ingest — Re-index knowledge base documents (admin use)
"""

import logging
import threading

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
