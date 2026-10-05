"""Academy bots hand unclear messages to the admin and never give a foreign number."""
import pytest

from handlers import message_handler
from integrations.providers.payload import (
    IncomingWhatsAppMessage,
    WhatsAppBusiness,
    WhatsAppCustomer,
)

pytestmark = pytest.mark.no_db

_KK = message_handler._ACADEMY_UNCLEAR_REPLY["kk"]
_RU = message_handler._ACADEMY_UNCLEAR_REPLY["ru"]


@pytest.mark.parametrize("text", [
    "Звоните +7 707 123 45 67",
    "номер 8 (701) 000-00-00",
    "77011234567",
])
def test_foreign_phone_detected(text):
    assert message_handler._has_foreign_phone(text)


@pytest.mark.parametrize("text", [
    "Әкімші телефоны: +7 700 555 6000.",
    "Позвоните 8 700 555 60 00",
    "Абонемент 10 000 - 15 000 тг",
    "Цены от 70 000 – 75 000 тг",
    "Занятия в 18:00–19:30, 2015–2017 г.р.",
    "",
    None,
])
def test_admin_phone_prices_and_dates_are_allowed(text):
    assert not message_handler._has_foreign_phone(text)


@pytest.fixture
def academy(monkeypatch):
    sent = []
    state = {"intent": ("other", "kk"), "llm_reply": "ok"}

    monkeypatch.setattr(message_handler.config, "BOT_CONFIGS", {
        "academy-phone-id": {"name": "dopsy_fs_school", "phone_number_id": "academy-phone-id"},
    })
    monkeypatch.setattr(message_handler, "is_bot_paused", lambda phone: False)
    monkeypatch.setattr(message_handler, "is_existing_academy_client", lambda phone: False)
    monkeypatch.setattr(message_handler, "mark_as_read", lambda *a: None)
    monkeypatch.setattr(message_handler, "send_text_message",
                        lambda channel, to, text: sent.append(text))
    monkeypatch.setattr(message_handler, "get_history", lambda chat_id: [])
    monkeypatch.setattr(message_handler, "append_message", lambda *a: None)
    monkeypatch.setattr(message_handler, "retrieve_context", lambda *a, **k: "")
    monkeypatch.setattr(message_handler._pg, "get_active_session", lambda *a: None)
    monkeypatch.setattr(message_handler.trial, "get_trial_daytime", lambda *a: [])
    monkeypatch.setattr(message_handler.trial, "format_availability_context", lambda free: "")
    monkeypatch.setattr(message_handler, "route_trial_message",
                        lambda *a, **k: state["intent"])
    monkeypatch.setattr(message_handler, "get_ai_response",
                        lambda **k: (state["llm_reply"], None))
    state["sent"] = sent
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


@pytest.mark.parametrize("lang, expected", [("kk", _KK), ("ru", _RU)])
def test_unclear_intent_gets_admin_line(academy, monkeypatch, lang, expected):
    academy["intent"] = ("unclear", lang)
    monkeypatch.setattr(message_handler, "get_ai_response",
                        lambda **k: pytest.fail("LLM must not be called for unclear"))

    message_handler.handle_incoming_message(_text("ммм қалай анау"))

    assert academy["sent"] == [expected]


def test_llm_reply_with_foreign_number_is_replaced(academy):
    academy["llm_reply"] = "Арена бойынша +7 707 123 45 67 нөміріне хабарласыңыз"

    message_handler.handle_incoming_message(_text("алаң қайда"))

    assert academy["sent"] == [_KK]


def test_llm_reply_with_admin_number_is_kept(academy):
    academy["llm_reply"] = "Әкімшіге хабарласыңыз: +7 700 555 6000"

    message_handler.handle_incoming_message(_text("турнир қашан"))

    assert academy["sent"] == ["Әкімшіге хабарласыңыз: +7 700 555 6000"]


@pytest.mark.parametrize("lang, needle", [("kk", "шотты"), ("ru", "Счёт")])
def test_invoice_request_goes_to_admin(academy, monkeypatch, lang, needle):
    academy["intent"] = ("question_invoice", lang)
    monkeypatch.setattr(message_handler, "get_ai_response",
                        lambda **k: pytest.fail("LLM must not be called for an invoice request"))

    message_handler.handle_incoming_message(_text("Маған шот жіберіңіз"))

    [reply] = academy["sent"]
    assert needle in reply
    assert "+7 700 555 6000" in reply
    assert "табылмады" not in reply


@pytest.mark.parametrize("call", [
    lambda m: m.handle_trial_status_request("+77001112233", "dopsy_fs_school", "kk"),
    lambda m: m.handle_cancel_trial_request("chat", "+77001112233", "dopsy_fs_school"),
    lambda m: m.handle_edit_request("chat", "+77001112233", {}, "dopsy_fs_school"),
])
def test_no_trial_on_record_points_to_admin(monkeypatch, call):
    from handlers import edit_trial

    monkeypatch.setattr(edit_trial, "_active_trials", lambda bot_name, phone: [])
    monkeypatch.setattr(edit_trial.postgres, "delete_session", lambda *a: None)

    reply = call(edit_trial)

    assert "+7 700 555 6000" in reply
    assert "табылмады" not in reply and "жоқ" not in reply


def test_voucher_question_answers_from_full_voucher_guide(academy, monkeypatch):
    academy["intent"] = ("question_voucher", "kk")
    seen = {}

    def fake_llm(**kwargs):
        seen.update(kwargs)
        return "Қадамдар: ...", None

    monkeypatch.setattr(message_handler, "retrieve_context", lambda *a, **k: "UNRELATED CHUNKS")
    monkeypatch.setattr(message_handler, "get_ai_response", fake_llm)

    message_handler.handle_incoming_message(_text("Ваучермен сіздерге ауысуға бола ма?"))

    assert "academy_football_voucher_transfer_steps.md" in seen["context"]
    assert "Выданные ваучеры" in seen["context"]
    assert "UNRELATED CHUNKS" not in seen["context"]
    assert academy["sent"] == ["Қадамдар: ..."]


def test_non_voucher_question_keeps_normal_rag_context(academy, monkeypatch):
    academy["intent"] = ("other", "kk")
    seen = {}

    def fake_llm(**kwargs):
        seen.update(kwargs)
        return "ok", None

    monkeypatch.setattr(message_handler, "retrieve_context", lambda *a, **k: "NORMAL CHUNKS")
    monkeypatch.setattr(message_handler, "get_ai_response", fake_llm)

    message_handler.handle_incoming_message(_text("Турнир бола ма?"))

    assert "NORMAL CHUNKS" in seen["context"]
    assert "voucher" not in seen["context"]
