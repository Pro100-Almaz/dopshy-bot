from integrations.providers.payload import (
    IncomingWhatsAppMessage,
    WhatsAppBusiness,
    WhatsAppCustomer,
)
from handlers import message_handler


def _payload(text: str = "/reset") -> IncomingWhatsAppMessage:
    return IncomingWhatsAppMessage(
        provider="meta",
        provider_message_id="provider-msg-1",
        whatsapp_message_id="wa-msg-1",
        message_type="text",
        text=text,
        customer=WhatsAppCustomer(phone="+77001112233"),
        business=WhatsAppBusiness(phone_number_id="academy-phone-id"),
    )


def test_academy_reset_cancels_existing_trial_draft(monkeypatch):
    calls = []

    monkeypatch.setattr(message_handler.config, "BOT_CONFIGS", {
        "academy-phone-id": {
            "name": "dopsy_fs_school",
            "phone_number_id": "academy-phone-id",
        }
    })
    monkeypatch.setattr(message_handler, "is_bot_paused", lambda phone: False)
    monkeypatch.setattr(
        message_handler,
        "mark_as_read",
        lambda channel, message_id: calls.append(("read", message_id)),
    )
    monkeypatch.setattr(
        message_handler,
        "get_existing_trial_draft",
        lambda phone, bot_name: {"id": 42, "phone": phone},
    )
    monkeypatch.setattr(
        message_handler,
        "cancel_booking_trial",
        lambda bot_name, object_id: calls.append(("cancel", bot_name, object_id)),
    )
    monkeypatch.setattr(
        message_handler,
        "refresh_all_trials",
        lambda: calls.append(("refresh_trials",)),
    )
    monkeypatch.setattr(
        message_handler,
        "clear_history",
        lambda chat_id: calls.append(("clear", chat_id)),
    )
    monkeypatch.setattr(
        message_handler,
        "send_text_message",
        lambda channel, to, text: calls.append(("send", to, text)),
    )

    message_handler.handle_incoming_message(_payload())

    assert ("cancel", "dopsy_fs_school", 42) in calls
    assert ("refresh_trials",) in calls
    assert ("clear", "academy-phone-id:+77001112233") in calls
    assert any(
        call[0] == "send" and "История разговора сброшена" in call[2]
        for call in calls
    )
