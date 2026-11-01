"""BOT_ONLY_NUMBERS: a local test instance next to the production bot talks
only to the listed numbers — in and out."""
from unittest.mock import MagicMock

import pytest

import config
from handlers import whatsapp_client
from integrations.providers.payload import OutboundChannel

pytestmark = pytest.mark.no_db

_LIST = frozenset({"77072479672", "77762760814"})


@pytest.mark.parametrize("phone,allowed", [
    ("+77072479672", True), ("77072479672", True), ("87072479672", True),
    ("+7 776 276 08 14", True), ("+77001234567", False), ("", False),
])
def test_allowlist_matches_any_spelling(monkeypatch, phone, allowed):
    monkeypatch.setattr(config, "BOT_ONLY_NUMBERS", _LIST)
    assert config.bot_number_allowed(phone) is allowed


def test_empty_allowlist_allows_everyone(monkeypatch):
    monkeypatch.setattr(config, "BOT_ONLY_NUMBERS", frozenset())
    assert config.bot_number_allowed("+77001234567")


def test_outbound_to_others_is_refused(monkeypatch):
    monkeypatch.setattr(config, "BOT_ONLY_NUMBERS", _LIST)
    post = MagicMock()
    monkeypatch.setattr(whatsapp_client.requests, "post", post)
    monkeypatch.setattr(whatsapp_client.test_context, "is_test_mode", lambda: False)
    channel = OutboundChannel(provider="ycloud", phone_number_id=config.WHATSAPP_PHONE_NUMBER_ID_BOT_1)

    r = whatsapp_client.send_text_message(channel, "+77001234567", "hi")

    assert r["messages"][0]["id"] == "refused-not-allowlisted"
    post.assert_not_called()


def test_inbound_from_others_is_ignored(monkeypatch):
    from handlers import message_handler
    monkeypatch.setattr(config, "BOT_ONLY_NUMBERS", _LIST)
    payload = MagicMock()
    payload.provider = "ycloud"
    payload.customer.phone = "+77001234567"
    monkeypatch.setattr(config, "resolve_inbound_phone_number_id",
                        lambda *a: config.WHATSAPP_PHONE_NUMBER_ID_BOT_1)
    read = MagicMock()
    monkeypatch.setattr(message_handler, "mark_as_read", read)
    monkeypatch.setattr(message_handler, "is_bot_paused",
                        lambda p: pytest.fail("must stop before touching the DB"))

    message_handler.handle_incoming_message(payload)

    read.assert_not_called()
