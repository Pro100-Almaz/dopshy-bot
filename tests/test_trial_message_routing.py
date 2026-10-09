"""Academy branch of handle_incoming_message while a trial draft is in progress.

Same shape as the arena branch: no flow session — the draft row marks a signup
in progress; a plain yes/no on a chosen class short-circuits the router; signup
intents go to LlmTrialFlowHandler.handle; anything else is answered and the
flow's pending question is added after the answer.
"""
from datetime import datetime, timedelta, timezone

import pytest

from handlers import message_handler
from handlers.llm_trial_flow import LlmTrialFlowHandler
from handlers.sessions import trial_session
from integrations.providers.payload import (
    IncomingWhatsAppMessage,
    WhatsAppBusiness,
    WhatsAppCustomer,
)

pytestmark = pytest.mark.no_db

_DRAFT = {"id": 11, "language": "kk", "group_id": None}


@pytest.fixture
def academy(monkeypatch):
    state = {
        "sent": [], "draft": None, "intent": ("other", "kk"), "llm_reply": "LLM",
        "handled": [], "routed": [],
    }
    monkeypatch.setattr(message_handler.config, "BOT_CONFIGS", {
        "academy-phone-id": {"name": "dopsy_boxing", "phone_number_id": "academy-phone-id"},
    })
    monkeypatch.setattr(message_handler, "is_bot_paused", lambda phone: False)
    monkeypatch.setattr(message_handler, "is_existing_academy_client", lambda phone: False)
    monkeypatch.setattr(message_handler, "mark_as_read", lambda *a: None)
    monkeypatch.setattr(message_handler, "send_text_message",
                        lambda channel, to, text: state["sent"].append(text))
    monkeypatch.setattr(message_handler, "get_history", lambda chat_id: [])
    monkeypatch.setattr(message_handler, "append_message", lambda *a: None)
    monkeypatch.setattr(message_handler, "retrieve_context", lambda *a, **k: "")
    monkeypatch.setattr(message_handler._pg, "get_active_session", lambda *a: None)
    monkeypatch.setattr(message_handler.academy_repo, "get_existing_trial_draft",
                        lambda *a: state["draft"])
    monkeypatch.setattr(message_handler.trial, "get_trial_daytime", lambda *a: [])
    monkeypatch.setattr(message_handler.trial, "format_availability_context", lambda free: "")

    def route(history, text, pending=None, fallback="other"):
        state["routed"].append(pending)
        return state["intent"]

    monkeypatch.setattr(message_handler, "route_trial_message", route)
    monkeypatch.setattr(message_handler, "get_ai_response", lambda **k: (state["llm_reply"], None))
    monkeypatch.setattr(
        LlmTrialFlowHandler, "handle",
        lambda self, chat_id, phone, bot, text, history, lang: state["handled"].append((text, lang)) or "FLOW",
    )
    monkeypatch.setattr(LlmTrialFlowHandler, "pending_prompt",
                        lambda self, bot, draft, lang: "PENDING" if draft else None)
    return state


def _text(text: str) -> IncomingWhatsAppMessage:
    return IncomingWhatsAppMessage(
        provider="meta",
        provider_message_id="provider-msg-1",
        whatsapp_message_id="wa-msg-1",
        message_type="text",
        text=text,
        customer=WhatsAppCustomer(phone="+77001112233"),
        business=WhatsAppBusiness(phone_number_id="academy-phone-id"),
    )


def test_yes_on_a_chosen_class_skips_the_router(academy):
    academy["draft"] = {**_DRAFT, "group_id": 7}

    message_handler.handle_incoming_message(_text("иә"))

    assert academy["handled"] == [("иә", "kk")]
    assert academy["routed"] == []
    assert academy["sent"] == ["FLOW"]


def test_list_choice_goes_to_the_flow_in_the_signup_language(academy):
    academy["draft"] = dict(_DRAFT)
    academy["intent"] = ("trial_continue", "ru")

    message_handler.handle_incoming_message(_text("2."))

    assert academy["routed"] == ["PENDING"]
    assert academy["handled"] == [("2.", "kk")]


def test_router_failure_mid_signup_stays_in_the_flow(academy):
    academy["draft"] = dict(_DRAFT)
    academy["intent"] = (None, "kk")

    message_handler.handle_incoming_message(_text("1 17:00 орта"))

    assert academy["handled"] == [("1 17:00 орта", "kk")]


def test_side_question_is_answered_and_the_pending_question_follows(academy):
    academy["draft"] = dict(_DRAFT)
    academy["intent"] = ("other", "kk")

    message_handler.handle_incoming_message(_text("жаттықтырушы кім?"))

    assert academy["handled"] == []
    assert academy["sent"] == ["LLM\n\nPENDING"]


def test_info_intent_mid_signup_adds_the_pending_question(academy):
    academy["draft"] = dict(_DRAFT)
    academy["intent"] = ("question_contacts", "kk")

    message_handler.handle_incoming_message(_text("мекенжай қандай?"))

    [reply] = academy["sent"]
    assert reply.endswith("\n\nPENDING")
    assert academy["handled"] == []


def test_status_mid_signup_adds_the_pending_question(academy, monkeypatch):
    academy["draft"] = dict(_DRAFT)
    academy["intent"] = ("trial_status", "kk")
    monkeypatch.setattr(message_handler, "handle_trial_status_request", lambda *a: "STATUS")

    message_handler.handle_incoming_message(_text("менің жазылымым"))

    assert academy["sent"] == ["STATUS\n\nPENDING"]


def test_greeting_mid_signup_goes_to_the_flow(academy):
    academy["draft"] = dict(_DRAFT)

    message_handler.handle_incoming_message(_text("сәлем"))

    assert academy["handled"] == [("сәлем", "kk")]
    assert academy["routed"] == []


def test_greeting_without_a_signup_gets_the_fixed_reply(academy):
    message_handler.handle_incoming_message(_text("сәлем"))

    assert academy["handled"] == []
    assert academy["sent"][0].startswith("Здравствуйте! Чем могу помочь?")


def test_no_draft_routes_without_a_pending_question(academy):
    academy["intent"] = ("trial_new", "kk")

    message_handler.handle_incoming_message(_text("ертең сынақ сабағына жазыңыз"))

    assert academy["routed"] == [None]
    assert academy["handled"] == [("ертең сынақ сабағына жазыңыз", "kk")]


def test_legacy_llm_flow_session_row_is_dropped(monkeypatch):
    deleted = []
    monkeypatch.setattr(trial_session.postgres, "get_active_session",
                        lambda bot, chat: {"state": "trial_select_slot", "params": {"trial_id": 11}})
    monkeypatch.setattr(trial_session.postgres, "delete_session",
                        lambda bot, chat: deleted.append(chat))

    assert trial_session.handle_trial_turn("chat-1", "pid", "7700", "2", "dopsy_boxing") is None
    assert deleted == ["chat-1"]


@pytest.mark.parametrize(("age_minutes", "active"), [(5, True), (19, True), (21, False), (60 * 24 * 14, False)])
def test_only_a_recently_touched_draft_counts_as_in_progress(monkeypatch, age_minutes, active):
    monkeypatch.setattr(message_handler.config, "BOOKING_SESSION_TTL", 1200)
    draft = {"id": 11, "updated_at": datetime.now(timezone.utc) - timedelta(minutes=age_minutes)}
    monkeypatch.setattr(message_handler.academy_repo, "get_existing_trial_draft", lambda *a: draft)

    assert (message_handler._active_trial_draft("7700", "dopsy_boxing") is draft) is active


def test_stale_draft_does_not_keep_the_client_in_signup_mode(academy):
    academy["draft"] = None  # what _active_trial_draft returns for a stale draft
    academy["intent"] = ("other", "kk")

    message_handler.handle_incoming_message(_text("бағасы қанша?"))

    assert academy["sent"] == ["LLM"]
    assert academy["routed"] == [None]


@pytest.mark.parametrize(("text", "expected"), [
    ("2.", False), ("17:00", False), ("1 17:00", False), ("2 орта", True), ("ok", True),
])
def test_has_letters(text, expected):
    assert message_handler._has_letters(text) is expected
