"""Refunds through ApiPay: first_paid_at, the 48h window, the cap, idempotency,
the asynchronous outcome (send response / webhook / sweeper)."""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

import config
from blueprints.apipay_webhook import apipay_webhook
from blueprints.manager_api import manager_api
from integrations import apipay_client, booking_refunds, client_notify
from integrations.apipay_client import ApiPayError
from integrations.repo.postgres import _conn

_KEY = "test-key"
_HDR = {"X-API-Key": _KEY}
_INVOICE = 501


class FakeApiPay:
    """Stands in for ApiPay's refund endpoints and records every call."""

    def __init__(self):
        self.calls: list[dict] = []
        self.next_id = 9000
        self.by_key: dict[str, dict] = {}
        self.fail: ApiPayError | None = None
        self.remote: dict[int, list[dict]] = {}

    def create_refund(self, invoice_id, amount, idempotency_key, reason=None):
        self.calls.append({"invoice_id": invoice_id, "amount": amount, "key": idempotency_key})
        if self.fail is not None:
            raise self.fail
        if idempotency_key in self.by_key:  # ApiPay's own replay behaviour
            return {"refund": self.by_key[idempotency_key], "idempotent_replay": True}
        self.next_id += 1
        refund = {"id": self.next_id, "status": "pending", "amount": amount,
                  "idempotency_key": idempotency_key}
        self.by_key[idempotency_key] = refund
        self.remote.setdefault(invoice_id, []).append(refund)
        return {"refund": refund}

    def get_refunds(self, invoice_id):
        return {"refunds": self.remote.get(invoice_id, []), "invoice": {"id": invoice_id}}


@pytest.fixture
def client(monkeypatch):
    config.X_SERVICE_TOKEN = _KEY
    monkeypatch.setattr(config, "APIPAY_ENABLED", False)
    monkeypatch.setattr(config, "APIPAY_WEBHOOK_SECRET", "whsec")
    # Never touch the real Google Sheet or WhatsApp from tests.
    for name in ("_single_table_write", "_single_table_erase", "upsert_booking_row",
                 "refresh_week_sheet"):
        monkeypatch.setattr(f"blueprints.manager_api.{name}", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(client_notify, "send_async",
                        lambda to, key, lang=None, **kw: sent.append((to, key, kw)))
    fake = FakeApiPay()
    monkeypatch.setattr(apipay_client, "create_refund", fake.create_refund)
    monkeypatch.setattr(apipay_client, "get_refunds", fake.get_refunds)
    app = Flask(__name__)
    app.register_blueprint(manager_api)
    app.register_blueprint(apipay_webhook)
    test_client = app.test_client()
    test_client.sent = sent
    test_client.apipay = fake
    test_client.enable = lambda: monkeypatch.setattr(config, "APIPAY_ENABLED", True)
    return test_client


def _sql(query, params=()):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone() if cur.description else None


def _create(client, price=20000) -> int:
    body = {"field": 2, "date": "2026-09-03", "time_start": "12:00",
            "time_end": "13:00", "repeat": "none", "phone": "77011234567"}
    r = client.post("/api/manager/bookings", json=body, headers=_HDR)
    bid = r.get_json()["data"]["booking_id"]
    _sql("UPDATE bookings SET price_total = %s WHERE id = %s", (price, bid))
    return bid


def _pay_via_apipay(bid, amount=10000, invoice=_INVOICE, phone="87009998877"):
    _sql("INSERT INTO apipay_invoices (invoice_id, external_order_id, phone, amount, status, "
         "  booking_ids, paid_at) VALUES (%s, %s, %s, %s, 'paid', %s, NOW())",
         (invoice, f"o-{invoice}", phone, amount, [bid]))
    _sql("INSERT INTO payments (booking_id, method, bank, amount, transaction_ref, status, verified_at) "
         "VALUES (%s, 'apipay', 'kaspi', %s, %s, 'accepted', NOW())",
         (bid, amount, f"apipay:{invoice}:{bid}"))


def _paid_booking(client, price=20000, amount=10000) -> int:
    bid = _create(client, price)
    _pay_via_apipay(bid, amount)
    client.enable()
    return bid


def _refund(client, bid, amount, key=None, **extra):
    body = {"amount": amount, **extra}
    if key:
        body["request_key"] = key
    return client.post(f"/api/manager/bookings/{bid}/refunds", json=body, headers=_HDR)


def _summary(client, bid):
    return client.get(f"/api/manager/bookings/{bid}/refunds", headers=_HDR).get_json()["data"]


def _first_paid_at(bid):
    return _sql("SELECT first_paid_at FROM bookings WHERE id = %s", (bid,))[0]


def _webhook(client, refund, invoice=_INVOICE):
    body = json.dumps({"event": "invoice.refunded",
                       "invoice": {"id": invoice, "status": "partially_refunded"},
                       "refund": refund}).encode()
    sig = "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/apipay", data=body, content_type="application/json",
                       headers={"X-Webhook-Signature": sig})


# ── window & first_paid_at ───────────────────────────────────────────────

@pytest.mark.no_db
def test_refund_window_boundaries():
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    assert booking_refunds.refund_window_open(None, now) is None
    assert booking_refunds.refund_window_open(now - timedelta(hours=48), now) is True
    assert booking_refunds.refund_window_open(now - timedelta(hours=48, seconds=1), now) is False


def test_manual_payment_stamps_first_paid_at_once(client):
    bid = _create(client)
    assert _first_paid_at(bid) is None
    client.patch(f"/api/manager/bookings/{bid}", json={"paid_cash": 5000}, headers=_HDR)
    first = _first_paid_at(bid)
    assert first is not None
    client.patch(f"/api/manager/bookings/{bid}", json={"paid_kaspi_qr": 3000}, headers=_HDR)
    assert _first_paid_at(bid) == first


def test_bot_payment_stamps_first_paid_at(client):
    bid = _create(client)
    paid = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    _sql("INSERT INTO payments (booking_id, amount, status, verified_at) "
         "VALUES (%s, 10000, 'accepted', %s)", (bid, paid))
    assert _first_paid_at(bid) == paid


def test_insert_with_expected_avans_does_not_stamp(client):
    body = {"field": 3, "date": "2026-09-04", "time_start": "12:00", "time_end": "13:00",
            "repeat": "none", "phone": "77011234567", "prepayment": 10000}
    bid = client.post("/api/manager/bookings", json=body, headers=_HDR).get_json()["data"]["booking_id"]
    assert _first_paid_at(bid) is None


# ── what is refundable ───────────────────────────────────────────────────

def test_cash_and_qr_are_not_refundable_through_the_system(client):
    bid = _create(client)
    client.patch(f"/api/manager/bookings/{bid}", json={"paid_cash": 20000}, headers=_HDR)
    client.enable()
    s = _summary(client, bid)
    assert s["paid_total"] == 20000 and s["apipay_paid"] == 0
    assert s["refundable"] == 0 and s["blocked_reason"] == "no_apipay"
    r = _refund(client, bid, 1000)
    assert r.status_code == 409 and r.get_json()["code"] == "NO_APIPAY_PAYMENT"
    assert client.apipay.calls == []


def test_refundable_is_capped_by_price(client):
    bid = _paid_booking(client, price=6000, amount=10000)
    s = _summary(client, bid)
    assert s["refundable"] == 6000 and s["refund_phone"] == "87009998877"
    assert _refund(client, bid, 6001).get_json()["code"] == "AMOUNT_TOO_LARGE"


@pytest.mark.parametrize("amount", [0, -5, "abc", None, 10.5])
def test_bad_amounts_are_rejected(client, amount):
    bid = _paid_booking(client)
    assert _refund(client, bid, amount).status_code == 400
    assert client.apipay.calls == []


def test_refund_unknown_booking(client):
    client.enable()
    assert _refund(client, 999999, 100).status_code == 404


# ── the happy path and the asynchronous outcome ──────────────────────────

def test_refund_is_sent_and_completed_by_webhook(client):
    bid = _paid_booking(client)
    r = _refund(client, bid, 4000, key="press-1", comment="отмена", source="a@b.kz")
    data = r.get_json()["data"]
    assert r.status_code == 200
    assert data["refund"]["state"] == "processing" and data["refund"]["created_by"] == "a@b.kz"
    call = client.apipay.calls[0]
    assert call["invoice_id"] == _INVOICE and call["amount"] == 4000
    assert call["key"].startswith("dopshy-refund-")
    # One refund in flight per invoice — nothing more can go out until it settles.
    assert data["refundable"] == 0 and data["blocked_reason"] == "in_progress"
    assert data["pending_total"] == 4000 and data["refunded_total"] == 0

    refund_id = data["refund"]["apipay_refund_id"]
    assert _webhook(client, {"id": refund_id, "status": "completed", "amount": 4000}).status_code == 200
    s = _summary(client, bid)
    assert s["refunds"][0]["state"] == "refunded"
    assert s["refunded_total"] == 4000 and s["refundable"] == 6000
    assert [k for _, k, _ in client.sent] == ["apipay_refunded"]

    # Redelivery is a no-op: no second message, no state change.
    _webhook(client, {"id": refund_id, "status": "completed", "amount": 4000})
    assert len(client.sent) == 1


def test_same_request_key_is_one_refund(client):
    bid = _paid_booking(client)
    first = _refund(client, bid, 3000, key="press-1").get_json()["data"]["refund"]
    second = _refund(client, bid, 3000, key="press-1").get_json()["data"]["refund"]
    assert first["id"] == second["id"]
    assert len(client.apipay.calls) == 1
    assert _sql("SELECT COUNT(*) FROM booking_refunds WHERE booking_id = %s", (bid,))[0] == 1


def test_second_refund_while_one_is_in_flight_is_refused(client):
    bid = _paid_booking(client)
    _refund(client, bid, 3000, key="press-1")
    r = _refund(client, bid, 1000, key="press-2")
    assert r.status_code == 409 and r.get_json()["code"] == "REFUND_IN_PROGRESS"
    assert len(client.apipay.calls) == 1


def test_lost_response_is_resent_with_the_same_key(client):
    bid = _paid_booking(client)
    client.apipay.fail = ApiPayError("timeout")  # no status code: outcome unknown
    data = _refund(client, bid, 2000, key="press-1").get_json()["data"]
    assert data["refund"]["state"] == "requested"

    client.apipay.fail = None
    booking_refunds.sweep(resend_after=timedelta(0))
    keys = [c["key"] for c in client.apipay.calls]
    assert len(keys) == 2 and keys[0] == keys[1]
    assert _summary(client, bid)["refunds"][0]["state"] == "processing"


def test_provider_refusal_fails_the_row_and_frees_the_amount(client):
    bid = _paid_booking(client)
    client.apipay.fail = ApiPayError("400", status_code=400,
                                     payload={"error_code": "refund_insufficient_funds"})
    r = _refund(client, bid, 2000, key="press-1")
    body = r.get_json()
    assert r.status_code == 409 and body["code"] == "REFUND_FAILED"
    assert body["data"]["refund"]["state"] == "failed"
    assert "не хватает денег" in body["message"]
    assert body["data"]["refundable"] == 10000
    # A new press is a new attempt with a new key.
    client.apipay.fail = None
    assert _refund(client, bid, 2000, key="press-2").get_json()["data"]["refund"]["state"] == "processing"
    assert client.apipay.calls[0]["key"] != client.apipay.calls[1]["key"]


def test_failed_webhook_frees_the_amount(client):
    bid = _paid_booking(client)
    refund_id = _refund(client, bid, 5000, key="k").get_json()["data"]["refund"]["apipay_refund_id"]
    _webhook(client, {"id": refund_id, "status": "failed", "error_code": "refund_window_expired"})
    s = _summary(client, bid)
    assert s["refunds"][0]["state"] == "failed" and s["refunds"][0]["error_code"] == "refund_window_expired"
    assert s["refundable"] == 10000 and client.sent == []


def test_sweeper_resolves_uncertain_then_completed(client):
    bid = _paid_booking(client)
    _refund(client, bid, 5000, key="k")
    remote = client.apipay.remote[_INVOICE][0]

    remote.update(status="processing", operation_state="uncertain",
                  error_code="refund_execution_uncertain")
    booking_refunds.sweep(check_after=timedelta(0))
    assert _summary(client, bid)["refunds"][0]["state"] == "checking"
    assert len(client.apipay.calls) == 1  # never retried while uncertain

    remote.update(status="completed", operation_state="completed", error_code=None)
    booking_refunds.sweep(check_after=timedelta(0))
    assert _summary(client, bid)["refunds"][0]["state"] == "refunded"


def test_webhook_before_send_response_attaches_to_our_row(client):
    bid = _paid_booking(client)
    client.apipay.fail = ApiPayError("timeout")
    _refund(client, bid, 2000, key="k")
    _webhook(client, {"id": 777, "status": "completed", "amount": 2000})
    s = _summary(client, bid)
    assert len(s["refunds"]) == 1
    assert s["refunds"][0]["state"] == "refunded" and s["refunds"][0]["apipay_refund_id"] == 777


def test_cabinet_refund_is_imported_once(client):
    bid = _paid_booking(client)
    refund = {"id": 555, "status": "completed", "amount": 3000, "idempotency_key": "cabinet-x"}
    _webhook(client, refund)
    _webhook(client, refund)
    s = _summary(client, bid)
    assert len(s["refunds"]) == 1 and s["refunds"][0]["created_by"] == "ApiPay (кабинет)"
    assert s["refunded_total"] == 3000 and s["refundable"] == 7000


def test_refund_past_window_is_allowed_but_flagged(client):
    bid = _paid_booking(client)
    _sql("UPDATE bookings SET first_paid_at = NOW() - INTERVAL '3 days' WHERE id = %s", (bid,))
    data = _refund(client, bid, 1000, key="k").get_json()["data"]
    assert data["within_window"] is False and data["refund"]["within_window"] is False


def test_refund_needs_apipay_configured(client):
    bid = _create(client)
    r = _refund(client, bid, 100)
    assert r.status_code == 503
