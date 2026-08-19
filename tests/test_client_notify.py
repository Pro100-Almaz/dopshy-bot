"""Tests for the client-notification layer.

No DB and no network: rendering and phone normalization are pure, and they are
where these messages actually go wrong. A number in the wrong shape is rejected
by the provider with a 400 the client never sees, so the booking silently
changes behind their back — the exact failure the module exists to prevent.
"""

import pytest

from integrations import client_notify


# ---------------------------------------------------------------------------
# Phone normalization — providers take E.164, our data mostly is not
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "87001234567",          # ApiPay's stored form, and what managers type
    "+7 (700) 123-45-67",   # sheet-typed, punctuated
    "77001234567",          # already international, no plus
    "+77001234567",         # already E.164 — must survive untouched
    "7001234567",           # national number, no prefix at all
    " 8 700 123 45 67 ",
])
def test_kz_numbers_reach_e164(raw):
    assert client_notify.normalize_recipient(raw) == "+77001234567"


@pytest.mark.parametrize("raw", ["", None, "   ", "—"])
def test_no_number_is_not_a_number(raw):
    """Walk-in bookings carry no phone. There is nobody to tell, which is a
    no-op — not a crash, and not a send to a garbage address."""
    assert client_notify.normalize_recipient(raw) is None


def test_an_international_number_is_left_alone():
    assert client_notify.normalize_recipient("+1 202 555 0100") == "+12025550100"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_every_message_is_russian_only():
    """The catalogue carries one language; a stray key would ship untranslated
    text to a client."""
    for key, texts in client_notify.MESSAGES.items():
        assert set(texts) == {"ru"}, key


def test_any_language_answers_in_russian():
    """Manager actions carry no language — nobody asked the client anything —
    and a `notify_lang` with no entry (including 'kk', still stored against
    older invoices) must not cost them the notification."""
    for lang in (None, "ru", "kk", "en", ""):
        text = client_notify.render("apipay_paid", lang, amount="10 000₸")
        assert "Оплата получена" in text


def test_amounts_read_as_money():
    assert client_notify.fmt_amount(20000) == "20 000₸"
    assert client_notify.fmt_amount("10000.00") == "10 000₸"


def test_a_long_series_is_summarised_not_dumped():
    """52 weekly slots must not become a message nobody scrolls to the end of."""
    rows = [{"date": f"2026-01-{d:02d}", "time_start": "10:00:00",
             "time_end": "11:00:00", "field": 1} for d in range(1, 21)]

    text = client_notify.fmt_slots(rows)

    assert text.count("📅") == 8
    assert "и ещё 12" in text


def test_a_missing_row_does_not_break_the_block():
    assert client_notify.fmt_slots([]) == ""
    assert client_notify.fmt_slots([None]) == ""


def test_send_without_a_recipient_is_a_no_op(monkeypatch):
    """It must not raise, and it must not reach the provider with junk."""
    calls = []
    monkeypatch.setattr("handlers.whatsapp_client.send_text_message",
                        lambda *a, **kw: calls.append(a))

    assert client_notify.send(None, "apipay_paid", amount="1₸") is False
    assert calls == []


def test_a_provider_failure_never_reaches_the_caller(monkeypatch):
    """The booking change this reports is already committed — a WhatsApp outage
    must not turn it into a 500, or make ApiPay retry a settled webhook."""
    def boom(*a, **kw):
        raise RuntimeError("ycloud is down")

    monkeypatch.setattr("handlers.whatsapp_client.send_text_message", boom)

    assert client_notify.send("87001234567", "apipay_paid", amount="1₸") is False
