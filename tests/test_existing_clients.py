"""Academy bots stay silent for existing clients; the arena bot does not."""
import pytest

from handlers import message_handler
from integrations.providers.payload import (
    IncomingWhatsAppMessage,
    WhatsAppBusiness,
    WhatsAppCustomer,
)
from integrations.repo import existing_client_repo as repo
from integrations.repo.postgres import _conn
from scripts.import_existing_clients import read_phones

_IMPORTED = "77010000901"
_SUBSCRIBED = "77010000902"
_TRIAL_ONLY = "77010000903"
_PHONES = [_IMPORTED, _SUBSCRIBED, _TRIAL_ONLY]


@pytest.mark.no_db
@pytest.mark.parametrize("raw", ["+7 701 000 09 01", "8 701 000 09 01", "7010000901", "77010000901"])
def test_canonical_kz_phone(raw):
    assert repo.canonical_kz_phone(raw) == _IMPORTED


@pytest.mark.no_db
def test_read_phones_takes_phone_cells_from_any_column(tmp_path):
    path = tmp_path / "contacts.csv"
    path.write_text(
        "Name;Phone;Note\n"
        "Айгуль;+7 701 000 09 01;old client\n"
        "Bolat;8 (701) 000-09-02;\n"
        "dup;77010000901;12\n",
        encoding="utf-8",
    )
    assert read_phones(str(path)) == [_IMPORTED, _SUBSCRIBED]


@pytest.fixture
def clients():
    def wipe():
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM academy_existing_clients WHERE phone = ANY(%s)", (_PHONES,))
                cur.execute(
                    "DELETE FROM academy_users WHERE regexp_replace(parent_phone, '\\D', '', 'g') "
                    "IN ('77010000902', '87010000902', '77010000903')"
                )
    wipe()
    with _conn() as conn:
        with conn.cursor() as cur:
            # Stored in the local '8…' format a manager might type.
            cur.execute(
                "INSERT INTO academy_users (child_name, parent_phone, subscribed) VALUES "
                "('Paid kid', '8 701 000 09 02', TRUE), ('Trial kid', %s, FALSE)",
                (_TRIAL_ONLY,),
            )
    assert repo.add_existing_clients(["+7 701 000 09 01"], source="test") == 1
    yield
    wipe()


def test_existing_client_signals(clients):
    assert repo.is_existing_academy_client("+" + _IMPORTED)    # B: imported
    assert repo.is_existing_academy_client("+" + _SUBSCRIBED)  # A: subscribed student
    assert not repo.is_existing_academy_client(_TRIAL_ONLY)     # trial only — bot keeps talking
    assert not repo.is_existing_academy_client("77010000999")


def test_import_is_idempotent(clients):
    assert repo.add_existing_clients([_IMPORTED, "8 701 000 09 01"]) == 0


def _text(phone_number_id: str) -> IncomingWhatsAppMessage:
    return IncomingWhatsAppMessage(
        provider="meta",
        provider_message_id="provider-msg-1",
        whatsapp_message_id="wa-msg-1",
        message_type="text",
        text="Сәлеметсіз бе",
        customer=WhatsAppCustomer(phone="+" + _IMPORTED),
        business=WhatsAppBusiness(phone_number_id=phone_number_id),
    )


class _ReachedBotLogic(BaseException):
    """BaseException so the handler's `except Exception` cannot swallow it."""


def _stop(*_args, **_kwargs):
    raise _ReachedBotLogic


@pytest.fixture
def handler_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(message_handler.config, "BOT_CONFIGS", {
        "academy-phone-id": {"name": "dopsy_fs_school", "phone_number_id": "academy-phone-id"},
        "arena-phone-id": {"name": "dopsy_bot", "phone_number_id": "arena-phone-id"},
    })
    monkeypatch.setattr(message_handler, "is_bot_paused", lambda phone: False)
    monkeypatch.setattr(message_handler, "is_existing_academy_client", lambda phone: True)
    monkeypatch.setattr(message_handler, "mark_as_read",
                        lambda channel, message_id: calls.append(("read", message_id)))
    monkeypatch.setattr(message_handler, "send_text_message",
                        lambda channel, to, text: calls.append(("send", to, text)))
    monkeypatch.setattr(message_handler, "retrieve_context", _stop)
    return calls


@pytest.mark.no_db
def test_academy_bot_skips_existing_client_and_leaves_it_unread(handler_calls):
    message_handler.handle_incoming_message(_text("academy-phone-id"))
    assert handler_calls == []


@pytest.mark.no_db
def test_arena_bot_ignores_existing_client_list(handler_calls):
    # The arena bot goes on to its normal flow (stubbed to raise right after
    # mark_as_read), proving the existing-client check did not stop it.
    with pytest.raises(_ReachedBotLogic):
        message_handler.handle_incoming_message(_text("arena-phone-id"))
    assert ("read", "wa-msg-1") in handler_calls
