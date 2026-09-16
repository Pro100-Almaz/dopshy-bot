"""Admin agent-test console API.

Lets staff hold a full conversation with any of the three bots through the REAL
pipeline (`handlers.message_handler.handle_incoming_message`), and see what the
agent did internally on each turn: the RAG chunks it retrieved, the routing
decision, the exact system prompt, token usage and latency.

The whole turn runs inside `test_context.console_session()`, which:
  * captures outbound replies instead of delivering them over WhatsApp,
  * marks every row the conversation creates `is_test` so it is invisible to
    availability, the slot-overlap constraint, Google Sheets and notifications,
  * collects the debug trace.

The turn is run SYNCHRONOUSLY — bypassing the Redis message batcher — so the
contextvars stay in scope and the replies are complete when the request returns.
"""

import logging
import time
import uuid

from flask import Blueprint, jsonify, request

import config
from blueprints.manager_api import _manager_request_token, _rate_limited
from chat.conversation import clear_history, get_history, invalidate
from handlers.message_handler import handle_incoming_message
from integrations import test_context
from integrations.providers.payload import (
    IncomingWhatsAppMessage,
    WhatsAppBusiness,
    WhatsAppCustomer,
    WhatsAppInteractive,
    WhatsAppButtonReply,
)
from integrations.repo import agent_test_repo, postgres as _pg

logger = logging.getLogger(__name__)

agent_test_api = Blueprint("agent_test_api", __name__)


@agent_test_api.before_request
def _authenticate_console():
    """Same shared-secret check as the manager API, but its own rate-limit bucket.

    manager_api keys its limiter on remote_addr alone. Behind the backend proxy
    every call — manager UI and console alike — arrives from the same address, so
    sharing the bucket would let a tester's turns 429 real managers.
    """
    if request.method == "OPTIONS":
        return None
    if not config.X_SERVICE_TOKEN:
        return jsonify({"ok": False, "code": "NOT_CONFIGURED",
                        "message": "Agent-test API is not configured."}), 503
    if _manager_request_token() != config.X_SERVICE_TOKEN:
        return jsonify({"ok": False, "code": "UNAUTHORIZED", "message": "Bad API key."}), 401
    if _rate_limited(f"agent-test:{request.remote_addr or 'unknown'}"):
        return jsonify({"ok": False, "code": "RATE_LIMITED",
                        "message": "Too many requests."}), 429
    return None


_MAX_TEXT_LEN = 4000


def _ok(data):
    return jsonify({"ok": True, "data": data}), 200


def _invalid(message: str):
    return jsonify({"ok": False, "code": "INVALID", "message": message}), 400


def _not_found(message: str):
    return jsonify({"ok": False, "code": "NOT_FOUND", "message": message}), 404


def _serialize(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _session_json(session: dict) -> dict:
    return {
        "id": session["id"],
        "bot_name": session["bot_name"],
        "phone_number_id": session["phone_number_id"],
        "test_phone": session["test_phone"],
        "chat_id": session["chat_id"],
        "title": session.get("title"),
        "created_by": session.get("created_by"),
        "created_at": _serialize(session.get("created_at")),
        "updated_at": _serialize(session.get("updated_at")),
    }


@agent_test_api.get("/api/agent-test/bots")
def list_bots():
    """The three agents, for the picker."""
    return _ok([
        {"bot_name": cfg["name"], "phone_number_id": pid}
        for pid, cfg in config.BOT_CONFIGS.items()
    ])


@agent_test_api.post("/api/agent-test/sessions")
def create_session():
    body = request.get_json(silent=True) or {}
    bot_name = (body.get("bot_name") or "").strip()
    if bot_name not in agent_test_repo.BOT_NAMES:
        return _invalid(f"bot_name must be one of {', '.join(agent_test_repo.BOT_NAMES)}")
    if not agent_test_repo.phone_number_id_for(bot_name):
        return _invalid(f"Bot {bot_name} is not configured on this server.")

    session = agent_test_repo.create_session(
        bot_name=bot_name,
        title=(body.get("title") or None),
        created_by=(body.get("created_by") or None),
    )
    return _ok(_session_json(session))


@agent_test_api.get("/api/agent-test/sessions")
def list_sessions():
    return _ok([_session_json(s) for s in agent_test_repo.list_sessions()])


@agent_test_api.get("/api/agent-test/sessions/<int:session_id>")
def get_session(session_id: int):
    session = agent_test_repo.get_session(session_id)
    if not session:
        return _not_found("Session not found.")
    # History comes from the pipeline's own store, so what is shown is exactly
    # what the agent will see as context on the next turn.
    # Re-read from SQLite: another gunicorn worker may have served the last turn.
    invalidate(session["chat_id"])
    messages = get_history(session["chat_id"])
    return _ok({"session": _session_json(session), "messages": messages})


@agent_test_api.post("/api/agent-test/sessions/<int:session_id>/messages")
def post_message(session_id: int):
    session = agent_test_repo.get_session(session_id)
    if not session:
        return _not_found("Session not found.")

    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    button_title = (body.get("button_title") or "").strip()
    if not text and not button_title:
        return _invalid("text is required.")
    if len(text) > _MAX_TEXT_LEN:
        return _invalid(f"text must be at most {_MAX_TEXT_LEN} characters.")

    bot_config = config.get_bot_config(session["phone_number_id"]) or {}
    message_id = f"console-{session_id}-{uuid.uuid4().hex[:12]}"

    # The pipeline resolves the bot from business.phone for ycloud, ignoring
    # phone_number_id. If YCLOUD_FROM_BOT_N is unset or rotated this resolves to
    # None and handle_incoming_message returns silently — a 200 with no reply and
    # no error, which looks like the agent ignoring you. Check it up front.
    resolved = config.resolve_inbound_phone_number_id(
        "ycloud", session["phone_number_id"], bot_config.get("ycloud_from")
    )
    if resolved != session["phone_number_id"]:
        return _invalid(
            f"Bot {session['bot_name']} cannot be resolved: YCLOUD_FROM_BOT_* for this "
            f"bot is missing or does not match its configured number."
        )

    payload = IncomingWhatsAppMessage(
        # 'ycloud' matches production for all three bots; the provider only
        # reaches invoice metadata, which is gated off in sandbox mode.
        provider="ycloud",
        provider_message_id=message_id,
        whatsapp_message_id=message_id,
        message_type="interactive" if button_title else "text",
        text=text or None,
        customer=WhatsAppCustomer(phone=session["test_phone"], name="Agent test"),
        business=WhatsAppBusiness(
            phone_number_id=session["phone_number_id"],
            # YCloud inbound resolves the bot by the business number, not the id.
            phone=bot_config.get("ycloud_from"),
        ),
        interactive=(
            WhatsAppInteractive(
                type="button_reply",
                button_reply=WhatsAppButtonReply(id=message_id, title=button_title),
            )
            if button_title
            else None
        ),
    )

    # Same reason as above: the pipeline is about to read this history as the
    # model's context, so a stale cache would silently drop a turn from it.
    invalidate(session["chat_id"])

    started = time.perf_counter()
    with test_context.console_session(session["test_phone"]) as (outbox, trace):
        try:
            handle_incoming_message(payload)
        except Exception as exc:  # noqa: BLE001 — report it, don't 500 the console
            logger.exception("[AGENT-TEST] Turn failed for session %s", session_id)
            trace.append({"kind": "error", "error": f"{type(exc).__name__}: {exc}"})
        replies = list(outbox)
        events = list(trace)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    agent_test_repo.touch_session(session_id)

    return _ok({
        "sent": text or button_title,
        "replies": [r["text"] for r in replies],
        "trace": events,
        "elapsed_ms": elapsed_ms,
        "messages": get_history(session["chat_id"]),
    })


def _reset(session: dict) -> dict:
    """Clear history, drop flow state, and purge the session's sandbox rows."""
    invalidate(session["chat_id"])
    clear_history(session["chat_id"])
    for bot_name in agent_test_repo.BOT_NAMES:
        try:
            _pg.delete_session(bot_name, session["chat_id"])
        except Exception:  # noqa: BLE001 — a missing session row is not an error
            logger.debug("[AGENT-TEST] No %s session row for %s", bot_name, session["chat_id"])
    return agent_test_repo.purge_test_data(session["test_phone"])


@agent_test_api.post("/api/agent-test/sessions/<int:session_id>/reset")
def reset_session(session_id: int):
    session = agent_test_repo.get_session(session_id)
    if not session:
        return _not_found("Session not found.")
    deleted = _reset(session)
    return _ok({"session": _session_json(session), "deleted": deleted, "messages": []})


@agent_test_api.delete("/api/agent-test/sessions/<int:session_id>")
def delete_session(session_id: int):
    session = agent_test_repo.get_session(session_id)
    if not session:
        return _not_found("Session not found.")
    deleted = _reset(session)
    agent_test_repo.delete_session_row(session_id)
    return _ok({"deleted": deleted})
