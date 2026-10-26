"""Platform booking handoff: prefill client + the one-time link offer in the booking flow."""

from unittest.mock import MagicMock, patch

import pytest
import requests

import config
from handlers.sessions.booking_session import BookingStepHandler
from integrations import platform_prefill

pytestmark = pytest.mark.no_db

URL = "https://dopsy.kz/arena?p=tok#booking"


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(config, "PLATFORM_HANDOFF_ENABLED", True)
    monkeypatch.setattr(config, "PLATFORM_API_URL", "https://api.example/api")
    monkeypatch.setattr(config, "BOT_PREFILL_API_KEY", "k")


def _resp(status=201, body=None):
    r = MagicMock()
    r.status_code = status
    r.text = ""
    r.json.return_value = body if body is not None else {"ok": True, "data": {"url": URL}}
    return r


def test_payload_includes_only_known_valid_fields():
    p = platform_prefill.build_payload("+7 (701) 123-45-67", date="2026-11-06",
                                       time_start="19:00", time_end="20:00", field=1, lang="ru")
    assert p == {"phone": "77011234567", "field": 1, "date": "2026-11-06",
                 "time_start": "19:00", "time_end": "20:00", "lang": "ru"}


@pytest.mark.parametrize("start,end", [("22:00", "00:00"), ("23:00", "24:00"), ("20:00", "19:00"), ("19:00", None)])
def test_payload_drops_times_that_cannot_be_expressed(start, end):
    p = platform_prefill.build_payload("77011234567", date="2026-11-06", time_start=start, time_end=end)
    assert "time_start" not in p and "time_end" not in p
    assert p["date"] == "2026-11-06"


def test_disabled_makes_no_request(monkeypatch):
    monkeypatch.setattr(config, "PLATFORM_HANDOFF_ENABLED", False)
    with patch("integrations.platform_prefill.requests.post") as post:
        assert platform_prefill.create_prefill_link("77011234567") is None
    post.assert_not_called()


def test_returns_url_and_sends_key(enabled):
    with patch("integrations.platform_prefill.requests.post", return_value=_resp()) as post:
        assert platform_prefill.create_prefill_link("77011234567", date="2026-11-06") == URL
    assert post.call_args.args[0] == "https://api.example/api/bot/prefill"
    assert post.call_args.kwargs["headers"]["X-API-Key"] == "k"


def test_retries_once_on_timeout_then_gives_up(enabled):
    with patch("integrations.platform_prefill.requests.post", side_effect=requests.Timeout) as post:
        assert platform_prefill.create_prefill_link("77011234567") is None
    assert post.call_count == 2


def test_client_error_is_not_retried(enabled):
    with patch("integrations.platform_prefill.requests.post", return_value=_resp(422)) as post:
        assert platform_prefill.create_prefill_link("77011234567") is None
    assert post.call_count == 1


def test_malformed_response_returns_none(enabled):
    with patch("integrations.platform_prefill.requests.post", return_value=_resp(body={"nope": 1})):
        assert platform_prefill.create_prefill_link("77011234567") is None


def _handler():
    return BookingStepHandler("arena")


def _params():
    return {"sender_phone": "77011234567", "date": "2026-11-06", "time_start": "19:00",
            "time_end": "20:00", "field": 1}


def test_note_offered_once(enabled):
    params = _params()
    with patch("integrations.platform_prefill.create_prefill_link", return_value=URL) as create:
        first = _handler()._platform_link_note("chat", params, "ru")
        second = _handler()._platform_link_note("chat", params, "ru")
    assert URL in first and second == ""
    assert create.call_count == 1


def test_note_localized_to_kazakh(enabled):
    with patch("integrations.platform_prefill.create_prefill_link", return_value=URL):
        note = _handler()._platform_link_note("chat", _params(), "kk")
    assert URL in note and "толтырып" in note


def test_backend_down_gives_empty_note_and_chat_continues(enabled):
    params = _params()
    with patch("integrations.platform_prefill.requests.post", side_effect=requests.ConnectionError):
        assert _handler()._platform_link_note("chat", params, "ru") == ""
    assert params["handoff_offered"] is True  # do not hammer a down backend every turn


def test_disabled_flag_gives_empty_note(monkeypatch):
    monkeypatch.setattr(config, "PLATFORM_HANDOFF_ENABLED", False)
    assert _handler()._platform_link_note("chat", _params(), "ru") == ""


def test_note_in_field_buttons_keeps_payload_valid_json(enabled):
    import json
    handler = _handler()
    with patch("integrations.platform_prefill.create_prefill_link", return_value=URL):
        note = handler._platform_link_note("chat", _params(), "ru")
    out = handler.builder.ask_field([{"id": 1, "format": "5x5"}, {"id": 3, "format": "6x6"}], "ru", note)
    payload = json.loads(out)
    assert URL in payload["body"]["text"]
    assert [b["reply"]["title"] for b in payload["action"]["buttons"]] == ["5x5", "6x6"]


# ── LLM booking flow (the arena bot's real path) ─────────────────────────────

from handlers.llm_booking_flow import LlmBookingFlowHandler  # noqa: E402


def _flow_data(**kw):
    d = {"date": "2026-11-06", "time_start": "19:00", "time_end": "20:00", "field": 1,
         "format": "5x5", "lang": "ru", "customer_name": None}
    d.update(kw)
    return d


@pytest.fixture
def flow(enabled):
    with patch("handlers.llm_booking_flow.booking_logic") as bl, \
         patch("integrations.platform_prefill.claim_offer", return_value=True) as claim, \
         patch("integrations.platform_prefill.create_prefill_link", return_value=URL) as create:
        import datetime as dt
        bl.get_week_range.return_value = (dt.date(2026, 11, 2), dt.date(2026, 11, 8))
        bl.get_all_booked.return_value = []
        bl.check_range_free.return_value = True
        yield LlmBookingFlowHandler(), bl, claim, create


def test_flow_appends_link_to_plain_reply(flow):
    h, _, _, create = flow
    out = h._with_platform_link("5x5 свободно. Имя?", _flow_data(), "c", "77011234567")
    assert out.endswith("5x5 свободно. Имя?") and URL in out and "Спасибо" in out
    assert create.call_args.kwargs["time_start"] == "19:00"


def test_flow_keeps_button_json_valid(flow):
    import json
    h = flow[0]
    reply = json.dumps({"type": "button", "body": {"text": "Выберите размер поля:"},
                        "action": {"buttons": [{"type": "reply", "reply": {"id": "id_1", "title": "5x5"}}]}})
    out = json.loads(h._with_platform_link(reply, _flow_data(field=None), "c", "77011234567"))
    assert URL in out["body"]["text"] and out["body"]["text"].endswith("Выберите размер поля:")
    assert out["action"]["buttons"][0]["reply"]["title"] == "5x5"


def test_flow_skips_taken_slot(flow):
    h, bl, claim, create = flow
    bl.check_range_free.return_value = False
    assert h._with_platform_link("занято", _flow_data(), "c", "77011234567") == "занято"
    create.assert_not_called()


def test_flow_skips_at_confirm_step(flow):
    h, _, _, create = flow
    assert h._with_platform_link("r", _flow_data(customer_name="Ерсултан"), "c", "7") == "r"
    create.assert_not_called()


def test_flow_offers_on_first_reply_before_any_details(flow):
    """The point of the link: offered before the user has to type a date/time range."""
    h, _, _, create = flow
    out = h._with_platform_link("📅 На какую дату?", _flow_data(date=None, time_start=None, time_end=None,
                                                              field=None, format=None), "c", "77011234567",
                                "хочу бронь")
    assert URL in out and out.endswith("📅 На какую дату?") and "время вводить не нужно" in out
    assert create.call_args.kwargs["date"] is None and create.call_args.kwargs["time_start"] is None


def test_note_variants_follow_what_the_user_said():
    n = platform_prefill.note
    assert "Дату мы уже подставили" in n("ru", URL, has_date=True)
    assert "осталось нажать" in n("ru", URL, has_date=True, has_time=True)
    assert "свободные слоты" in n("ru", URL, availability=True)
    assert "время вводить не нужно" in n("ru", URL)
    assert "рахмет" in n("kk", URL).lower()


@pytest.mark.parametrize("text,expected", [
    ("какое время доступно?", True), ("какие есть свободные слоты", True),
    ("хочу бронь", False), ("10.00-11.00", False),
])
def test_asks_availability(text, expected):
    assert platform_prefill.asks_availability(text) is expected


def test_flow_availability_question_gets_availability_wording(flow):
    h = flow[0]
    out = h._with_platform_link("r", _flow_data(date=None, time_start=None, time_end=None, field=None),
                                "c", "7", "какое время доступно?")
    assert "свободные слоты" in out


def test_flow_does_not_offer_when_requested_slot_is_taken(flow):
    h, bl, claim, create = flow
    bl.check_range_free.return_value = False
    assert h._with_platform_link("занято", _flow_data(), "c", "7") == "занято"
    claim.assert_not_called()


def test_flow_offers_once(flow):
    h, _, claim, create = flow
    claim.side_effect = [True, False]
    first = h._with_platform_link("r", _flow_data(), "c", "7")
    second = h._with_platform_link("r", _flow_data(), "c", "7")
    assert URL in first and second == "r" and create.call_count == 1


def test_flow_never_raises(flow):
    h, bl, _, _ = flow
    bl.get_all_booked.side_effect = RuntimeError("db down")
    assert h._with_platform_link("r", _flow_data(), "c", "7") == "r"


def test_claim_offer_fails_closed_without_redis(monkeypatch):
    monkeypatch.setattr(platform_prefill, "_redis_client", None)
    monkeypatch.setattr(config, "REDIS_URL", "redis://127.0.0.1:1/0")
    assert platform_prefill.claim_offer("chat") is False


def test_flow_failed_link_allows_retry_soon(flow):
    h, _, _, create = flow
    create.return_value = None
    with patch("integrations.platform_prefill.retry_later") as retry:
        assert h._with_platform_link("r", _flow_data(), "c", "7") == "r"
    retry.assert_called_once_with("c")


def test_clear_history_releases_the_offer_mark(monkeypatch):
    from chat import conversation
    released = []
    monkeypatch.setattr(platform_prefill, "release_offer", released.append)
    monkeypatch.setattr(conversation, "_save_to_db", lambda *a, **k: None)
    conversation.clear_history("chat-x")
    assert released == ["chat-x"]
