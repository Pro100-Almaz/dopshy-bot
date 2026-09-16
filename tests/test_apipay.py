"""Tests for the ApiPay.kz avans integration.

Covers the things that can silently go wrong:
  * how much is charged (one invoice per batch, repeating slots excluded),
  * the outbox (a failed send keeps the row, retries reuse the same
    external_order_id, released slots are never billed),
  * the webhook (signature, idempotent redelivery, state transitions, and
    resolution by external_order_id when it outruns our own send),
  * the bot flow (queued inside the reservation transaction, paid → confirmed
    → the client is told, in their language).

No test talks to ApiPay: `integrations.apipay_client` is monkeypatched.
"""

import hashlib
import hmac
import json
import time
import uuid

import pytest
from flask import Flask

import config
from blueprints import apipay_webhook as webhook_bp
from blueprints.manager_api import manager_api
from integrations import apipay_client, apipay_service
from integrations.apipay_client import ApiPayError, normalize_phone, verify_webhook_signature
from integrations.repo.postgres import _conn

_KEY = "test-key"
_HDR = {"X-API-Key": _KEY}
_SECRET = "whsec-test"
_PHONE = "+7 (700) 123-45-67"


# ---------------------------------------------------------------------------
# Pure helpers — no DB, no network
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "+7 (700) 123-45-67", "77001234567", "87001234567", "8 700 123 45 67", "7001234567",
])
def test_normalize_phone_accepts_kz_forms(raw):
    assert normalize_phone(raw) == "87001234567"


@pytest.mark.parametrize("raw", ["", None, "12345", "+1 202 555 0100", "abc"])
def test_normalize_phone_rejects_junk(raw):
    with pytest.raises(ApiPayError):
        normalize_phone(raw)


def test_signature_verified_over_raw_body(monkeypatch):
    monkeypatch.setattr(config, "APIPAY_WEBHOOK_SECRET", _SECRET)
    body = b'{"event":"invoice.status_changed","invoice":{"id":42}}'
    good = "sha256=" + hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()

    assert verify_webhook_signature(body, good)
    assert not verify_webhook_signature(body, "sha256=deadbeef")
    assert not verify_webhook_signature(body, None)
    assert not verify_webhook_signature(body + b" ", good)   # body tampered


def test_signature_rejected_when_secret_missing(monkeypatch):
    monkeypatch.setattr(config, "APIPAY_WEBHOOK_SECRET", "")
    assert not verify_webhook_signature(b"{}", "sha256=whatever")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def apipay_on(monkeypatch):
    """Enable ApiPay and stub out the HTTP layer. Yields the call recorder."""
    monkeypatch.setattr(config, "APIPAY_ENABLED", True)
    monkeypatch.setattr(config, "APIPAY_API_KEY", "test-api-key")
    monkeypatch.setattr(config, "APIPAY_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setattr(config, "APIPAY_AVANS_PER_BOOKING", 10000)
    monkeypatch.setattr(config, "APIPAY_CHECK_CLIENT", True)

    # `remote` is what ApiPay would answer a GET with, keyed by invoice id —
    # set it to simulate a status we were never told about.
    # `no_kaspi` holds numbers /clients/check should report as unregistered.
    calls = {"created": [], "cancelled": [], "fetched": [], "remote": {},
             "checked": [], "no_kaspi": set(), "next_id": 4200}

    def fake_check(phone):
        normalized = apipay_client.normalize_phone(phone)
        calls["checked"].append(normalized)
        has_kaspi = normalized not in calls["no_kaspi"]
        return {"phone": normalized, "has_kaspi": has_kaspi,
                "client_name": "Иван И." if has_kaspi else None}

    monkeypatch.setattr(apipay_client, "check_client", fake_check)

    def fake_create(phone, amount, description, external_order_id):
        calls["next_id"] += 1
        calls["created"].append({"phone": phone, "amount": amount,
                                 "description": description,
                                 "external_order_id": external_order_id})
        return {"id": calls["next_id"], "amount": amount, "status": "processing",
                "phone": phone}

    def fake_get(invoice_id):
        calls["fetched"].append(invoice_id)
        return calls["remote"].get(invoice_id, {"id": invoice_id,
                                                "status": "processing"})

    monkeypatch.setattr(apipay_client, "create_invoice", fake_create)
    monkeypatch.setattr(apipay_client, "get_invoice", fake_get)
    monkeypatch.setattr(apipay_client, "cancel_invoice",
                        lambda iid: calls["cancelled"].append(iid))
    return calls


@pytest.fixture
def client(monkeypatch):
    config.X_SERVICE_TOKEN = _KEY
    # The Google Sheet is a view, irrelevant here — and must not be written to.
    monkeypatch.setattr("blueprints.manager_api._single_table_write", lambda row: None)
    monkeypatch.setattr("blueprints.manager_api._single_table_erase", lambda row: None)
    monkeypatch.setattr("blueprints.manager_api.upsert_booking_row", lambda row: None)
    monkeypatch.setattr("blueprints.manager_api.refresh_week_sheet", lambda: None)
    app = Flask(__name__)
    app.register_blueprint(manager_api)
    return app.test_client()


@pytest.fixture
def hook_client(monkeypatch):
    monkeypatch.setattr(apipay_service, "sync_sheets", lambda ids, released: None)
    app = Flask(__name__)
    app.register_blueprint(webhook_bp.apipay_webhook)
    return app.test_client()


def _post_webhook(hook_client, invoice_id, status, event="invoice.status_changed",
                  secret=_SECRET, body=None):
    payload = body if body is not None else {
        "event": event,
        "invoice": {"id": invoice_id, "status": status, "amount": "20000.00",
                    "paid_at": "2026-08-13T08:35:00Z"},
        "source": "test-key",
    }
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hook_client.post("/webhooks/apipay", data=raw,
                            headers={"Content-Type": "application/json",
                                     "X-Webhook-Signature": sig})


def _slot(field, date, ts="10:00", te="11:00", **extra):
    return {"field": field, "date": date, "time_start": ts, "time_end": te, **extra}


def _states(booking_ids):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, state FROM bookings WHERE id = ANY(%s)",
                        (list(booking_ids),))
            return dict(cur.fetchall())


def _states_on_date(date):
    """Same, by date — for asserting that a refused request created nothing."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, state FROM bookings WHERE date = %s", (date,))
            return dict(cur.fetchall())


def _avans(booking_id):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT paid_avans FROM bookings WHERE id = %s", (booking_id,))
            return float(cur.fetchone()[0])


def _invoice_count():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM apipay_invoices")
            return cur.fetchone()[0]


def _invoice_row(external_order_id):
    import psycopg2.extras
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM apipay_invoices WHERE external_order_id = %s",
                        (external_order_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def _invoice_for_booking(booking_id):
    """The invoice covering a booking — for flows that don't hand the row back."""
    import psycopg2.extras
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM apipay_invoices WHERE %s = ANY(booking_ids)",
                        (booking_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def _history(booking_id):
    """(source, description) of every booking_history row, oldest first."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source, description FROM booking_history "
                " WHERE booking_id = %s ORDER BY id", (booking_id,))
            return [(r[0], r[1]) for r in cur.fetchall()]


def _draft(date="2027-07-01", ts="18:00", te="19:00", field=1, phone="87001234567"):
    """A draft booking, inserted directly — the bot's starting point."""
    token = str(uuid.uuid4())
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bookings (phone, customer_name, date, time_start, time_end, "
                "  field, format, players, state, source, client_token) "
                "VALUES (%s, 'Клиент', %s, %s, %s, %s, '5x5', 8, 'draft', 'whatsapp', %s) "
                "RETURNING id",
                (phone, date, ts, te, field, token),
            )
            return cur.fetchone()[0], token


def _apipay_outage(apipay_on, monkeypatch):
    """Make ApiPay unreachable until the returned dict's ``down`` is cleared.

    Delegates to the recorder's fake rather than using monkeypatch.undo(),
    which would also unwind the `apipay_on` fixture's own patches.
    """
    fake = apipay_client.create_invoice
    state = {"down": True}

    def flaky(**kwargs):
        if state["down"]:
            raise ApiPayError("ApiPay недоступен: timeout")
        return fake(**kwargs)

    monkeypatch.setattr(apipay_client, "create_invoice", flaky)
    return state


def _queued_batch(slots, phone=_PHONE):
    """A committed batch whose invoice never reached ApiPay.

    What a worker that dies between the commit and the send leaves behind — and
    the only way to reach that state now, since a send that fails in front of a
    caller takes its bookings back instead of queueing them. Mirrors the manager
    endpoint exactly up to, but not including, `send_invoice`.
    """
    from integrations import apipay_service, booking_service

    res = booking_service.manager_create_bookings_batch(
        slots, phone=phone, actor_id="test@dopshy.kz",
        on_created=apipay_service.batch_invoice_hook(phone),
        avans_per_booking=config.APIPAY_AVANS_PER_BOOKING,
    )
    assert res["ok"], res
    return res["data"]


def _age_outbox(external_order_id):
    """Push a queued row past the send lease so a sweep will pick it up."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apipay_invoices SET updated_at = NOW() - interval '10 minutes' "
                "WHERE external_order_id = %s", (external_order_id,))


# ---------------------------------------------------------------------------
# Invoice creation on bookings/batch
# ---------------------------------------------------------------------------

def test_two_slots_make_one_invoice_of_20000(client, apipay_on):
    body = {"phone": _PHONE, "customer": "Асхат", "slots": [
        _slot(1, "2027-03-01", "10:00", "11:00"),
        _slot(1, "2027-03-01", "12:00", "13:00"),
    ]}
    r = client.post("/api/manager/bookings/batch", json=body, headers=_HDR)
    assert r.status_code == 200, r.get_json()
    data = r.get_json()

    assert len(apipay_on["created"]) == 1, "one total invoice, never one per booking"
    assert apipay_on["created"][0]["amount"] == 20000
    assert apipay_on["created"][0]["phone"] == "87001234567"

    assert data["invoice"]["amount"] == 20000
    assert data["invoice"]["id"] == apipay_on["next_id"]
    assert sorted(data["invoice"]["booking_ids"]) == sorted(data["booking_ids"])
    assert set(_states(data["booking_ids"]).values()) == {"awaiting_payment"}
    for bid in data["booking_ids"]:
        assert _avans(bid) == 10000


def test_single_slot_is_10000(client, apipay_on):
    body = {"phone": _PHONE, "slots": [_slot(2, "2027-03-02")]}
    r = client.post("/api/manager/bookings/batch", json=body, headers=_HDR)
    assert r.status_code == 200
    assert apipay_on["created"][0]["amount"] == 10000


def test_repeating_slots_are_not_charged(client, apipay_on):
    """A weekly slot expands to many bookings but adds nothing to the avans."""
    body = {"phone": _PHONE, "slots": [
        _slot(1, "2027-04-05", "10:00", "11:00"),
        _slot(2, "2027-04-05", "10:00", "11:00",
              repeat_mode="weekly", repeat_until="2027-04-26"),
    ]}
    r = client.post("/api/manager/bookings/batch", json=body, headers=_HDR)
    assert r.status_code == 200
    data = r.get_json()

    assert apipay_on["created"][0]["amount"] == 10000, "only the one-off slot is charged"
    assert data["created_count"] > 2, "the weekly slot still created its bookings"
    assert len(data["invoice"]["booking_ids"]) == 1


def test_all_repeating_batch_creates_no_invoice(client, apipay_on):
    body = {"phone": _PHONE, "slots": [
        _slot(1, "2027-05-03", repeat_mode="weekly", repeat_until="2027-05-24"),
    ]}
    r = client.post("/api/manager/bookings/batch", json=body, headers=_HDR)
    assert r.status_code == 200
    data = r.get_json()

    assert apipay_on["created"] == []
    assert data["invoice"] is None
    assert _invoice_count() == 0
    assert set(_states(data["booking_ids"]).values()) == {"awaiting_payment"}


def test_failed_send_cancels_the_batch_and_reports_the_error(client, apipay_on,
                                                             monkeypatch):
    """No invoice means no bookings, and the manager is told why.

    The batch is committed before ApiPay is called, so the rollback is a
    compensating cancel rather than an aborted transaction: the slots go back
    on the market and the API answers with ApiPay's own wording instead of a
    success the manager would have to notice was hollow.
    """
    def boom(**kwargs):
        raise ApiPayError("ApiPay недоступен: timeout")
    monkeypatch.setattr(apipay_client, "create_invoice", boom)

    body = {"phone": _PHONE, "slots": [
        _slot(1, "2027-06-07", "10:00", "11:00"),
        _slot(1, "2027-06-07", "12:00", "13:00"),
    ]}
    r = client.post("/api/manager/bookings/batch", json=body, headers=_HDR)
    assert r.status_code == 502, r.get_json()
    data = r.get_json()

    assert data["ok"] is False
    assert data["code"] == "PAYMENT_PROVIDER_ERROR"
    assert data["error"] == "apipay_error"
    assert "timeout" in data["error_message"], "ApiPay's wording reaches the manager"

    cancelled = data["cancelled_booking_ids"]
    assert len(cancelled) == 2
    assert set(_states(cancelled).values()) == {"cancelled"}

    # Retired, not deleted — a payment ApiPay took anyway must still land on a
    # known invoice. And the outbox must not resurrect it.
    row = _invoice_row(_invoice_for_booking(cancelled[0])["external_order_id"])
    assert row["status"] == "cancelled" and row["invoice_id"] is None
    assert "timeout" in row["last_send_error"]
    _age_outbox(row["external_order_id"])
    assert apipay_service.sweep_pending_sends() == 0


def test_failed_send_frees_the_slot_for_the_next_booking(client, apipay_on,
                                                          monkeypatch):
    """The cancel must drop out of the EXCLUDE constraint, not just look tidy."""
    # An outage that clears, NOT monkeypatch.undo(): undo would also unwind the
    # `apipay_on` fixture, and the retry would then reach the real ApiPay with
    # the real key from .env — creating an actual invoice from a test run.
    outage = _apipay_outage(apipay_on, monkeypatch)

    body = {"phone": _PHONE, "slots": [_slot(1, "2027-06-08", "10:00", "11:00")]}
    assert client.post("/api/manager/bookings/batch",
                       json=body, headers=_HDR).status_code == 502

    outage["down"] = False
    r = client.post("/api/manager/bookings/batch", json=body, headers=_HDR)
    assert r.status_code == 200, r.get_json()
    assert set(_states(r.get_json()["booking_ids"]).values()) == {"awaiting_payment"}


def test_failed_send_takes_back_the_repeating_slots_too(client, apipay_on,
                                                         monkeypatch):
    """One request, one outcome: uncharged repeats do not survive the failure."""
    def boom(**kwargs):
        raise ApiPayError("ApiPay недоступен: timeout")
    monkeypatch.setattr(apipay_client, "create_invoice", boom)

    body = {"phone": _PHONE, "slots": [
        _slot(1, "2027-06-14", "10:00", "11:00"),
        _slot(2, "2027-06-14", "10:00", "11:00",
              repeat_mode="weekly", repeat_until="2027-07-05"),
    ]}
    r = client.post("/api/manager/bookings/batch", json=body, headers=_HDR)
    assert r.status_code == 502, r.get_json()

    cancelled = r.get_json()["cancelled_booking_ids"]
    assert len(cancelled) > 2, "the weekly expansion is rolled back as well"
    assert set(_states(cancelled).values()) == {"cancelled"}


def test_outbox_sweep_resends_and_reuses_the_same_external_order_id(
        client, apipay_on, monkeypatch):
    """The retry must be the SAME order, or the client is charged twice."""
    from integrations import apipay_service

    data = _queued_batch([_slot(1, "2027-06-09")])
    eoid = data["invoice"]["external_order_id"]
    assert apipay_on["created"] == []

    _age_outbox(eoid)
    assert apipay_service.sweep_pending_sends() == 1

    assert len(apipay_on["created"]) == 1
    assert apipay_on["created"][0]["external_order_id"] == eoid
    row = _invoice_row(eoid)
    assert row["invoice_id"] == apipay_on["next_id"]
    assert row["status"] == "processing"


def test_outbox_sweep_skips_invoices_whose_bookings_are_gone(client, apipay_on,
                                                            monkeypatch):
    """Never ask for money for a slot the TTL already released."""
    from integrations import apipay_service

    data = _queued_batch([_slot(1, "2027-06-10")])
    eoid = data["invoice"]["external_order_id"]

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE bookings SET state = 'unpaid' WHERE id = ANY(%s)",
                        (data["booking_ids"],))

    _age_outbox(eoid)
    assert apipay_service.sweep_pending_sends() == 0
    assert apipay_on["created"] == [], "no invoice for a released slot"
    assert _invoice_row(eoid)["status"] == "cancelled"


def test_outbox_sweep_bills_only_the_bookings_that_survived(client, apipay_on,
                                                            monkeypatch):
    """A 2-slot batch that lost a slot before the send is charged 10 000, not 20 000."""
    from integrations import apipay_service

    data = _queued_batch([
        _slot(1, "2027-06-11", "10:00", "11:00"),
        _slot(1, "2027-06-11", "12:00", "13:00"),
    ])
    eoid = data["invoice"]["external_order_id"]
    assert _invoice_row(eoid)["amount"] == 20000

    # One booking cancelled in the window the cancellation paths leave open:
    # committed, but the queued invoice not yet retired.
    gone, kept = data["booking_ids"][0], data["booking_ids"][1]
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE bookings SET state = 'cancelled' WHERE id = %s", (gone,))

    _age_outbox(eoid)
    assert apipay_service.sweep_pending_sends() == 1

    assert apipay_on["created"][0]["amount"] == 10000, "never bill for a dead slot"
    row = _invoice_row(eoid)
    # The row must agree with what was charged: the webhook settles across it.
    assert row["amount"] == 10000
    assert row["booking_ids"] == [kept]


def test_outbox_sweep_drops_a_row_retired_while_it_was_held(client, apipay_on,
                                                            monkeypatch):
    """The reprice is the final claim: a cancelled row is never sent."""
    from integrations import apipay_service
    from integrations.repo import apipay_repo

    data = _queued_batch([_slot(1, "2027-06-12")])
    eoid = data["invoice"]["external_order_id"]

    # The bookings still await payment, so the survivor check passes — the row
    # itself was retired between the claim and the send.
    real_slots = apipay_repo.awaiting_payment_slots

    def cancel_then_check(booking_ids):
        apipay_repo.cancel_unsent_for_bookings(booking_ids, "paid_by_receipt")
        return real_slots(booking_ids)

    monkeypatch.setattr(apipay_repo, "awaiting_payment_slots", cancel_then_check)

    _age_outbox(eoid)
    assert apipay_service.sweep_pending_sends() == 0
    assert apipay_on["created"] == []
    assert _invoice_row(eoid)["status"] == "cancelled"


def test_webhook_resolves_by_external_order_id_before_the_send_lands(
        client, hook_client, apipay_on, monkeypatch):
    """The race the outbox introduces: their webhook can beat our own record.

    `invoice_id` is written only after `POST /invoices` returns. If a paid
    webhook arrives first and we keyed on it, the transition would be dropped,
    answered 200, and never retried — a paid booking stranded in
    awaiting_payment. `external_order_id` is committed before the call, so it
    resolves regardless of ordering.

    Staged through the queue alone (`request_payment`, no send), which is the
    only way a live row still carries a NULL `invoice_id`: a send that fails in
    front of a caller now takes its bookings back instead of leaving them.
    """
    from integrations import booking_service

    bid, token = _draft(date="2027-06-11")
    res = booking_service.request_payment(bid, token, on_reserved=_bot_hook())
    eoid = res["data"]["invoice"]["external_order_id"]
    assert _invoice_row(eoid)["invoice_id"] is None

    payload = {
        "event": "invoice.status_changed",
        "invoice": {"id": 987654, "status": "paid", "amount": "10000.00",
                    "external_order_id": eoid, "paid_at": "2026-08-13T08:35:00Z"},
    }
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    r = hook_client.post("/webhooks/apipay", data=raw,
                         headers={"Content-Type": "application/json",
                                  "X-Webhook-Signature": sig})

    assert r.status_code == 200
    assert _states([bid]) == {bid: "confirmed"}
    row = _invoice_row(eoid)
    assert row["status"] == "paid"
    assert row["invoice_id"] == 987654, "the webhook backfills the id we never got"


def test_missing_phone_is_rejected_before_anything_is_created(client, apipay_on):
    body = {"slots": [_slot(1, "2027-06-08")]}
    r = client.post("/api/manager/bookings/batch", json=body, headers=_HDR)
    assert r.status_code == 400
    assert apipay_on["created"] == []
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM bookings WHERE date = '2027-06-08'")
            assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Kaspi registration check — the failure that must NOT be found out by webhook
# ---------------------------------------------------------------------------

def test_batch_is_refused_when_the_number_has_no_kaspi(client, apipay_on):
    """Nothing is created, nothing is sent, and the manager is told in the reply.

    Without the check the invoice would be accepted, spend one of the day's
    invoice slots, and only fail later as a webhook — by then this request has
    answered 200 and nobody is listening.
    """
    apipay_on["no_kaspi"].add("87001234567")
    body = {"phone": _PHONE, "slots": [_slot(1, "2027-06-11")]}

    r = client.post("/api/manager/bookings/batch", json=body, headers=_HDR)

    assert r.status_code == 400
    payload = r.get_json()
    assert payload["code"] == "NO_KASPI"
    assert "Kaspi" in payload["message"]
    assert apipay_on["checked"] == ["87001234567"]
    assert apipay_on["created"] == [], "the quota is never spent on a doomed invoice"
    assert _invoice_count() == 0
    assert _states_on_date("2027-06-11") == {}


def test_batch_is_refused_when_the_check_itself_fails(client, apipay_on, monkeypatch):
    """An unanswerable check is fatal too — before anything is written.

    Reserving on the assumption that the number is fine would only move the
    failure to `send_invoice`, which takes the whole batch back again.
    """
    def down(phone):
        raise ApiPayError("ApiPay недоступен: timeout")

    monkeypatch.setattr(apipay_client, "check_client", down)
    body = {"phone": _PHONE, "slots": [_slot(1, "2027-06-12")]}

    r = client.post("/api/manager/bookings/batch", json=body, headers=_HDR)

    assert r.status_code == 502
    assert r.get_json()["code"] == "PAYMENT_PROVIDER_ERROR"
    assert apipay_on["created"] == []
    assert _states_on_date("2027-06-12") == {}


def test_repeating_only_batch_is_not_checked(client, apipay_on):
    """No avans → no invoice → no reason to ask Kaspi anything."""
    body = {"slots": [_slot(1, "2027-06-13", repeat_mode="weekly",
                            repeat_until="2027-06-27")]}
    r = client.post("/api/manager/bookings/batch", json=body, headers=_HDR)

    assert r.status_code == 200
    assert apipay_on["checked"] == []


def test_check_can_be_switched_off(client, apipay_on, monkeypatch):
    """The escape hatch: the flow still works with the endpoint out of the way."""
    monkeypatch.setattr(config, "APIPAY_CHECK_CLIENT", False)
    apipay_on["no_kaspi"].add("87001234567")   # would be refused if consulted

    body = {"phone": _PHONE, "slots": [_slot(1, "2027-06-14")]}
    r = client.post("/api/manager/bookings/batch", json=body, headers=_HDR)

    assert r.status_code == 200
    assert apipay_on["checked"] == []
    assert len(apipay_on["created"]) == 1


def test_batch_still_works_when_apipay_is_disabled(client, monkeypatch):
    monkeypatch.setattr(config, "APIPAY_ENABLED", False)
    body = {"phone": _PHONE, "slots": [_slot(3, "2027-06-09")]}
    r = client.post("/api/manager/bookings/batch", json=body, headers=_HDR)
    assert r.status_code == 200
    assert r.get_json()["invoice"] is None
    assert _invoice_count() == 0


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

def _make_batch(client, date="2027-07-01", extra_slots=()):
    body = {"phone": _PHONE, "customer": "Асхат", "slots": [
        _slot(1, date, "10:00", "11:00"),
        _slot(1, date, "12:00", "13:00"),
        *extra_slots,
    ]}
    r = client.post("/api/manager/bookings/batch", json=body, headers=_HDR)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_paid_webhook_confirms_the_bookings(client, hook_client, apipay_on):
    data = _make_batch(client)
    invoice_id = data["invoice"]["id"]

    r = _post_webhook(hook_client, invoice_id, "paid")
    assert r.status_code == 200
    assert set(_states(data["booking_ids"]).values()) == {"confirmed"}

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT SUM(amount) FROM payments WHERE method = 'apipay'")
            assert float(cur.fetchone()[0]) == 20000.0


def test_paid_webhook_leaves_repeating_bookings_awaiting_payment(client, hook_client, apipay_on):
    """A mixed batch: payment confirms only what the invoice covered."""
    data = _make_batch(client, date="2027-07-08", extra_slots=[
        _slot(2, "2027-07-08", "10:00", "11:00",
              repeat_mode="weekly", repeat_until="2027-07-29"),
    ])
    charged = set(data["invoice"]["booking_ids"])
    uncharged = set(data["booking_ids"]) - charged
    assert uncharged, "the weekly slot should have produced extra bookings"

    _post_webhook(hook_client, data["invoice"]["id"], "paid")

    states = _states(data["booking_ids"])
    assert {states[b] for b in charged} == {"confirmed"}
    assert {states[b] for b in uncharged} == {"awaiting_payment"}


def test_real_apipay_payload_shape(client, hook_client, apipay_on):
    """The exact `invoice.status_changed` body ApiPay documents, field for field.

    Guards against a field rename silently turning every payment into a no-op.
    """
    data = _make_batch(client, date="2027-11-04")
    invoice_id = data["invoice"]["id"]

    payload = {
        "event": "invoice.status_changed",
        "invoice": {
            "id": invoice_id,
            "external_order_id": data["invoice"]["external_order_id"],
            "amount": "20000.00",
            "status": "paid",
            "description": "Оплата заказа",
            "kaspi_invoice_id": "13234689513",
            "client_name": "Иван Иванов",
            "client_phone": "87071234567",
            "paid_at": "2025-12-25T14:35:00Z",
        },
        "timestamp": "2025-12-25T14:35:01Z",
    }
    r = _post_webhook(hook_client, None, None, body=payload)

    assert r.status_code == 200
    assert sorted(r.get_json()["bookings"]) == sorted(data["booking_ids"])
    assert set(_states(data["booking_ids"]).values()) == {"confirmed"}

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, kaspi_invoice_id, paid_at FROM apipay_invoices "
                        "WHERE invoice_id = %s", (invoice_id,))
            status, kaspi_id, paid_at = cur.fetchone()
    assert status == "paid"
    assert kaspi_id == "13234689513", "Kaspi's id is needed for manual refunds"
    assert paid_at is not None, "paid_at from the payload must be stored"


def test_webhook_redelivery_is_idempotent(client, hook_client, apipay_on):
    data = _make_batch(client, date="2027-07-15")
    invoice_id = data["invoice"]["id"]

    first = _post_webhook(hook_client, invoice_id, "paid")
    second = _post_webhook(hook_client, invoice_id, "paid")
    assert first.status_code == second.status_code == 200
    assert second.get_json()["duplicate"] is True

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM payments WHERE method = 'apipay'")
            assert cur.fetchone()[0] == len(data["booking_ids"]), "no double payments"


def test_cancelled_webhook_releases_the_slots(client, hook_client, apipay_on):
    data = _make_batch(client, date="2027-07-22")
    r = _post_webhook(hook_client, data["invoice"]["id"], "cancelled")
    assert r.status_code == 200
    assert set(_states(data["booking_ids"]).values()) == {"unpaid"}


def test_expired_webhook_releases_the_slots(client, hook_client, apipay_on):
    data = _make_batch(client, date="2027-07-23")
    _post_webhook(hook_client, data["invoice"]["id"], "expired")
    assert set(_states(data["booking_ids"]).values()) == {"unpaid"}


def test_bad_signature_is_rejected_and_changes_nothing(client, hook_client, apipay_on):
    data = _make_batch(client, date="2027-07-29")
    r = _post_webhook(hook_client, data["invoice"]["id"], "paid", secret="wrong-secret")
    assert r.status_code == 401
    assert set(_states(data["booking_ids"]).values()) == {"awaiting_payment"}


def test_test_event_is_acknowledged(hook_client, apipay_on):
    r = _post_webhook(hook_client, None, None, body={"event": "webhook.test"})
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


def test_unknown_invoice_is_acknowledged_not_retried(hook_client, apipay_on):
    """Answering 2xx stops ApiPay's 11-attempt retry storm for an invoice we never made."""
    r = _post_webhook(hook_client, 999999, "paid")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# TTL sweeper
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Reconciliation — the safety net for a webhook that never arrived
# ---------------------------------------------------------------------------

@pytest.fixture
def quiet_sheets(monkeypatch):
    """The poller finishes a transition itself, sheet sync included."""
    monkeypatch.setattr(apipay_service, "sync_sheets", lambda ids, released: None)


def test_reconcile_confirms_a_payment_whose_webhook_was_lost(client, apipay_on,
                                                             quiet_sheets):
    """The client paid, the delivery never reached us — poll and find it anyway."""
    data = _make_batch(client, date="2027-08-01")
    eoid = data["invoice"]["external_order_id"]
    apipay_on["remote"][data["invoice"]["id"]] = {
        "id": data["invoice"]["id"], "status": "paid",
        "paid_at": "2026-08-13T08:35:00Z", "kaspi_invoice_id": "KZ-9001",
    }

    _age_outbox(eoid)   # nothing has happened to the row for 10 minutes
    assert apipay_service.reconcile_open_invoices() == 1

    assert set(_states(data["booking_ids"]).values()) == {"confirmed"}
    row = _invoice_row(eoid)
    assert row["status"] == "paid" and row["kaspi_invoice_id"] == "KZ-9001"
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT SUM(amount) FROM payments WHERE method = 'apipay'")
            assert float(cur.fetchone()[0]) == 20000.0


def test_reconcile_releases_slots_of_an_invoice_that_died_at_apipay(client, apipay_on,
                                                                    quiet_sheets):
    """A terminal failure we were never told about must free the slot too."""
    data = _make_batch(client, date="2027-08-02")
    apipay_on["remote"][data["invoice"]["id"]] = {"id": data["invoice"]["id"],
                                                  "status": "expired"}

    _age_outbox(data["invoice"]["external_order_id"])
    assert apipay_service.reconcile_open_invoices() == 1
    assert set(_states(data["booking_ids"]).values()) == {"unpaid"}


def test_reconcile_leaves_recent_invoices_to_the_webhook(client, apipay_on,
                                                         quiet_sheets):
    """Never race a delivery that is merely a few seconds behind."""
    data = _make_batch(client, date="2027-08-03")
    apipay_on["remote"][data["invoice"]["id"]] = {"id": data["invoice"]["id"],
                                                  "status": "paid"}

    assert apipay_service.reconcile_open_invoices() == 0
    assert apipay_on["fetched"] == [], "a fresh invoice must not be polled"
    assert set(_states(data["booking_ids"]).values()) == {"awaiting_payment"}


def test_reconcile_after_the_webhook_already_landed_changes_nothing(
        client, hook_client, apipay_on, quiet_sheets):
    """Poll and push can overlap — the second one through must be a no-op."""
    data = _make_batch(client, date="2027-08-04")
    _post_webhook(hook_client, data["invoice"]["id"], "paid")
    apipay_on["remote"][data["invoice"]["id"]] = {"id": data["invoice"]["id"],
                                                  "status": "paid"}

    _age_outbox(data["invoice"]["external_order_id"])
    assert apipay_service.reconcile_open_invoices() == 0, "already applied"

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM payments WHERE method = 'apipay'")
            assert cur.fetchone()[0] == len(data["invoice"]["booking_ids"]), \
                "no second payment row"


def test_reconcile_ignores_unsent_rows_and_survives_apipay_being_down(
        client, apipay_on, monkeypatch, quiet_sheets):
    """An unsent row belongs to the outbox; an unreachable ApiPay is not fatal."""
    data = _queued_batch([_slot(1, "2027-08-05")])
    eoid = data["invoice"]["external_order_id"]
    assert _invoice_row(eoid)["invoice_id"] is None

    _age_outbox(eoid)
    assert apipay_service.reconcile_open_invoices() == 0
    assert apipay_on["fetched"] == [], "nothing was ever sent to ApiPay"

    # Now one that WAS sent, with the GET failing.
    sent = client.post("/api/manager/bookings/batch",
                       json={"phone": _PHONE, "slots": [_slot(2, "2027-08-06")]},
                       headers=_HDR).get_json()
    monkeypatch.setattr(apipay_client, "get_invoice",
                        lambda iid: (_ for _ in ()).throw(ApiPayError("timeout")))
    _age_outbox(sent["invoice"]["external_order_id"])
    assert apipay_service.reconcile_open_invoices() == 0
    assert set(_states(sent["booking_ids"]).values()) == {"awaiting_payment"}


def test_ttl_sweep_cancels_the_open_invoice(client, apipay_on):
    from integrations import apipay_service

    data = _make_batch(client, date="2027-08-05")
    apipay_service.cancel_invoices_for_bookings(data["booking_ids"], reason="ttl_expired")

    assert apipay_on["cancelled"] == [data["invoice"]["id"]]
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM apipay_invoices WHERE invoice_id = %s",
                        (data["invoice"]["id"],))
            assert cur.fetchone()[0] == "cancelled"


def test_manager_cancel_cancels_the_invoice(client, apipay_on):
    """DELETE /bookings/<id> must not leave a live payment request in Kaspi."""
    data = _make_batch(client, date="2027-09-02")
    a, b = data["invoice"]["booking_ids"]

    r = client.delete(f"/api/manager/bookings/{a}", headers=_HDR)
    assert r.status_code == 200
    assert apipay_on["cancelled"] == [data["invoice"]["id"]]


def test_partial_cancel_reissues_for_the_remainder(client, apipay_on):
    """Cancelling A of a 20 000 ₸ A+B invoice re-issues 10 000 ₸ for B alone."""
    data = _make_batch(client, date="2027-09-09")
    a, b = data["invoice"]["booking_ids"]
    assert apipay_on["created"][0]["amount"] == 20000

    client.delete(f"/api/manager/bookings/{a}", headers=_HDR)

    assert len(apipay_on["created"]) == 2, "a replacement invoice must be raised"
    reissued = apipay_on["created"][1]
    assert reissued["amount"] == 10000, "only the surviving booking is charged"
    assert reissued["phone"] == "87001234567"

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, amount, booking_ids FROM apipay_invoices "
                        "ORDER BY id")
            rows = cur.fetchall()
    assert rows[0][0] == "cancelled" and float(rows[0][1]) == 20000
    assert rows[1][0] == "processing" and float(rows[1][1]) == 10000
    assert rows[1][2] == [b], "the new invoice covers only the surviving booking"


def test_reissued_invoice_confirms_the_survivor_when_paid(client, hook_client, apipay_on):
    data = _make_batch(client, date="2027-09-16")
    a, b = data["invoice"]["booking_ids"]
    client.delete(f"/api/manager/bookings/{a}", headers=_HDR)

    new_invoice_id = apipay_on["next_id"]
    _post_webhook(hook_client, new_invoice_id, "paid")

    states = _states([a, b])
    assert states[b] == "confirmed"
    assert states[a] == "cancelled"


def test_cancelling_the_last_booking_reissues_nothing(client, apipay_on):
    data = _make_batch(client, date="2027-09-23")
    a, b = data["invoice"]["booking_ids"]

    client.delete(f"/api/manager/bookings/{a}", headers=_HDR)
    assert len(apipay_on["created"]) == 2      # re-issued for b
    client.delete(f"/api/manager/bookings/{b}", headers=_HDR)

    assert len(apipay_on["created"]) == 2, "nothing left to charge"
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM apipay_invoices WHERE status = 'cancelled'")
            assert cur.fetchone()[0] == 2


def test_no_reissue_when_apipay_refuses_the_cancel(client, apipay_on, monkeypatch):
    """If the invoice is already paid, ApiPay rejects the cancel — never re-issue."""
    data = _make_batch(client, date="2027-09-30")
    a, _ = data["invoice"]["booking_ids"]

    def refuse(invoice_id):
        raise ApiPayError("ApiPay POST /invoices/x/cancel → 409: already paid")
    monkeypatch.setattr(apipay_client, "cancel_invoice", refuse)

    r = client.delete(f"/api/manager/bookings/{a}", headers=_HDR)
    assert r.status_code == 200, "the cancellation itself must still succeed"
    assert len(apipay_on["created"]) == 1, "no second charge for the client"


def test_cancellation_survives_apipay_being_down(client, apipay_on, monkeypatch):
    def boom(invoice_id):
        raise ApiPayError("ApiPay недоступен: timeout")
    monkeypatch.setattr(apipay_client, "cancel_invoice", boom)

    data = _make_batch(client, date="2027-10-07")
    a = data["invoice"]["booking_ids"][0]
    r = client.delete(f"/api/manager/bookings/{a}", headers=_HDR)

    assert r.status_code == 200
    assert _states([a])[a] == "cancelled"


def test_paid_after_cancellation_is_flagged_for_manual_refund(client, hook_client,
                                                              apipay_on, caplog,
                                                              monkeypatch):
    """The unavoidable race: client pays while the manager is cancelling."""
    import logging
    data = _make_batch(client, date="2027-10-14")
    a, b = data["invoice"]["booking_ids"]
    invoice_id = data["invoice"]["id"]

    # ApiPay refuses the cancel because payment is already in flight — exactly
    # the window in which this race happens.
    def refuse(iid):
        raise ApiPayError("уже оплачивается")
    monkeypatch.setattr(apipay_client, "cancel_invoice", refuse)

    client.delete(f"/api/manager/bookings/{a}", headers=_HDR)
    client.delete(f"/api/manager/bookings/{b}", headers=_HDR)

    with caplog.at_level(logging.ERROR):
        r = _post_webhook(hook_client, invoice_id, "paid")

    assert r.status_code == 200
    assert r.get_json()["bookings"] == [], "nothing to confirm"
    assert "ТРЕБУЕТСЯ РУЧНОЙ ВОЗВРАТ" in caplog.text

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT error_code FROM apipay_invoices WHERE invoice_id = %s",
                        (invoice_id,))
            assert cur.fetchone()[0] == "paid_after_cancellation"

    # And no booking was resurrected.
    assert set(_states([a, b]).values()) == {"cancelled"}


def test_ttl_sweep_does_not_reissue(client, apipay_on):
    """A TTL sweep expires the whole batch — no pointless replacement invoice."""
    from integrations import apipay_service

    data = _make_batch(client, date="2027-10-21")
    apipay_service.cancel_invoices_for_bookings(data["booking_ids"], reason="ttl_expired")

    assert len(apipay_on["created"]) == 1, "the sweeper must not raise a new invoice"


def test_ttl_sweep_leaves_paid_invoices_alone(client, hook_client, apipay_on):
    from integrations import apipay_service

    data = _make_batch(client, date="2027-08-12")
    _post_webhook(hook_client, data["invoice"]["id"], "paid")
    apipay_service.cancel_invoices_for_bookings(data["booking_ids"], reason="ttl_expired")

    assert apipay_on["cancelled"] == [], "a paid invoice must never be cancelled"


# ---------------------------------------------------------------------------
# booking_history — what the manager UI shows for an ApiPay-driven change
# ---------------------------------------------------------------------------

def test_paid_writes_an_apipay_history_row(client, hook_client, apipay_on):
    data = _make_batch(client, date="2027-11-04")
    invoice_id = data["invoice"]["id"]
    _post_webhook(hook_client, invoice_id, "paid")

    rows = _history(data["invoice"]["booking_ids"][0])
    assert all(src == "bot:ApiPay" for src, _ in rows[1:]), \
        "every ApiPay-driven row is attributed to the bot, never to a manager"
    descriptions = [d for _, d in rows]
    assert f"Оплата аванса 10000тг через Kaspi (ApiPay), счёт №{invoice_id}." in descriptions
    assert "Изменение статуса с ОЖИДАЕТ ОПЛАТЫ на ПОДТВЕРЖДЕНО." in descriptions, \
        "the status change is still recorded alongside the payment"


@pytest.mark.parametrize("status, expected", [
    ("expired", "Время на оплату истекло — счёт ApiPay №{iid} закрыт, бронь освобождена."),
    ("cancelled", "Счёт ApiPay №{iid} отменён. Причина: отменён на стороне ApiPay."),
    ("error", "Счёт ApiPay №{iid} отменён. Причина: ошибка оплаты."),
])
def test_failed_webhook_writes_ttl_or_cancelled_history(client, hook_client, apipay_on,
                                                        status, expected):
    """'expired' is the payment window running out; the others are a killed invoice."""
    data = _make_batch(client, date="2027-11-11")
    invoice_id = data["invoice"]["id"]
    _post_webhook(hook_client, invoice_id, status)

    rows = _history(data["invoice"]["booking_ids"][0])
    assert (("bot:ApiPay", expected.format(iid=invoice_id))) in rows


def test_ttl_sweep_writes_a_ttl_history_row(client, apipay_on):
    """Cancelling before the slot is released is a TTL expiry, not a cancellation."""
    data = _make_batch(client, date="2027-11-18")
    apipay_service.cancel_invoices_for_bookings(data["booking_ids"], reason="ttl_expired")

    for bid in data["invoice"]["booking_ids"]:
        assert ("bot:ApiPay",
                f"Время на оплату истекло — счёт ApiPay №{data['invoice']['id']} "
                f"закрыт, бронь освобождена.") in _history(bid)


def test_manager_cancel_writes_a_cancelled_history_row(client, apipay_on):
    data = _make_batch(client, date="2027-11-25")
    a, b = data["invoice"]["booking_ids"]

    client.delete(f"/api/manager/bookings/{a}", headers=_HDR)

    expected = f"Счёт ApiPay №{data['invoice']['id']} отменён. Причина: бронь отменена менеджером."
    assert ("bot:ApiPay", expected) in _history(a)
    assert ("bot:ApiPay", expected) in _history(b), \
        "the whole batch's invoice died, so every covered booking says so"


def test_receipt_payment_closes_the_invoice_in_the_history(client, apipay_on):
    """Paid by receipt — the invoice is retired, and the history says why."""
    data = _make_batch(client, date="2027-12-02")
    bid = data["invoice"]["booking_ids"][0]

    apipay_service.cancel_invoices_for_bookings([bid], reason="paid_by_receipt")

    assert ("bot:ApiPay",
            f"Счёт ApiPay №{data['invoice']['id']} отменён. Причина: оплата подтверждена чеком.") \
        in _history(bid)


def test_apipay_history_is_the_bot_channel_not_the_manager_one(client, hook_client,
                                                               apipay_on):
    """'bot:*' rows must never be reported as manager-driven changes."""
    from integrations.repo import history_repo

    data = _make_batch(client, date="2027-12-09")
    _post_webhook(hook_client, data["invoice"]["id"], "paid")

    bot_rows, _ = history_repo.get_whatsapp_history()
    manager_rows, _ = history_repo.get_manager_history()
    assert any(r["source"] == "bot:ApiPay" for r in bot_rows)
    assert all(r["source"] != "bot:ApiPay" for r in manager_rows)


# ---------------------------------------------------------------------------
# Bot flow: the WhatsApp client pays the same way the manager's clients do
# ---------------------------------------------------------------------------

def _bot_hook(chat_id="7770001:87001234567", lang="ru", provider="ycloud",
              phone="87001234567"):
    from integrations import apipay_service

    def hook(cur, booking_ids):
        return {"invoice": apipay_service.queue_invoice(
            cur, phone, booking_ids, slot_count=1, description="Аванс за бронь",
            source="bot", notify_chat_id=chat_id, notify_provider=provider,
            notify_lang=lang,
        )}
    return hook


def test_bot_booking_queues_the_invoice_in_the_reservation_transaction(apipay_on):
    """The row must be committed WITH the reservation, before ApiPay is called."""
    from integrations import apipay_service, booking_service

    bid, token = _draft()
    res = booking_service.request_payment(bid, token, on_reserved=_bot_hook())

    assert res["ok"]
    assert _states([bid]) == {bid: "awaiting_payment"}
    assert apipay_on["created"] == [], "queueing must not touch the network"

    queued = res["data"]["invoice"]
    row = _invoice_row(queued["external_order_id"])
    assert row["status"] == "created" and row["invoice_id"] is None
    assert row["source"] == "bot"
    assert row["notify_chat_id"] == "7770001:87001234567"
    assert float(row["amount"]) == 10000

    sent = apipay_service.send_invoice(queued)
    assert sent["id"] == apipay_on["next_id"]
    assert len(apipay_on["created"]) == 1
    assert apipay_on["created"][0]["amount"] == 10000


def test_bot_slot_taken_creates_no_invoice(apipay_on):
    """A hook inside the transaction must not outlive a rejected reservation."""
    from integrations import booking_service

    first, tok1 = _draft(date="2027-07-02")
    assert booking_service.request_payment(first, tok1, on_reserved=_bot_hook())["ok"]

    second, tok2 = _draft(date="2027-07-02")           # identical slot
    res = booking_service.request_payment(second, tok2, on_reserved=_bot_hook())

    assert not res["ok"] and res["code"] == "SLOT_TAKEN"
    assert _invoice_count() == 1, "the loser's invoice row must be rolled back"


def test_bot_paid_webhook_confirms_and_notifies_the_client(hook_client, apipay_on,
                                                           monkeypatch):
    from integrations import apipay_service, booking_service

    sent_messages = []
    monkeypatch.setattr("handlers.whatsapp_client.send_text_message",
                        lambda channel, to, text: sent_messages.append((channel, to, text)))

    bid, token = _draft(date="2027-07-03")
    res = booking_service.request_payment(bid, token, on_reserved=_bot_hook(lang="kk"))
    invoice = apipay_service.send_invoice(res["data"]["invoice"])

    r = _post_webhook(hook_client, invoice["id"], "paid")
    assert r.status_code == 200
    assert _states([bid]) == {bid: "confirmed"}

    # The webhook answers within its 5-second budget and notifies off-thread,
    # so the message lands just after the response.
    for _ in range(50):
        if sent_messages:
            break
        time.sleep(0.05)

    assert len(sent_messages) == 1
    channel, to, text = sent_messages[0]
    assert to == "+77001234567", "E.164 — YCloud rejects the local '8…' form"
    assert channel.phone_number_id == "7770001"
    assert "Төлем қабылданды" in text, "must answer in the language the client used"


def _wait_for(messages, count=1, timeout=2.5):
    """The webhook answers inside its 5-second budget and notifies off-thread,
    so the message lands just after the response."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(messages) < count:
        time.sleep(0.02)
    return messages


def test_manager_invoice_paid_notifies_the_billed_number(client, hook_client,
                                                         apipay_on, monkeypatch):
    """A manager-created invoice carries no chat — but the client was still
    charged in their Kaspi app, so they still have to hear it landed."""
    sent = []
    monkeypatch.setattr("handlers.whatsapp_client.send_text_message",
                        lambda channel, to, text: sent.append((channel, to, text)))

    data = _make_batch(client, date="2027-09-01")
    _post_webhook(hook_client, data["invoice"]["id"], "paid")

    _wait_for(sent)
    assert len(sent) == 1
    _, to, text = sent[0]
    # The invoice row holds ApiPay's '8XXXXXXXXXX'; the provider takes E.164.
    assert normalize_phone(_PHONE) == "87001234567"
    assert to == "+77001234567"
    assert "Оплата получена" in text and "Төлем қабылданды" in text, \
        "nobody chose a language for this invoice — send both"


def test_expired_invoice_tells_the_client_the_slot_is_gone(client, hook_client,
                                                           apipay_on, monkeypatch):
    """The slot goes back on the market either way; the client learning that
    from us is the difference between rebooking and turning up."""
    sent = []
    monkeypatch.setattr("handlers.whatsapp_client.send_text_message",
                        lambda channel, to, text: sent.append((channel, to, text)))

    data = _make_batch(client, date="2027-09-02")
    _post_webhook(hook_client, data["invoice"]["id"], "expired")

    assert set(_states(data["booking_ids"]).values()) == {"unpaid"}
    _wait_for(sent)
    assert len(sent) == 1
    text = sent[0][2]
    assert "Срок оплаты" in text
    assert "10:00–11:00" in text and "12:00–13:00" in text, \
        "both slots of the batch, in one message"


def test_failed_invoice_is_worded_as_a_failure_not_a_timeout(client, hook_client,
                                                             apipay_on, monkeypatch):
    sent = []
    monkeypatch.setattr("handlers.whatsapp_client.send_text_message",
                        lambda channel, to, text: sent.append((channel, to, text)))

    data = _make_batch(client, date="2027-09-04")
    _post_webhook(hook_client, data["invoice"]["id"], "error")

    _wait_for(sent)
    assert len(sent) == 1
    assert "Оплата не прошла" in sent[0][2]


def test_a_payment_that_released_nothing_says_nothing(client, hook_client,
                                                      apipay_on, monkeypatch):
    """The invoice died, but a manager had already confirmed the bookings by
    hand. Telling that client their slot is gone would be a lie."""
    sent = []
    monkeypatch.setattr("handlers.whatsapp_client.send_text_message",
                        lambda channel, to, text: sent.append((channel, to, text)))

    data = _make_batch(client, date="2027-09-05")
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE bookings SET state = 'confirmed' WHERE id = ANY(%s)",
                        (data["booking_ids"],))

    _post_webhook(hook_client, data["invoice"]["id"], "expired")

    _wait_for(sent)
    assert sent == []
    assert set(_states(data["booking_ids"]).values()) == {"confirmed"}


def test_refund_reaches_the_client_though_no_booking_moves(client, hook_client,
                                                           apipay_on, monkeypatch):
    """A refund transitions nothing — under a 'did any booking change?' gate it
    would reach nobody, which is exactly the message a client is waiting for."""
    sent = []
    monkeypatch.setattr("handlers.whatsapp_client.send_text_message",
                        lambda channel, to, text: sent.append((channel, to, text)))

    data = _make_batch(client, date="2027-09-03")
    _post_webhook(hook_client, data["invoice"]["id"], "paid")
    _wait_for(sent)
    sent.clear()

    r = _post_webhook(hook_client, data["invoice"]["id"], "refunded",
                      event="invoice.refunded")

    assert r.status_code == 200
    assert r.get_json()["bookings"] == []
    _wait_for(sent)
    assert len(sent) == 1
    assert "Возврат оформлен" in sent[0][2]
    assert "20 000₸" in sent[0][2]


def test_receipt_payment_retires_the_apipay_invoice(apipay_on, monkeypatch):
    """Both payment paths stay open, so exactly one of them must win.

    A client told to pay by link (ApiPay was down at booking time) can still
    receive the Kaspi push once the outbox retries. Confirming by receipt must
    take the invoice off the table, or they can pay for the same slot twice.
    """
    from integrations import apipay_service, booking_service

    bid, token = _draft(date="2027-07-04")
    res = booking_service.request_payment(bid, token, on_reserved=_bot_hook())
    invoice = apipay_service.send_invoice(res["data"]["invoice"])

    proof = booking_service.submit_payment_proof(
        bid, parsed={"bank": "kaspi", "amount": 10000, "ref": "QR-777",
                     "date": __import__("datetime").datetime(2027, 6, 30)},
        proof_media_id="media-1")

    assert proof["ok"]
    assert _states([bid]) == {bid: "confirmed"}
    assert apipay_on["cancelled"] == [invoice["id"]], "the Kaspi push must be withdrawn"
    assert _invoice_row(invoice["external_order_id"])["status"] == "cancelled"


def test_queued_invoice_is_retired_when_the_receipt_arrives_first(apipay_on,
                                                                  monkeypatch):
    """Same, for an invoice still sitting in the outbox — never send it later."""
    from integrations import booking_service

    _apipay_outage(apipay_on, monkeypatch)
    bid, token = _draft(date="2027-07-05")
    res = booking_service.request_payment(bid, token, on_reserved=_bot_hook())
    eoid = res["data"]["invoice"]["external_order_id"]

    booking_service.submit_payment_proof(
        bid, parsed={"bank": "kaspi", "amount": 10000, "ref": "QR-778",
                     "date": __import__("datetime").datetime(2027, 6, 30)},
        proof_media_id="media-2")

    assert _invoice_row(eoid)["status"] == "cancelled"
    _age_outbox(eoid)
    from integrations import apipay_service
    assert apipay_service.sweep_pending_sends() == 0
    assert apipay_on["created"] == [], "a cancelled row must never be sent"


# ---------------------------------------------------------------------------
# The other direction: the push must not outrun the fallback the client was given
# ---------------------------------------------------------------------------

def _finalize(monkeypatch, booking_id, token, date, lang="ru"):
    """Run the bot's real finalization step, side effects stubbed."""
    from handlers import llm_booking_flow as flow_mod

    for name in ("clear_history", "refresh_all_bookings", "refresh_week_sheet"):
        monkeypatch.setattr(flow_mod, name, lambda *a, **kw: None)

    draft = {"id": booking_id, "client_token": token, "date": date,
             "time_start": "18:00", "time_end": "19:00", "field": 1,
             "format": "5x5", "players": 8, "customer_name": "Клиент"}
    return flow_mod.LlmBookingFlowHandler()._finalize_booking(
        draft, chat_id="7770001:87001234567", phone="87001234567", lang=lang)


def test_bot_failed_send_cancels_the_booking_and_tells_the_client(apipay_on,
                                                                  monkeypatch):
    """The client is told what failed and to call a manager — not left waiting.

    A reservation with no invoice behind it is a slot the client cannot pay
    for: it would sit held until the TTL quietly dropped it, and the only
    payment request they were promised would never arrive.
    """
    outage = _apipay_outage(apipay_on, monkeypatch)
    bid, token = _draft(date="2027-07-06")

    reply = _finalize(monkeypatch, bid, token, "2027-07-06")

    assert "ApiPay недоступен" in reply, "the provider's wording reaches the client"
    assert "менеджер" in reply.lower(), "and a way out of it"
    assert config.KASPI_PAYMENT_URL not in reply, "no payment path was left open"
    assert apipay_on["created"] == []

    assert _states([bid]) == {bid: "cancelled"}, "the slot goes back on the market"

    row = _invoice_for_booking(bid)
    assert row["status"] == "cancelled" and row["invoice_id"] is None

    # ApiPay comes back — and must find nothing left to send.
    outage["down"] = False
    _age_outbox(row["external_order_id"])
    assert apipay_service.sweep_pending_sends() == 0
    assert apipay_on["created"] == [], "no request for money after a cancelled booking"


def test_bot_refuses_a_number_that_has_no_kaspi(apipay_on, monkeypatch):
    """The client is told immediately — not left holding an unpayable slot.

    The number is the one they are writing from, so there is nothing for them
    to correct: the reply hands them to a manager. Nothing is reserved, and no
    invoice is spent on a request Kaspi could never deliver.
    """
    apipay_on["no_kaspi"].add("87001234567")
    bid, token = _draft(date="2027-07-11")

    reply = _finalize(monkeypatch, bid, token, "2027-07-11")

    assert "Kaspi" in reply and "менеджер" in reply.lower()
    assert config.KASPI_PAYMENT_URL not in reply
    assert apipay_on["created"] == [], "the doomed invoice is never created"
    assert _invoice_count() == 0, "and no row is queued for the sweeper either"
    assert _states([bid]) == {bid: "draft"}, "the slot was never reserved"


def test_bot_answers_the_kaspi_refusal_in_kazakh(apipay_on, monkeypatch):
    apipay_on["no_kaspi"].add("87001234567")
    bid, token = _draft(date="2027-07-12")

    reply = _finalize(monkeypatch, bid, token, "2027-07-12", lang="kk")

    assert "Kaspi" in reply
    assert "Менеджерге" in reply


def test_bot_does_not_reserve_when_the_check_cannot_be_made(apipay_on, monkeypatch):
    """A check that fails is treated like a send that fails — but for free."""
    monkeypatch.setattr(apipay_client, "check_client",
                        lambda phone: (_ for _ in ()).throw(
                            ApiPayError("ApiPay недоступен: timeout")))
    bid, token = _draft(date="2027-07-13")

    reply = _finalize(monkeypatch, bid, token, "2027-07-13")

    assert "ApiPay недоступен" in reply
    assert _states([bid]) == {bid: "draft"}, "nothing to take back"
    assert _invoice_count() == 0


def test_bot_failed_send_answers_in_kazakh(apipay_on, monkeypatch):
    _apipay_outage(apipay_on, monkeypatch)
    bid, token = _draft(date="2027-07-08")

    reply = _finalize(monkeypatch, bid, token, "2027-07-08", lang="kk")

    assert "Менеджерге" in reply
    assert "ApiPay недоступен" in reply


def test_payment_that_lands_after_a_rollback_is_flagged_for_refund(
        hook_client, apipay_on, monkeypatch):
    """The reason the invoice row is retired instead of deleted.

    A timeout cannot be told apart from a rejection, so ApiPay may have created
    the invoice we gave up on. If the client confirms it in Kaspi anyway, the
    money must surface as a manual refund — not as a webhook for an invoice
    nobody has heard of.
    """
    _apipay_outage(apipay_on, monkeypatch)
    bid, token = _draft(date="2027-07-09")

    _finalize(monkeypatch, bid, token, "2027-07-09")
    assert _states([bid]) == {bid: "cancelled"}

    row = _invoice_for_booking(bid)
    payload = {
        "event": "invoice.status_changed",
        "invoice": {"id": 999001, "status": "paid", "amount": "10000.00",
                    "external_order_id": row["external_order_id"],
                    "paid_at": "2026-08-13T08:35:00Z"},
    }
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    r = hook_client.post("/webhooks/apipay", data=raw,
                         headers={"Content-Type": "application/json",
                                  "X-Webhook-Signature": sig})
    assert r.status_code == 200

    assert _states([bid]) == {bid: "cancelled"}, "a cancelled slot is not resurrected"
    flagged = _invoice_row(row["external_order_id"])
    assert flagged["status"] == "paid"
    assert "возврат" in (flagged["error_message"] or "").lower(), \
        "the row carries the manual-refund note"


def test_bot_invoice_queued_but_never_answered_is_still_retried(apipay_on, monkeypatch):
    """The outbox must keep covering bot rows the client was told nothing about.

    Retiring the row belongs to the fallback branch alone. A worker that dies
    between the commit and the send leaves a reservation with no payment request
    behind it, and the retry is the only one the client will ever get — so
    `source='bot'` on its own must never be a reason to skip.
    """
    outage = _apipay_outage(apipay_on, monkeypatch)
    bid, token = _draft(date="2027-07-07")

    # request_payment only — the process never reached `send_invoice`.
    from integrations import booking_service
    res = booking_service.request_payment(bid, token, on_reserved=_bot_hook())
    eoid = res["data"]["invoice"]["external_order_id"]
    assert _invoice_row(eoid)["status"] == "created", "still queued, nothing sent"

    outage["down"] = False
    _age_outbox(eoid)
    assert apipay_service.sweep_pending_sends() == 1

    row = _invoice_row(eoid)
    assert row["invoice_id"] == apipay_on["next_id"]
    assert row["status"] == "processing"
    assert apipay_on["created"][0]["external_order_id"] == eoid
