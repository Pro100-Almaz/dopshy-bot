"""Offer consent before the ApiPay invoice (integrations/consent_flow.py).

Acceptance:
  * no ApiPay `create_invoice` before the client's "Келісемін" — on any path;
  * a client whose WhatsApp number has no Kaspi is asked for another number,
    and the invoice goes to that number.
Plus: decline, the retry limit and admin hand-over, "same number as last
time", the guard in queue_invoice, the slot being held, the manager and
website endpoints, the admin invoice endpoint and the TTL sweep.

ApiPay and WhatsApp are stubbed; the database is real (see conftest).
"""

import json
import uuid

import pytest
from flask import Flask

import config
from integrations import apipay_client, apipay_service, consent_flow
from integrations.repo.postgres import _conn

_KEY = "test-key"
_HDR = {"X-API-Key": _KEY}
_WA = "87001234567"          # the client's WhatsApp number
_OTHER = "87071112233"       # a second number, with Kaspi
_THIRD = "87479998877"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

@pytest.mark.no_db
@pytest.mark.parametrize("text,expected", [
    ("✅ Келісемін", "yes"), ("✅ Согласен", "yes"), ("иә", "yes"), ("да", "yes"),
    ("❌ Жоқ", "no"), ("❌ Нет", "no"), ("не согласен", "no"), ("келіспеймін", "no"),
    ("❌ Басқа нөмір", "no"), ("❌ Другой номер", "no"),
    ("давай в 19:00", None), ("а где поле?", None), ("", None),
    # A refusal cancels the booking: ambiguous words only re-ask.
    ("не знаю", None), ("а можно другой день?", None),
    ("Подтверждаю✅", None), ("Растаймын✅", None), ("Отмена❌", "no"),
])
def test_parse_answer(text, expected):
    assert consent_flow.parse_answer(text) == expected


@pytest.mark.no_db
@pytest.mark.parametrize("text,expected", [
    ("✅ Иә", "yes"), ("❌ Басқа нөмір", "no"), ("другой", "no"), ("басқа", "no"),
])
def test_parse_same_number_answer(text, expected):
    assert consent_flow.parse_answer(text, same_number=True) == expected


@pytest.mark.no_db
@pytest.mark.parametrize("text,expected", [
    ("+7 707 111 22 33", "87071112233"), ("87071112233", "87071112233"),
    ("7071112233", "87071112233"), ("мой номер 8 (707) 111-22-33", "87071112233"),
    ("12345", None), ("нет", None),
])
def test_parse_phone(text, expected):
    assert consent_flow.parse_phone(text) == expected


@pytest.mark.no_db
def test_prompt_has_offer_link_and_buttons_in_both_languages():
    for lang, yes, no in (("kk", "✅ Келісемін", "❌ Жоқ"), ("ru", "✅ Согласен", "❌ Нет")):
        msg = json.loads(consent_flow.prompt_message(lang, 10000))
        assert config.OFFER_URL in msg["body"]["text"]
        assert "10 000₸" in msg["body"]["text"]
        titles = [b["reply"]["title"] for b in msg["action"]["buttons"]]
        assert titles == [yes, no]
        assert all(len(t) <= 20 for t in titles), "WhatsApp caps button titles at 20"


@pytest.mark.no_db
def test_template_button_reply_is_parsed_as_an_interactive_reply():
    from integrations.providers.meta import parse_meta
    from integrations.providers.ycloud import parser_ycloud

    meta = parse_meta({"entry": [{"id": "waba", "changes": [{"value": {
        "metadata": {"phone_number_id": "1", "display_phone_number": "7"},
        "contacts": [{"wa_id": "7700", "profile": {"name": "A"}}],
        "messages": [{"from": "7700", "id": "m1", "type": "button",
                      "button": {"text": "✅ Келісемін", "payload": "yes"}}]}}]}]})
    yc = parser_ycloud({"whatsappInboundMessage": {
        "id": "m2", "from": "+7700", "to": "+7701", "fromUserId": "u", "type": "button",
        "button": {"text": "❌ Жоқ", "payload": "no"}}})
    for parsed, title in ((meta, "✅ Келісемін"), (yc, "❌ Жоқ")):
        assert parsed.message_type == "interactive"
        assert parsed.interactive.button_reply.title == title


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def apipay(monkeypatch):
    """ApiPay on, HTTP stubbed. `no_kaspi` = numbers /clients/check rejects."""
    monkeypatch.setattr(config, "APIPAY_ENABLED", True)
    monkeypatch.setattr(config, "APIPAY_API_KEY", "k")
    monkeypatch.setattr(config, "APIPAY_WEBHOOK_SECRET", "s")
    monkeypatch.setattr(config, "APIPAY_AVANS_PER_BOOKING", 10000)
    monkeypatch.setattr(config, "APIPAY_CHECK_CLIENT", True)
    monkeypatch.setattr(config, "ADMIN_ALERT_PHONE", "+7 701 000 00 00")
    monkeypatch.setattr(config, "KASPI_PAYMENT_URL", "https://pay.kaspi.kz/pay/test")
    monkeypatch.setattr(config, "WA_TEMPLATES", {k: "" for k in config.WA_TEMPLATES})
    calls = {"created": [], "checked": [], "no_kaspi": set(), "next_id": 9000}

    def check(phone):
        n = apipay_client.normalize_phone(phone)
        calls["checked"].append(n)
        return {"phone": n, "has_kaspi": n not in calls["no_kaspi"], "client_name": "X"}

    def create(phone, amount, description, external_order_id):
        calls["next_id"] += 1
        calls["created"].append({"phone": phone, "amount": amount})
        return {"id": calls["next_id"], "amount": amount, "status": "processing"}

    monkeypatch.setattr(apipay_client, "check_client", check)
    monkeypatch.setattr(apipay_client, "create_invoice", create)
    monkeypatch.setattr(apipay_client, "cancel_invoice", lambda iid: None)
    monkeypatch.setattr(consent_flow, "_refresh_sheets", lambda: None)
    return calls


@pytest.fixture(autouse=True)
def outbox(monkeypatch):
    """Every outbound WhatsApp message, synchronously:
    (to, "text"|"template", text-or-template-name, template params)."""
    from handlers import whatsapp_client
    from integrations import test_context
    sent = []
    monkeypatch.setattr(whatsapp_client, "send_text_message",
                        lambda ch, to, text: sent.append((to, "text", text, None)))
    monkeypatch.setattr(whatsapp_client, "send_template_message",
                        lambda ch, to, name, lang, params: sent.append((to, "template", name, params)))
    monkeypatch.setattr(test_context, "spawn_thread", lambda fn, *a, **kw: fn(*a, **kw))
    return sent


_ADMIN = "+77010000000"
_CLIENT = "+77001234567"


def _to(outbox, who):
    return [m for m in outbox if m[0] == who]


@pytest.fixture
def api(monkeypatch):
    config.X_SERVICE_TOKEN = _KEY
    for name in ("_single_table_write", "_single_table_erase", "upsert_booking_row",
                 "refresh_week_sheet"):
        monkeypatch.setattr(f"blueprints.manager_api.{name}", lambda *a, **k: None)
    from blueprints.manager_api import manager_api
    app = Flask(__name__)
    app.register_blueprint(manager_api)
    return app.test_client()


def _draft(date, ts="18:00", te="19:00", field=1, phone=_WA):
    token = str(uuid.uuid4())
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bookings (phone, customer_name, date, time_start, time_end, "
                "  field, format, players, state, source, client_token) "
                "VALUES (%s, 'Клиент', %s, %s, %s, %s, '5x5', 8, 'draft', 'whatsapp', %s) "
                "RETURNING id", (phone, date, ts, te, field, token))
            return cur.fetchone()[0], token


def _confirm(monkeypatch, date, lang="ru", phone=_WA, field=1):
    """The client presses "Подтверждаю" on a draft. Returns (booking_id, reply)."""
    from handlers import llm_booking_flow as flow_mod
    for name in ("clear_history", "refresh_all_bookings", "refresh_week_sheet"):
        monkeypatch.setattr(flow_mod, name, lambda *a, **kw: None)
    bid, token = _draft(date, phone=phone, field=field)
    draft = {"id": bid, "client_token": token, "date": date, "time_start": "18:00",
             "time_end": "19:00", "field": field, "format": "5x5", "players": 8,
             "customer_name": "Клиент"}
    reply = flow_mod.LlmBookingFlowHandler()._finalize_booking(
        draft, chat_id=f"7770001:{phone}", phone=phone, lang=lang)
    return bid, reply


def _row(bid):
    import psycopg2.extras
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM bookings WHERE id = %s", (bid,))
            return dict(cur.fetchone())


def _state(bid):
    return _row(bid)["state"]


def _customer(phone, kaspi=None):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO customers (name, phone, kaspi_phone) VALUES ('C', %s, %s) "
                        "ON CONFLICT (phone) DO UPDATE SET kaspi_phone = EXCLUDED.kaspi_phone",
                        (phone, kaspi))


def _customer_kaspi(phone):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT kaspi_phone FROM customers WHERE phone = %s", (phone,))
            row = cur.fetchone()
            return row[0] if row else None


def _say(text, phone=_WA):
    return consent_flow.handle_message(phone, text)


# ---------------------------------------------------------------------------
# Acceptance 1: no invoice before "Келісемін"
# ---------------------------------------------------------------------------

def test_five_bot_bookings_raise_no_invoice_before_consent(apipay, monkeypatch):
    phones = [f"8700000000{i}" for i in range(5)]
    bids = []
    for i, phone in enumerate(phones):
        bid, reply = _confirm(monkeypatch, f"2027-09-0{i + 1}", phone=phone)
        bids.append(bid)
        prompt = json.loads(reply)
        assert config.OFFER_URL in prompt["body"]["text"]
        assert _state(bid) == "awaiting_consent"
        assert _row(bid)["consent_at"] is None
    assert apipay["created"] == [], "create_invoice must not be called before consent"
    assert apipay["checked"] == [], "nor is the number even checked"

    for phone, bid in zip(phones, bids):
        reply = _say("✅ Келісемін", phone)
        assert "отправлен в Kaspi" in reply
        row = _row(bid)
        assert row["state"] == "awaiting_payment"
        assert row["consent_at"] is not None and row["consent_channel"] == "bot"
        assert row["offer_version"] == config.OFFER_VERSION
        assert row["consent_text_version"] == config.CONSENT_TEXT_VERSION
        assert row["kaspi_phone"] == phone
    assert len(apipay["created"]) == 5


def test_decline_cancels_and_bills_nothing(apipay, monkeypatch):
    bid, _ = _confirm(monkeypatch, "2027-09-10", lang="kk")

    reply = _say("❌ Жоқ")

    assert "шот жіберілмейді" in reply
    assert _state(bid) == "cancelled"
    assert apipay["created"] == []
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT event, note FROM booking_events WHERE booking_id = %s "
                        "  AND event = 'cancelled'", (bid,))
            assert cur.fetchone() is not None
    # The slot is free again.
    bid2, reply2 = _confirm(monkeypatch, "2027-09-10", phone=_OTHER)
    assert _state(bid2) == "awaiting_consent"


def test_anything_else_re_asks_the_consent_question(apipay, monkeypatch):
    bid, _ = _confirm(monkeypatch, "2027-09-11")
    reply = _say("а где находится поле?")
    assert config.OFFER_URL in json.loads(reply)["body"]["text"]
    assert _state(bid) == "awaiting_consent"
    assert apipay["created"] == []


def test_no_dialog_means_the_message_is_not_intercepted(apipay):
    assert _say("привет") is None


# ---------------------------------------------------------------------------
# Acceptance 2 (revised): a payment number without Kaspi → payment link + admin
# ---------------------------------------------------------------------------

def test_number_without_kaspi_asks_for_another_and_bills_it(apipay, monkeypatch):
    """Acceptance 2: the WhatsApp number has no Kaspi → the bot asks for another
    number, and the invoice goes to that number."""
    apipay["no_kaspi"].add(_WA)
    bid, _ = _confirm(monkeypatch, "2027-09-12", lang="kk")

    reply = _say("✅ Келісемін")
    assert reply == ("Бұл нөмірде Kaspi табылмады. Төлем шотын қай нөмірге жіберейін? "
                     "Нөмірді +7 7XX XXX XX XX форматында жазыңыз.")
    assert _state(bid) == "awaiting_consent", "the slot stays held meanwhile"
    assert apipay["created"] == []

    reply = _say("+7 707 111 22 33")

    assert "жіберілді" in reply and "+7 707 111 22 33" in reply
    assert apipay["created"] == [{"phone": _OTHER, "amount": 10000}]
    row = _row(bid)
    assert row["state"] == "awaiting_payment" and row["kaspi_phone"] == _OTHER
    assert row["payment_method"] == "kaspi_invoice"


def test_bad_format_is_re_asked_without_counting(apipay, monkeypatch):
    apipay["no_kaspi"].update({_WA, _OTHER})
    _confirm(monkeypatch, "2027-09-20")
    _say("✅ Согласен")                                 # WhatsApp number: ask #1
    assert "Kaspi не найден" in _say(_OTHER)           # typed, no Kaspi: ask #2

    for junk in ("12345", "не знаю", "+1 202 555 0100"):
        assert "Не удалось распознать номер" in _say(junk)
    assert consent_flow.get_active_request(_WA)["kaspi_retries"] == 2

    assert "отправлен в Kaspi" in _say(_THIRD), "the last allowed number still works"
    assert apipay["created"][0]["phone"] == _THIRD


def test_after_two_asks_admin_is_alerted_and_the_link_sent(apipay, monkeypatch, outbox):
    apipay["no_kaspi"].update({_WA, _OTHER, _THIRD})
    bid, _ = _confirm(monkeypatch, "2027-09-21", lang="kk")
    assert "қай нөмірге" in _say("✅ Келісемін")      # ask #1
    assert "қай нөмірге" in _say(_OTHER)               # ask #2

    reply = _say(_THIRD)

    assert "Администратор сізбен байланысады" in reply
    assert config.KASPI_PAYMENT_URL in reply and "10 000₸" in reply
    assert apipay["created"] == []
    row = _row(bid)
    assert row["state"] == "awaiting_payment" and row["payment_method"] == "link"
    assert row["consent_at"] is not None
    alert = _to(outbox, _ADMIN)
    assert len(alert) == 1 and "ни на одном из номеров" in alert[0][2]
    assert "Свяжитесь с клиентом" in alert[0][2]
    assert "+7 747 999 88 77" in alert[0][2] and "+7 700 123 45 67" in alert[0][2]
    assert consent_flow.get_active_request(_WA) is None, "the dialog is over"


def test_kaspi_number_is_remembered_for_a_whatsapp_only_client(apipay, monkeypatch):
    """No manager ever created this client: the bookings trigger (migration 053)
    made their profile, billing stores the number on it, and the next booking
    offers the same number."""
    apipay["no_kaspi"].add(_WA)
    _confirm(monkeypatch, "2027-09-22")
    _say("✅ Согласен")
    _say(_OTHER)

    assert _customer_kaspi(_WA) == _OTHER
    _confirm(monkeypatch, "2027-09-23")
    reply = json.loads(_say("✅ Согласен"))
    assert "+7 707 111 22 33" in reply["body"]["text"], "offered next time"


def test_failed_kaspi_check_also_falls_back_to_the_link(apipay, monkeypatch, outbox):
    monkeypatch.setattr(apipay_client, "check_client",
                        lambda phone: (_ for _ in ()).throw(apipay_client.ApiPayError("timeout")))
    bid, _ = _confirm(monkeypatch, "2027-09-13", lang="kk")

    reply = _say("✅ Келісемін")

    assert config.KASPI_PAYMENT_URL in reply and "Администратор сізбен байланысады" in reply
    assert _row(bid)["payment_method"] == "link"
    assert "проверка номера" in _to(outbox, _ADMIN)[0][2]


def test_typed_number_with_bad_format_is_re_asked(apipay, monkeypatch):
    _customer(_WA, kaspi=_OTHER)
    _confirm(monkeypatch, "2027-09-14")
    _say("✅ Согласен")
    _say("❌ Другой номер")

    for junk in ("12345", "не знаю", "+1 202 555 0100"):
        assert "Не удалось распознать номер" in _say(junk)
    assert "отправлен в Kaspi" in _say(_THIRD)
    assert apipay["created"][0]["phone"] == _THIRD


# ---------------------------------------------------------------------------
# "Same number as last time?"
# ---------------------------------------------------------------------------

def test_previous_kaspi_number_is_offered_and_used(apipay, monkeypatch):
    _customer(_WA, kaspi=_OTHER)
    bid, _ = _confirm(monkeypatch, "2027-09-15", lang="kk")

    reply = json.loads(_say("✅ Келісемін"))
    assert "өткендегідей +7 707 111 22 33" in reply["body"]["text"]

    assert "жіберілді" in _say("✅ Иә")
    assert apipay["created"][0]["phone"] == _OTHER
    assert _row(bid)["kaspi_phone"] == _OTHER


def test_previous_number_declined_asks_for_a_new_one(apipay, monkeypatch):
    _customer(_WA, kaspi=_OTHER)
    bid, _ = _confirm(monkeypatch, "2027-09-16")
    _say("✅ Согласен")

    assert "Напишите номер" in _say("❌ Другой номер")
    assert "отправлен в Kaspi" in _say(_THIRD)
    assert apipay["created"][0]["phone"] == _THIRD
    assert _customer_kaspi(_WA) == _THIRD


# ---------------------------------------------------------------------------
# The guard and the held slot
# ---------------------------------------------------------------------------

def test_queue_invoice_refuses_bookings_without_consent(apipay):
    bid, _ = _draft("2027-09-17")
    with pytest.raises(apipay_service.ConsentRequired):
        with _conn() as conn:
            with conn.cursor() as cur:
                apipay_service.queue_invoice(cur, _WA, [bid], 1, "Аванс")
    assert apipay["created"] == []


def test_awaiting_consent_holds_the_slot(apipay, monkeypatch):
    _confirm(monkeypatch, "2027-09-18")
    bid2, reply = _confirm(monkeypatch, "2027-09-18", phone=_OTHER)
    assert _state(bid2) == "draft", "the second client cannot take the held slot"
    assert "занят" in reply.lower() or "бос емес" in reply.lower() or "Свободн" in reply


def test_expired_consent_releases_the_slot_and_tells_the_client(apipay, monkeypatch, outbox):
    bid, _ = _confirm(monkeypatch, "2027-09-19")
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE consent_requests SET expires_at = NOW() - interval '1 minute'")

    assert consent_flow.expire_stale() == 1

    assert _state(bid) == "cancelled"
    assert "подтверждение не получено" in _to(outbox, _CLIENT)[-1][2]
    assert _say("✅ Согласен") is None, "a late answer finds no dialog"
    assert apipay["created"] == []


def test_unanswered_template_ends_with_the_cancelled_template(api, apipay, monkeypatch, outbox):
    """The template-only flow: a client who never wrote gets the confirm
    request as a template and, with no answer, the cancellation as one too."""
    monkeypatch.setattr(config, "WA_TEMPLATES", {**config.WA_TEMPLATES,
                                                 "booking_confirm_request": "dopsy_confirm",
                                                 "booking_cancelled": "dopsy_cancelled"})
    r = api.post("/api/manager/bookings/batch",
                 json={"phone": _WA, "slots": [_slot(1, "2027-10-12")]}, headers=_HDR)
    bid = r.get_json()["booking_ids"][0]
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE consent_requests SET expires_at = NOW() - interval '1 minute'")

    consent_flow.expire_stale()

    sent = _to(outbox, _CLIENT)
    assert [(m[1], m[2]) for m in sent] == [("template", "dopsy_confirm"),
                                            ("template", "dopsy_cancelled")]
    assert sent[1][3][1] == "подтверждение не получено"
    assert all("\n" not in p for m in sent for p in m[3]), "no newlines in variables"
    assert _state(bid) == "cancelled"


def test_reset_cancels_the_dialog(apipay, monkeypatch):
    bid, _ = _confirm(monkeypatch, "2027-09-20")
    assert consent_flow.cancel_for_phone(_WA)
    assert _state(bid) == "cancelled"


# ---------------------------------------------------------------------------
# Manager endpoints
# ---------------------------------------------------------------------------

def _slot(field, date, ts="10:00", te="11:00", **extra):
    return {"field": field, "date": date, "time_start": ts, "time_end": te, **extra}


def test_manager_batch_waits_for_consent_then_bills(api, apipay, outbox):
    body = {"phone": "+7 700 123 45 67", "customer": "Иван",
            "slots": [_slot(1, "2027-10-01"), _slot(2, "2027-10-01")]}

    r = api.post("/api/manager/bookings/batch", json=body, headers=_HDR)

    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["invoice"] is None and data["consent"]["status"] == "pending"
    assert {_state(b) for b in data["booking_ids"]} == {"awaiting_consent"}
    assert apipay["created"] == [] and apipay["checked"] == []
    # First the booking itself — no offer, no amount yet.
    ask = json.loads(_to(outbox, _CLIENT)[0][2])
    assert "оформил для вас бронь" in ask["body"]["text"] and "поле 2" in ask["body"]["text"]
    assert config.OFFER_URL not in ask["body"]["text"]
    assert [b["reply"]["title"] for b in ask["action"]["buttons"]] == ["✅ Подтвердить", "❌ Отменить"]
    assert _say("✅ Согласен") is not None and _state(data["booking_ids"][0]) == "awaiting_consent", \
        "consent words do not confirm the booking — the question is re-asked"

    # Then the offer, as a separate step.
    offer = json.loads(_say("Подтвердить"))
    assert config.OFFER_URL in offer["body"]["text"] and "20 000₸" in offer["body"]["text"]
    assert apipay["created"] == [], "confirming the booking is not consent"

    assert "отправлен в Kaspi" in _say("✅ Согласен")
    assert apipay["created"] == [{"phone": _WA, "amount": 20000}], "one invoice, both slots"
    for b in data["booking_ids"]:
        row = _row(b)
        assert row["state"] == "awaiting_payment" and row["consent_channel"] == "admin_bot"


def test_manager_can_hold_24h_without_phone_or_invoice_then_start_consent(api, apipay, outbox):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO customers (name, phone) VALUES ('Walk-in', '77009990000') RETURNING id"
            )
            customer_id = cur.fetchone()[0]

    r = api.post(
        "/api/manager/bookings/batch",
        json={
            "customer_id": customer_id,
            "hold_without_payment": True,
            "prepayment": 9999,
            "reserved_until": 5,
            "slots": [_slot(1, "2027-10-21")],
        },
        headers=_HDR,
    )

    assert r.status_code == 200, r.get_json()
    assert r.get_json()["hold_without_payment"] is True
    bid = r.get_json()["booking_ids"][0]
    row = _row(bid)
    assert row["state"] == "awaiting_payment"
    assert row["payment_method"] == "hold_without_payment"
    assert float(row["paid_avans"]) == 0
    assert 23.9 * 3600 < (row["reserved_until"] - row["created_at"]).total_seconds() < 24.1 * 3600
    assert apipay["created"] == [] and apipay["checked"] == []
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT description FROM booking_history WHERE booking_id = %s ORDER BY id", (bid,))
            assert "Слот 24 сағатқа төлемсіз ұсталды (админ)" in [x[0] for x in cur.fetchall()]

    converted = api.post(
        f"/api/manager/bookings/{bid}/request-payment",
        json={"phone": _WA},
        headers=_HDR,
    )
    assert converted.status_code == 200, converted.get_json()
    assert _state(bid) == "awaiting_consent"
    assert apipay["created"] == [], "conversion starts consent; it never invoices directly"
    assert "оформил для вас бронь" in json.loads(_to(outbox, _CLIENT)[0][2])["body"]["text"]


def test_manager_batch_respects_the_prepayment_override(api, apipay, outbox):
    body = {"phone": _WA, "prepayment": 5000, "slots": [_slot(1, "2027-10-02")]}
    api.post("/api/manager/bookings/batch", json=body, headers=_HDR)
    _say("Подтвердить")
    _say("✅ Согласен")
    assert apipay["created"][0]["amount"] == 5000


def test_client_cancels_the_manager_booking_at_the_first_step(api, apipay, outbox):
    r = api.post("/api/manager/bookings/batch",
                 json={"phone": _WA, "slots": [_slot(1, "2027-10-19")]}, headers=_HDR)

    assert "бронь отменена" in _say("❌ Отменить")

    assert _state(r.get_json()["booking_ids"][0]) == "cancelled"
    assert apipay["created"] == [] and apipay["checked"] == []


def test_manager_single_booking_also_waits_for_consent(api, apipay, outbox):
    body = {"field": 1, "date": "2027-10-03", "time_start": "10:00", "time_end": "11:00",
            "phone": _WA, "customer": "Иван"}

    r = api.post("/api/manager/bookings", json=body, headers=_HDR)

    assert r.status_code == 200, r.get_json()
    bid = r.get_json()["data"]["booking_id"]
    assert _state(bid) == "awaiting_consent"
    assert apipay["created"] == []
    assert _to(outbox, _CLIENT), "the client was asked"


def test_manager_payment_number_is_billed_without_asking(api, apipay):
    body = {"phone": _WA, "kaspi_phone": "+7 747 999 88 77", "slots": [_slot(1, "2027-10-13")]}
    api.post("/api/manager/bookings/batch", json=body, headers=_HDR)

    _say("Подтвердить")
    assert "отправлен в Kaspi" in _say("✅ Согласен")
    assert apipay["created"] == [{"phone": _THIRD, "amount": 10000}]


def test_manager_payment_number_must_be_valid(api, apipay):
    body = {"phone": _WA, "kaspi_phone": "12345", "slots": [_slot(1, "2027-10-14")]}
    r = api.post("/api/manager/bookings/batch", json=body, headers=_HDR)
    assert r.status_code == 400 and r.get_json()["code"] == "INVALID"


def test_manager_single_repeating_booking_is_unchanged(api, apipay):
    """A repeating booking carries no avans — nothing to consent to."""
    body = {"field": 1, "date": "2027-10-04", "time_start": "10:00", "time_end": "11:00",
            "repeat": "weekly", "end_date": "2027-10-18", "phone": _WA}
    r = api.post("/api/manager/bookings", json=body, headers=_HDR)
    assert r.status_code == 200, r.get_json()
    assert _state(r.get_json()["data"]["booking_id"]) == "confirmed"
    assert consent_flow.get_active_request(_WA) is None


def test_manager_batch_outside_24h_uses_the_template(api, apipay, monkeypatch, outbox):
    monkeypatch.setattr(config, "WA_TEMPLATES", {**config.WA_TEMPLATES,
                                                 "booking_confirm_request": "dopsy_confirm"})
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO whatsapp_last_inbound (phone, at) "
                        "VALUES ('77001234567', NOW() - interval '2 days')")

    r = api.post("/api/manager/bookings/batch",
                 json={"phone": _WA, "slots": [_slot(1, "2027-10-05")]}, headers=_HDR)

    (to, kind, name, params), = _to(outbox, _CLIENT)
    assert kind == "template" and name == "dopsy_confirm"
    assert len(params) == 1 and "поле 1" in params[0]
    req = consent_flow.get_request(r.get_json()["consent"]["request_id"])
    assert req["prompt_delivery"] == "template"
    # The template's quick reply comes back as a plain button title — and
    # opens the window, so the rest of the dialog is free-form.
    from integrations import client_notify
    client_notify.touch_inbound(_CLIENT)
    assert config.OFFER_URL in _say("Подтвердить")
    assert "отправлен в Kaspi" in _say("✅ Согласен")


def test_inside_24h_the_free_form_message_is_sent(api, apipay, monkeypatch, outbox):
    monkeypatch.setattr(config, "WA_TEMPLATES", {**config.WA_TEMPLATES,
                                                 "booking_confirm_request": "dopsy_confirm"})
    from integrations import client_notify
    client_notify.touch_inbound(_CLIENT)
    api.post("/api/manager/bookings/batch",
             json={"phone": _WA, "slots": [_slot(1, "2027-10-15")]}, headers=_HDR)
    assert _to(outbox, _CLIENT)[0][1] == "text"


def test_reschedule_tells_the_client(api, apipay, monkeypatch, outbox):
    monkeypatch.setattr(config, "WA_TEMPLATES", {**config.WA_TEMPLATES,
                                                 "booking_edited": "dopsy_edited"})
    bid, _ = _confirm(monkeypatch, "2027-10-16")
    outbox.clear()

    r = api.patch(f"/api/manager/bookings/{bid}", json={"time_start": "20:00", "time_end": "21:00"},
                  headers=_HDR)

    assert r.status_code == 200, r.get_json()
    (to, kind, name, params), = _to(outbox, _CLIENT)
    assert name == "dopsy_edited" and params == [params[0]] and "20:00" in params[0], \
        "the approved template has one variable: the booking as it is now"


def test_notes_edit_says_nothing(api, apipay, monkeypatch, outbox):
    bid, _ = _confirm(monkeypatch, "2027-10-17")
    outbox.clear()
    api.patch(f"/api/manager/bookings/{bid}", json={"notes": "мяч свой"}, headers=_HDR)
    assert _to(outbox, _CLIENT) == []


# ---------------------------------------------------------------------------
# Website
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("consent", [None, False, {"accepted": False}, {"accepted": "yes"}])
def test_site_without_consent_is_rejected(api, apipay, consent):
    body = {"channel": "site", "phone": _WA, "slots": [_slot(1, "2027-10-06")]}
    if consent is not None:
        body["consent"] = consent
    r = api.post("/api/manager/bookings/batch", json=body, headers=_HDR)
    assert r.status_code == 400 and r.get_json()["code"] == "CONSENT_REQUIRED"
    assert apipay["created"] == []


def test_site_with_consent_bills_the_kaspi_number_at_once(api, apipay, outbox):
    _customer(_WA)
    body = {"phone": _WA, "kaspi_phone": "+7 707 111 22 33",
            "consent": {"accepted": True, "offer_version": "2026-10-31", "text_version": "site-v1"},
            "slots": [_slot(1, "2027-10-07")]}

    r = api.post("/api/manager/bookings/batch", json=body, headers=_HDR)

    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["payment"] == {"method": "kaspi_invoice", "phone": _OTHER, "amount": 10000}
    assert apipay["checked"] == [_OTHER]
    assert apipay["created"] == [{"phone": _OTHER, "amount": 10000}]
    row = _row(data["booking_ids"][0])
    assert row["consent_channel"] == "site" and row["consent_at"] is not None
    assert row["consent_text_version"] == "site-v1" and row["kaspi_phone"] == _OTHER
    assert row["payment_method"] == "kaspi_invoice"
    assert _customer_kaspi(_WA) == _OTHER
    assert "Бронь создана" in _to(outbox, _CLIENT)[0][2], "booking-created message to WhatsApp"


def test_site_kaspi_number_without_kaspi_pays_by_link(api, apipay, outbox):
    apipay["no_kaspi"].add(_OTHER)
    body = {"phone": _WA, "kaspi_phone": _OTHER, "consent": {"accepted": True},
            "slots": [_slot(1, "2027-10-08")]}

    r = api.post("/api/manager/bookings/batch", json=body, headers=_HDR)

    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["payment"] == {"method": "link", "url": config.KASPI_PAYMENT_URL,
                               "amount": 10000, "reason": "no_kaspi"}
    assert data["invoice"] is None and apipay["created"] == []
    row = _row(data["booking_ids"][0])
    assert row["state"] == "awaiting_payment" and row["payment_method"] == "link"
    assert row["consent_channel"] == "site"
    assert config.KASPI_PAYMENT_URL in _to(outbox, _CLIENT)[0][2]
    assert "+7 707 111 22 33" in _to(outbox, _ADMIN)[0][2]


# ---------------------------------------------------------------------------
# Admin card and the admin invoice endpoint
# ---------------------------------------------------------------------------

def test_admin_invoice_endpoint_needs_consent(api, apipay, monkeypatch):
    bid, _ = _confirm(monkeypatch, "2027-10-09")

    r = api.post(f"/api/manager/bookings/{bid}/invoice", json={"kaspi_phone": _OTHER},
                 headers=_HDR)

    assert r.status_code == 409 and r.get_json()["code"] == "CONSENT_REQUIRED"
    assert apipay["created"] == []


def test_admin_bills_a_corrected_number_after_the_link(api, apipay, monkeypatch, outbox):
    apipay["no_kaspi"].update({_WA, _OTHER})
    bid, _ = _confirm(monkeypatch, "2027-10-10")
    _say("✅ Согласен")
    _say(_OTHER)
    _say(_OTHER)                                       # 2 asks used up → link
    outbox.clear()

    r = api.post(f"/api/manager/bookings/{bid}/invoice", json={"kaspi_phone": _THIRD},
                 headers=_HDR)

    assert r.status_code == 200, r.get_json()
    assert apipay["created"] == [{"phone": _THIRD, "amount": 10000}]
    row = _row(bid)
    assert row["state"] == "awaiting_payment" and row["payment_method"] == "kaspi_invoice"
    assert "отправлен в Kaspi" in _to(outbox, _CLIENT)[0][2]

    card = api.get(f"/api/manager/bookings/{bid}", headers=_HDR).get_json()["data"]
    assert card["consent_channel"] == "bot" and card["consent_at"]
    assert card["kaspi_phone"] == _THIRD and card["payment_method"] == "kaspi_invoice"
    assert card["offer_version"] == config.OFFER_VERSION
    assert card["apipay_invoice_id"] == apipay["next_id"]
    assert card["consent_step"] == "done"


def test_awaiting_consent_is_listed_for_managers(api, apipay, monkeypatch):
    bid, _ = _confirm(monkeypatch, "2027-10-11")
    r = api.get("/api/manager/bookings?from=2027-10-01&to=2027-10-31", headers=_HDR)
    rows = {b["id"]: b for b in r.get_json()["data"]}
    assert rows[bid]["state"] == "awaiting_consent"


def test_admin_alert_template_has_three_variables(apipay, monkeypatch, outbox):
    """Meta rejects a short template with many variables: the alert sends
    exactly three (reason, client, booking) into a long fixed text."""
    monkeypatch.setattr(config, "WA_TEMPLATES", {**config.WA_TEMPLATES,
                                                 "admin_alert": "dopsy_admin_alert"})
    apipay["no_kaspi"].update({_WA, _OTHER, _THIRD})
    _confirm(monkeypatch, "2027-10-20")
    _say("✅ Согласен")
    _say(_OTHER)
    _say(_THIRD)

    (to, kind, name, params), = _to(outbox, _ADMIN)
    assert kind == "template" and name == "dopsy_admin_alert"
    reason, client, booking = params
    assert "ни на одном из номеров" in reason
    assert "+7 700 123 45 67" in client and "+7 747 999 88 77" in client
    assert "поле 1" in booking


@pytest.mark.no_db
@pytest.mark.parametrize("text,expected", [
    ("Подтвердить", "yes"), ("Растау", "yes"), ("✅ Подтвердить", "yes"),
    ("Отменить", "no"), ("Болдырмау", "no"), ("❌ Болдырмау", "no"),
    ("а где поле?", None),
])
def test_template_button_words(text, expected):
    assert consent_flow.parse_confirm(text) == expected


@pytest.mark.no_db
def test_kazakh_template_variables_are_translated():
    from integrations import client_notify as cn
    assert cn.localize_param("📅 5 ноября, 18:00–19:00 · поле 1", "kk") == \
        "📅 5 қараша, 18:00–19:00 · 1-алаң"
    assert cn.localize_param("оплата не поступила вовремя", "kk") == "төлем уақытында түспеді"
    assert cn.localize_param("подтверждение не получено", "kk") == "растау алынбады"
    assert cn.localize_param("счёт на аванс 10 000₸ отправлен в Kaspi на номер +7 707 111 22 33",
                             "kk") == "10 000₸ аванс шоты Kaspi-ге (+7 707 111 22 33) жіберілді"
    assert cn.localize_param("📅 5 ноября · поле 1", "ru") == "📅 5 ноября · поле 1"


def test_kazakh_client_gets_the_kazakh_template(api, apipay, monkeypatch, outbox):
    monkeypatch.setattr(config, "WA_TEMPLATES", {**config.WA_TEMPLATES,
                                                 "booking_confirm_request": "booking_confirm_request"})
    from handlers import whatsapp_client
    langs = []
    monkeypatch.setattr(whatsapp_client, "send_template_message",
                        lambda ch, to, name, lang, params: (
                            langs.append(lang), outbox.append((to, "template", name, params))))
    r = api.post("/api/manager/bookings/batch",
                 json={"phone": _WA, "lang": "kk", "slots": [_slot(1, "2027-11-05")]}, headers=_HDR)
    assert r.status_code == 200

    assert langs == ["kk"]
    (to, kind, name, params), = _to(outbox, _CLIENT)
    assert kind == "template" and "қараша" in params[0] and "1-алаң" in params[0]
    assert "Kaspi" not in params[0]
    # The Kazakh quick reply leads into the same dialog.
    from integrations import client_notify
    client_notify.touch_inbound(_CLIENT)
    assert config.OFFER_URL in _say("Растау")


def test_missing_kazakh_template_falls_back_to_russian(apipay, monkeypatch):
    from handlers import whatsapp_client
    from integrations import client_notify
    monkeypatch.setattr(config, "WA_TEMPLATES", {**config.WA_TEMPLATES,
                                                 "booking_cancelled": "dopsy_cancelled"})
    tried = []

    def send(ch, to, name, lang, params):
        tried.append((lang, params[1]))
        if lang == "kk":
            raise RuntimeError("template language not found")
    monkeypatch.setattr(whatsapp_client, "send_template_message", send)

    assert client_notify.send(_WA, "ttl_unpaid", "kk", slots="📅 5 ноября · поле 1")
    assert tried == [("kk", "төлем уақытында түспеді"), ("ru", "оплата не поступила вовремя")]
