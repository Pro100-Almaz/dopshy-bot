"""Agent-test console: capture seam, sandbox flag and cleanup scoping.

The guarantees worth protecting are that a console turn (1) never delivers a
real WhatsApp message, (2) never writes a production row, and (3) that the
sandbox flag survives into the background threads the pipeline spawns.
"""

import pytest

import config
from integrations import test_context
from integrations.providers.payload import OutboundChannel
from integrations.repo import agent_test_repo

pytestmark = pytest.mark.no_db


def _channel() -> OutboundChannel:
    return OutboundChannel(provider="ycloud", phone_number_id="test-phone-id")


# --------------------------------------------------------------------------
# Capture seam
# --------------------------------------------------------------------------

def test_send_text_message_is_captured_not_delivered(monkeypatch):
    """In console mode the reply lands in the outbox and no HTTP call is made."""
    import requests

    from handlers import whatsapp_client

    def _explode(*args, **kwargs):
        raise AssertionError("console mode must not make an outbound HTTP call")

    monkeypatch.setattr(requests, "post", _explode)

    with test_context.console_session() as (outbox, _trace):
        result = whatsapp_client.send_text_message(_channel(), "77000000001", "привет")

    assert len(outbox) == 1
    assert outbox[0]["to"] == "77000000001"
    assert outbox[0]["text"] == "привет"
    assert result["messages"][0]["id"].startswith("console-")


def test_mark_as_read_and_download_media_are_inert(monkeypatch):
    import requests

    from handlers import whatsapp_client
    from integrations.providers.payload import WhatsAppMedia

    def _explode(*args, **kwargs):
        raise AssertionError("console mode must not make an outbound HTTP call")

    monkeypatch.setattr(requests, "post", _explode)
    monkeypatch.setattr(requests, "get", _explode)

    with test_context.console_session():
        assert whatsapp_client.mark_as_read(_channel(), "wa-1") is None
        assert whatsapp_client.download_media(_channel(), WhatsAppMedia(id="m-1")) is None


def test_outside_console_mode_the_seam_is_transparent(monkeypatch):
    """A production send must still go over HTTP — the guard is context-scoped."""
    from handlers import whatsapp_client

    assert test_context.is_test_mode() is False
    calls = []
    monkeypatch.setattr(
        whatsapp_client.requests, "post",
        lambda *a, **k: calls.append(a) or _FakeResponse(),
    )
    monkeypatch.setattr(
        whatsapp_client.config, "get_bot_config",
        lambda pid: {"ycloud_api_key": "k", "ycloud_from": "77010000001"},
    )
    whatsapp_client.send_text_message(_channel(), "77011234567", "real")
    assert len(calls) == 1


class _FakeResponse:
    ok = True
    status_code = 200
    text = "{}"

    def raise_for_status(self):
        return None

    def json(self):
        return {"messages": [{"id": "real-1"}]}


# --------------------------------------------------------------------------
# Sandbox context
# --------------------------------------------------------------------------

def test_flag_is_scoped_to_the_block():
    assert test_context.is_test_mode() is False
    with test_context.console_session():
        assert test_context.is_test_mode() is True
    assert test_context.is_test_mode() is False


def test_flag_is_inherited_by_spawned_threads():
    """The Sheets/notify background threads must stay inside the sandbox."""
    seen = []
    with test_context.console_session():
        thread = test_context.spawn_thread(lambda: seen.append(test_context.is_test_mode()))
        thread.join(timeout=5)
    assert seen == [True]


def test_record_is_a_noop_outside_console_mode():
    test_context.record("rag", chunks=[])  # must not raise
    with test_context.console_session() as (_outbox, trace):
        test_context.record("rag", chunks=[1, 2, 3])
    assert [e["kind"] for e in trace] == ["rag"]


# --------------------------------------------------------------------------
# Synthetic phones
# --------------------------------------------------------------------------

@pytest.mark.parametrize("phone", [
    "+77000000001", "77000000001", "87000000001", "7000000001", "+7 700 000 00-01",
])
def test_console_phones_are_recognised_in_every_normalized_form(phone):
    assert agent_test_repo.is_test_phone(phone) is True


@pytest.mark.parametrize("phone", [
    # 77017000001 is the trap: it *contains* the console block "700000", so an
    # unanchored substring match would purge this real subscriber's rows.
    "+77011234567", "87011234567", "77771234567", "77017000001", "", None,
])
def test_real_phones_are_not_mistaken_for_console_phones(phone):
    assert agent_test_repo.is_test_phone(phone) is False


def test_synthetic_phone_survives_the_strict_apipay_normalizer():
    """apipay_client.normalize_phone RAISES on a malformed number."""
    from integrations import apipay_client

    phone = f"7{config.CONSOLE_TEST_NATIONAL_PREFIX}0001"
    assert len(phone) == 11
    assert apipay_client.normalize_phone(phone) == f"8{config.CONSOLE_TEST_NATIONAL_PREFIX}0001"


# --------------------------------------------------------------------------
# Side-effect guards
# --------------------------------------------------------------------------

def test_sheets_sync_is_skipped_in_console_mode(monkeypatch):
    from integrations.sheets import booking_sheets

    monkeypatch.setattr(booking_sheets.config, "GOOGLE_SPREADSHEET_ID", "sheet-1")
    monkeypatch.setattr(
        booking_sheets, "_get_worksheet",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not touch Sheets")),
    )
    with test_context.console_session():
        booking_sheets.upsert_booking_row({"id": 1})
        booking_sheets.refresh_all_bookings()
