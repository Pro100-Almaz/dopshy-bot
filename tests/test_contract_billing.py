"""Tests for contract payment plans (integrations/contract_billing.py).

Covers what can silently cost money:
  * how the price is split (static: even split / manager amounts / re-spread;
    dynamic: (price - paid - invoiced) / installments left, per booking),
  * the channel (Kaspi invoice vs. WhatsApp link) and the Kaspi check,
  * issuing through the ApiPay outbox, and the webhook / poller settling it,
  * retries, overdue, manual payment, and cancellation taking invoices back.

No test talks to ApiPay or WhatsApp: both are monkeypatched.
"""

import hashlib
import hmac
import json
from datetime import date
from decimal import Decimal

import pytest
from flask import Flask

import config
from blueprints import apipay_webhook as webhook_bp
from blueprints.manager_api import manager_api
from integrations import apipay_client, apipay_service, client_notify, contract_billing
from integrations.apipay_client import ApiPayError
from integrations.contract_billing import PlanError, normalize_plan, split_evenly
from integrations.repo.postgres import _conn

_KEY = "test-key"
_HDR = {"X-API-Key": _KEY}
_SECRET = "whsec-test"
_KASPI = "87001234567"
_NO_KASPI = "87770000000"


# ---------------------------------------------------------------------------
# Pure helpers — no DB, no network
# ---------------------------------------------------------------------------

@pytest.mark.no_db
def test_split_puts_the_remainder_on_the_last_installment():
    assert split_evenly(Decimal(100000), 3) == [33333, 33333, 33334]
    assert sum(split_evenly(Decimal(450000), 7)) == 450000


@pytest.mark.no_db
def test_monthly_steps_clamp_to_the_end_of_the_month():
    assert contract_billing._add_months(date(2027, 1, 31), 1) == date(2027, 2, 28)
    assert contract_billing._add_months(date(2027, 11, 15), 3) == date(2028, 2, 15)


@pytest.mark.no_db
def test_static_plan_derives_the_count_from_the_contract_period():
    plan = normalize_plan({"mode": "static", "frequency": "monthly"}, price=450000,
                          start_date="2027-01-01", end_date="2027-03-31", phone=_KASPI)
    assert [i["due_date"] for i in plan["schedule"]] == [
        date(2027, 1, 1), date(2027, 2, 1), date(2027, 3, 1)]
    assert [i["amount"] for i in plan["schedule"]] == [150000] * 3


@pytest.mark.no_db
def test_static_plan_without_a_period_needs_a_count():
    plan = normalize_plan({"mode": "static", "frequency": "weekly", "installments": 4,
                           "first_due_date": "2027-01-04"},
                          price=100000, start_date="2027-01-01", end_date=None, phone=_KASPI)
    assert [i["due_date"].isoformat() for i in plan["schedule"]] == [
        "2027-01-04", "2027-01-11", "2027-01-18", "2027-01-25"]
    with pytest.raises(PlanError):
        normalize_plan({"mode": "static", "frequency": "weekly"}, price=100000,
                       start_date="2027-01-01", end_date=None, phone=_KASPI)


@pytest.mark.no_db
def test_explicit_amounts_must_add_up_to_the_price():
    with pytest.raises(PlanError):
        normalize_plan({"mode": "static", "due_dates": ["2027-01-01", "2027-02-01"],
                        "amounts": [100000, 50000]},
                       price=200000, start_date="2027-01-01", end_date="2027-03-01", phone=_KASPI)
    plan = normalize_plan({"mode": "static", "due_dates": ["2027-02-01", "2027-01-01"],
                           "amounts": [150000, 50000]},
                          price=200000, start_date="2027-01-01", end_date="2027-03-01",
                          phone=_KASPI)
    assert [i["amount"] for i in plan["schedule"]] == [150000, 50000]
    assert all(i["locked"] for i in plan["schedule"])


@pytest.mark.no_db
@pytest.mark.parametrize("plan, price, phone", [
    ({"mode": "weekly"}, 100000, _KASPI),                       # bad mode
    ({"mode": "dynamic"}, 100000.5, _KASPI),                    # kopecks
    ({"mode": "dynamic"}, 100000, None),                        # nobody to bill
    ({"mode": "dynamic", "billing_phone": "12345"}, 100000, None),
])
def test_invalid_plans_are_refused(plan, price, phone):
    with pytest.raises(PlanError):
        normalize_plan(plan, price=price, start_date="2027-01-01", end_date="2027-02-01",
                       phone=phone)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def apipay_on(monkeypatch):
    """Enable ApiPay and stub HTTP + WhatsApp. Yields the call recorder."""
    monkeypatch.setattr(config, "APIPAY_ENABLED", True)
    monkeypatch.setattr(config, "APIPAY_API_KEY", "test-api-key")
    monkeypatch.setattr(config, "APIPAY_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setattr(config, "APIPAY_CHECK_CLIENT", True)
    monkeypatch.setattr(config, "CONTRACT_INVOICE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(config, "CONTRACT_INVOICE_RETRY_HOURS", 24)

    calls = {"created": [], "cancelled": [], "checked": [], "messages": [],
             "remote": {}, "no_kaspi": {_NO_KASPI}, "fail_send": False,
             "fail_check": False, "next_id": 9000}

    def fake_check(phone):
        if calls["fail_check"]:
            raise ApiPayError("ApiPay недоступен")
        normalized = apipay_client.normalize_phone(phone)
        calls["checked"].append(normalized)
        return {"phone": normalized, "has_kaspi": normalized not in calls["no_kaspi"],
                "client_name": None}

    def fake_create(phone, amount, description, external_order_id):
        if calls["fail_send"]:
            raise ApiPayError("timeout")
        calls["next_id"] += 1
        calls["created"].append({"id": calls["next_id"], "phone": phone, "amount": amount,
                                 "description": description,
                                 "external_order_id": external_order_id})
        return {"id": calls["next_id"], "status": "processing"}

    def fake_get(invoice_id):
        return calls["remote"].get(invoice_id, {"id": invoice_id, "status": "processing"})

    def fake_send(to, key, lang=None, **fields):
        calls["messages"].append({"to": to, "key": key, **fields})
        return True

    monkeypatch.setattr(apipay_client, "check_client", fake_check)
    monkeypatch.setattr(apipay_client, "create_invoice", fake_create)
    monkeypatch.setattr(apipay_client, "get_invoice", fake_get)
    monkeypatch.setattr(apipay_client, "cancel_invoice",
                        lambda iid: calls["cancelled"].append(iid))
    monkeypatch.setattr(client_notify, "send", fake_send)
    return calls


@pytest.fixture
def client(monkeypatch):
    config.X_SERVICE_TOKEN = _KEY
    monkeypatch.setattr("blueprints.manager_api._single_table_write", lambda row: None)
    monkeypatch.setattr("blueprints.manager_api._single_table_erase", lambda row: None)
    monkeypatch.setattr("blueprints.manager_api.upsert_booking_row", lambda row: None)
    monkeypatch.setattr("blueprints.manager_api.refresh_week_sheet", lambda: None)
    monkeypatch.setattr(apipay_service, "sync_sheets", lambda ids, released: None)
    app = Flask(__name__)
    app.register_blueprint(manager_api)
    app.register_blueprint(webhook_bp.apipay_webhook)
    return app.test_client()


def _webhook(client, invoice, status, event="invoice.status_changed"):
    raw = json.dumps({"event": event, "invoice": {
        "id": invoice["id"], "status": status,
        "external_order_id": invoice["external_order_id"]}}).encode()
    sig = "sha256=" + hmac.new(_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/apipay", data=raw,
                       headers={"Content-Type": "application/json",
                                "X-Webhook-Signature": sig})


def _slot(date_, ts="10:00", te="11:00", field=1, **extra):
    return {"field": field, "date": date_, "time_start": ts, "time_end": te, **extra}


def _create(client, plan, price=450000, slots=None, phone=_KASPI,
            start="2027-01-01", end="2027-03-31"):
    body = {"customer_name": "ТОО Ромашка", "phone": phone, "start_date": start,
            "end_date": end, "price": price, "payment_plan": plan}
    if slots is not None:
        body["slots"] = slots
    return client.post("/api/manager/contracts", json=body, headers=_HDR)


def _plan(client, contract_id):
    r = client.get(f"/api/manager/contracts/{contract_id}/payments", headers=_HDR)
    assert r.status_code == 200, r.get_json()
    return r.get_json()["data"]


def _make_due(installment_id=None, contract_id=None):
    """Pull due dates into the past so the sweep picks them up now."""
    with _conn() as conn:
        with conn.cursor() as cur:
            if installment_id is not None:
                cur.execute("UPDATE contract_installments SET due_at = NOW() - INTERVAL '1 minute' "
                            "WHERE id = %s", (installment_id,))
            else:
                cur.execute("UPDATE contract_installments SET due_at = NOW() - INTERVAL '1 minute' "
                            "WHERE contract_id = %s AND status = 'scheduled'", (contract_id,))


def _invoice_rows(installment_id):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, status, amount, booking_ids, invoice_id "
                        "FROM apipay_invoices WHERE contract_installment_id = %s ORDER BY id",
                        (installment_id,))
            return cur.fetchall()


# ---------------------------------------------------------------------------
# Creating a plan
# ---------------------------------------------------------------------------

def test_static_plan_creates_even_monthly_installments(client, apipay_on):
    r = _create(client, {"mode": "static", "frequency": "monthly"})
    assert r.status_code == 200, r.get_json()
    plan = r.get_json()["data"]["payment_plan"]

    assert plan["payment_mode"] == "static"
    assert plan["payment_channel"] == "kaspi_invoice"
    assert plan["payment_status"] == "scheduled"
    assert [i["amount"] for i in plan["installments"]] == [150000.0] * 3
    assert [i["due_at"][:10] for i in plan["installments"]] == [
        "2027-01-01", "2027-02-01", "2027-03-01"]
    assert apipay_on["checked"] == [_KASPI]
    assert apipay_on["created"] == []          # nothing is billed at creation


def test_number_without_kaspi_gets_the_whatsapp_link_channel(client, apipay_on):
    r = _create(client, {"mode": "static", "frequency": "monthly", "billing_phone": _NO_KASPI})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["data"]["payment_plan"]["payment_channel"] == "whatsapp_link"


def test_failed_kaspi_check_creates_nothing(client, apipay_on):
    apipay_on["fail_check"] = True
    r = _create(client, {"mode": "static", "frequency": "monthly"},
                slots=[_slot("2027-01-05")])
    assert r.status_code == 502
    assert r.get_json()["code"] == "PAYMENT_PROVIDER_ERROR"
    assert client.get("/api/manager/contracts", headers=_HDR).get_json()["data"] == []


def test_contract_without_a_plan_is_unchanged(client, apipay_on):
    body = {"customer_name": "Old", "phone": _KASPI, "start_date": "2027-01-01",
            "end_date": "2027-01-31", "price": 100000}
    r = client.post("/api/manager/contracts", json=body, headers=_HDR)
    assert r.status_code == 200
    plan = _plan(client, r.get_json()["data"]["contract_id"])
    assert plan["payment_mode"] is None
    assert plan["payment_status"] == "none"
    assert plan["installments"] == []
    assert apipay_on["checked"] == []


# ---------------------------------------------------------------------------
# Issuing and settling — Kaspi invoices
# ---------------------------------------------------------------------------

def _static_contract(client, **plan_extra):
    r = _create(client, {"mode": "static", "frequency": "monthly", **plan_extra})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()["data"]
    return data["contract_id"], data["payment_plan"]["installments"]


def test_due_installment_is_invoiced_through_the_apipay_outbox(client, apipay_on):
    cid, inst = _static_contract(client)
    _make_due(inst[0]["id"])

    assert contract_billing.issue_due_installments() == 1

    assert len(apipay_on["created"]) == 1
    sent = apipay_on["created"][0]
    assert sent["amount"] == 150000 and sent["phone"] == _KASPI
    assert sent["description"] == f"Договор №{cid}: платёж 1 из 3"
    rows = _invoice_rows(inst[0]["id"])
    assert len(rows) == 1 and rows[0][3] == []          # no booking_ids
    plan = _plan(client, cid)
    assert plan["installments"][0]["status"] == "issued"
    assert plan["payment_status"] == "awaiting"
    # A second sweep finds nothing new.
    assert contract_billing.issue_due_installments() == 0


def test_paid_webhook_settles_the_installment_and_tells_the_client(client, apipay_on):
    cid, inst = _static_contract(client)
    _make_due(inst[0]["id"])
    contract_billing.issue_due_installments()
    invoice = apipay_on["created"][0]

    r = _webhook(client, invoice, "paid")
    assert r.status_code == 200

    plan = _plan(client, cid)
    first = plan["installments"][0]
    assert first["status"] == "paid" and first["paid_amount"] == 150000.0
    assert first["paid_via"] == "apipay"
    assert plan["summary"]["paid"] == 150000.0
    assert plan["summary"]["left_to_pay"] == 300000.0
    assert plan["payment_status"] == "scheduled"

    # Redelivery changes nothing.
    assert _webhook(client, invoice, "paid").get_json()["duplicate"] is True
    assert _plan(client, cid)["summary"]["paid"] == 150000.0

    import time
    for _ in range(50):          # notified off the webhook thread
        if any(m["key"] == "contract_installment_paid" for m in apipay_on["messages"]):
            break
        time.sleep(0.05)
    paid_msgs = [m for m in apipay_on["messages"] if m["key"] == "contract_installment_paid"]
    assert paid_msgs and paid_msgs[0]["to"] == _KASPI and paid_msgs[0]["contract_id"] == cid


def test_all_installments_paid_marks_the_contract_paid(client, apipay_on):
    cid, inst = _static_contract(client)
    for i in inst:
        _make_due(i["id"])
    contract_billing.issue_due_installments()
    for invoice in apipay_on["created"]:
        _webhook(client, invoice, "paid")
    assert _plan(client, cid)["payment_status"] == "paid"


def test_expired_invoice_is_retried_then_goes_overdue(client, apipay_on):
    cid, inst = _static_contract(client)
    iid = inst[0]["id"]
    for attempt in range(1, 4):
        _make_due(iid)
        assert contract_billing.issue_due_installments() == 1
        _webhook(client, apipay_on["created"][-1], "expired")
        state = _plan(client, cid)["installments"][0]
        if attempt < 3:
            assert state["status"] == "scheduled", attempt
            assert state["attempts"] == attempt
        else:
            assert state["status"] == "overdue"
    assert [c["amount"] for c in apipay_on["created"]] == [150000] * 3
    assert _plan(client, cid)["payment_status"] == "overdue"

    # A manager sends it once more by hand.
    r = client.post(f"/api/manager/contracts/{cid}/installments/{iid}/send", headers=_HDR)
    assert r.status_code == 200, r.get_json()
    assert len(apipay_on["created"]) == 4
    assert _plan(client, cid)["installments"][0]["status"] == "issued"


def test_stale_expiry_of_an_older_invoice_does_not_reschedule(client, apipay_on):
    cid, inst = _static_contract(client)
    iid = inst[0]["id"]
    _make_due(iid)
    contract_billing.issue_due_installments()
    first = apipay_on["created"][0]
    _webhook(client, first, "expired")          # → rescheduled
    _make_due(iid)
    contract_billing.issue_due_installments()   # second invoice out
    _webhook(client, first, "cancelled")        # late news about the FIRST one
    assert _plan(client, cid)["installments"][0]["status"] == "issued"


def test_failed_send_stays_queued_and_the_sweep_resends_it(client, apipay_on):
    cid, inst = _static_contract(client)
    _make_due(inst[0]["id"])
    apipay_on["fail_send"] = True
    contract_billing.issue_due_installments()
    assert apipay_on["created"] == []
    assert _plan(client, cid)["installments"][0]["status"] == "issued"

    apipay_on["fail_send"] = False
    with _conn() as conn:
        with conn.cursor() as cur:       # lease expired
            cur.execute("UPDATE apipay_invoices SET updated_at = NOW() - INTERVAL '5 minutes'")
    assert apipay_service.sweep_pending_sends() == 1
    assert apipay_on["created"][0]["amount"] == 150000
    assert _invoice_rows(inst[0]["id"])[0][4] is not None       # invoice_id stored


def test_sweep_drops_a_queued_invoice_for_an_installment_paid_meanwhile(client, apipay_on):
    cid, inst = _static_contract(client)
    iid = inst[0]["id"]
    _make_due(iid)
    apipay_on["fail_send"] = True
    contract_billing.issue_due_installments()
    apipay_on["fail_send"] = False

    r = client.post(f"/api/manager/contracts/{cid}/installments/{iid}/mark-paid",
                    json={"note": "наличные"}, headers=_HDR)
    assert r.status_code == 200, r.get_json()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE apipay_invoices SET updated_at = NOW() - INTERVAL '5 minutes'")
    assert apipay_service.sweep_pending_sends() == 0
    assert apipay_on["created"] == []
    assert _invoice_rows(iid)[0][1] == "cancelled"


def test_reconciliation_settles_an_installment_whose_webhook_was_lost(client, apipay_on):
    cid, inst = _static_contract(client)
    _make_due(inst[0]["id"])
    contract_billing.issue_due_installments()
    invoice_id = apipay_on["created"][0]["id"]
    apipay_on["remote"][invoice_id] = {"id": invoice_id, "status": "paid"}
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE apipay_invoices SET updated_at = NOW() - INTERVAL '10 minutes'")

    assert apipay_service.reconcile_open_invoices() == 1
    assert _plan(client, cid)["installments"][0]["status"] == "paid"


# ---------------------------------------------------------------------------
# WhatsApp link channel
# ---------------------------------------------------------------------------

def test_link_channel_sends_the_payment_link_and_waits_for_a_manager(client, apipay_on):
    r = _create(client, {"mode": "static", "frequency": "monthly", "billing_phone": _NO_KASPI})
    cid = r.get_json()["data"]["contract_id"]
    iid = r.get_json()["data"]["payment_plan"]["installments"][0]["id"]
    _make_due(iid)

    assert contract_billing.issue_due_installments() == 1
    assert apipay_on["created"] == []
    msg = apipay_on["messages"][-1]
    assert msg["key"] == "contract_payment_link" and msg["to"] == _NO_KASPI
    assert msg["link"] == config.KASPI_PAYMENT_URL
    assert _plan(client, cid)["installments"][0]["status"] == "issued"

    # Resend the link.
    r = client.post(f"/api/manager/contracts/{cid}/installments/{iid}/send", headers=_HDR)
    assert r.status_code == 200 and r.get_json()["data"]["notified"] is True

    r = client.post(f"/api/manager/contracts/{cid}/installments/{iid}/mark-paid",
                    json={}, headers=_HDR)
    first = r.get_json()["data"]["installments"][0]
    assert first["status"] == "paid" and first["paid_via"] == "manual"
    assert first["paid_amount"] == 150000.0


def test_paying_the_whole_price_by_hand_cancels_the_rest(client, apipay_on):
    cid, inst = _static_contract(client)
    r = client.post(f"/api/manager/contracts/{cid}/installments/{inst[0]['id']}/mark-paid",
                    json={"amount": 450000}, headers=_HDR)
    assert r.status_code == 200, r.get_json()
    data = r.get_json()["data"]
    assert [i["status"] for i in data["installments"]] == ["paid", "cancelled", "cancelled"]
    assert data["payment_status"] == "paid"
    _make_due(contract_id=cid)
    assert contract_billing.issue_due_installments() == 0


def test_a_partial_manual_payment_moves_the_difference_onto_the_rest(client, apipay_on):
    cid, inst = _static_contract(client)
    r = client.post(f"/api/manager/contracts/{cid}/installments/{inst[0]['id']}/mark-paid",
                    json={"amount": 50000}, headers=_HDR)
    assert [i["amount"] for i in r.get_json()["data"]["installments"][1:]] == [200000.0,
                                                                               200000.0]


def test_link_channel_is_used_when_apipay_is_off(client, apipay_on, monkeypatch):
    monkeypatch.setattr(config, "APIPAY_ENABLED", False)
    r = _create(client, {"mode": "static", "frequency": "monthly"})
    assert r.get_json()["data"]["payment_plan"]["payment_channel"] == "whatsapp_link"
    assert apipay_on["checked"] == []


# ---------------------------------------------------------------------------
# Dynamic (per-booking) plans
# ---------------------------------------------------------------------------

def _dynamic_contract(client, price=200000, dates=("2027-01-05", "2027-01-12")):
    r = _create(client, {"mode": "dynamic"}, price=price,
                slots=[_slot(d, "19:00", "21:00") for d in dates])
    assert r.status_code == 200, r.get_json()
    data = r.get_json()["data"]
    return data["contract_id"], data["booking_ids"], data["payment_plan"]


def test_dynamic_plan_bills_each_booking_its_share(client, apipay_on):
    """The spec's example: 200k over 2 bookings → 100k after each one."""
    cid, booking_ids, plan = _dynamic_contract(client)
    inst = plan["installments"]
    assert [i["booking_id"] for i in inst] == booking_ids
    assert [i["amount"] for i in inst] == [None, None]           # fixed at issue time
    assert inst[0]["due_at"].startswith("2027-01-05T")           # when the booking ends
    assert plan["summary"]["per_booking_estimate"] == 100000.0

    _make_due(inst[0]["id"])
    contract_billing.issue_due_installments()
    assert apipay_on["created"][0]["amount"] == 100000
    assert apipay_on["created"][0]["description"] == f"Договор №{cid}: бронь 05.01.2027"
    _webhook(client, apipay_on["created"][0], "paid")

    _make_due(inst[1]["id"])
    contract_billing.issue_due_installments()
    assert apipay_on["created"][1]["amount"] == 100000
    _webhook(client, apipay_on["created"][1], "paid")
    assert _plan(client, cid)["payment_status"] == "paid"


def test_adding_bookings_re_spreads_what_is_left(client, apipay_on):
    cid, booking_ids, plan = _dynamic_contract(client)
    _make_due(plan["installments"][0]["id"])
    contract_billing.issue_due_installments()
    _webhook(client, apipay_on["created"][0], "paid")             # 100k paid

    r = client.post(f"/api/manager/contracts/{cid}/bookings/batch",
                    json={"slots": [_slot("2027-01-19", "19:00", "21:00")]}, headers=_HDR)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["data"]["installments_added"] == 1

    # 100k left over 2 bookings → 50k each, the last one closes it exactly.
    assert _plan(client, cid)["summary"]["per_booking_estimate"] == 50000.0
    _make_due(contract_id=cid)
    contract_billing.issue_due_installments()
    assert [c["amount"] for c in apipay_on["created"][1:]] == [50000, 50000]


def test_cancelling_a_booking_takes_its_installment_and_invoice_with_it(client, apipay_on):
    cid, booking_ids, plan = _dynamic_contract(client, price=300000,
                                               dates=("2027-01-05", "2027-01-12", "2027-01-19"))
    second = plan["installments"][1]
    _make_due(second["id"])
    contract_billing.issue_due_installments()
    open_invoice = apipay_on["created"][0]
    assert open_invoice["amount"] == 100000

    r = client.delete(f"/api/manager/contracts/{cid}/bookings/batch",
                      json={"booking_ids": [booking_ids[1]]}, headers=_HDR)
    assert r.status_code == 200, r.get_json()

    assert open_invoice["id"] in apipay_on["cancelled"]
    plan = _plan(client, cid)
    assert plan["installments"][1]["status"] == "cancelled"
    # The whole price now rides on the two bookings left.
    assert plan["summary"]["per_booking_estimate"] == 150000.0

    # A late 'paid' for the cancelled installment is flagged, not counted.
    _webhook(client, open_invoice, "paid")
    assert _plan(client, cid)["summary"]["paid"] == 0.0
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT error_code FROM apipay_invoices WHERE invoice_id = %s",
                        (open_invoice["id"],))
            assert cur.fetchone()[0] == "paid_after_cancellation"


def test_manager_can_fix_one_dynamic_share(client, apipay_on):
    cid, _, plan = _dynamic_contract(client)
    first = plan["installments"][0]["id"]
    r = client.patch(f"/api/manager/contracts/{cid}/installments/{first}",
                     json={"amount": 120000}, headers=_HDR)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["data"]["summary"]["per_booking_estimate"] == 80000.0
    _make_due(contract_id=cid)
    contract_billing.issue_due_installments()
    assert sorted(c["amount"] for c in apipay_on["created"]) == [80000, 120000]


# ---------------------------------------------------------------------------
# Manager adjustments and cancellation
# ---------------------------------------------------------------------------

def test_locking_one_static_amount_re_spreads_the_rest(client, apipay_on):
    cid, inst = _static_contract(client)
    r = client.patch(f"/api/manager/contracts/{cid}/installments/{inst[0]['id']}",
                     json={"amount": 250000}, headers=_HDR)
    assert r.status_code == 200, r.get_json()
    assert [i["amount"] for i in r.get_json()["data"]["installments"]] == [250000.0, 100000.0,
                                                                           100000.0]
    r = client.patch(f"/api/manager/contracts/{cid}/installments/{inst[0]['id']}",
                     json={"amount": 500000}, headers=_HDR)
    assert r.status_code == 400
    assert r.get_json()["code"] == "INVALID_AMOUNT"


def test_price_change_re_spreads_the_static_schedule(client, apipay_on):
    cid, inst = _static_contract(client)
    _make_due(inst[0]["id"])
    contract_billing.issue_due_installments()
    _webhook(client, apipay_on["created"][0], "paid")          # 150k paid

    r = client.patch(f"/api/manager/contracts/{cid}", json={"price": 350000}, headers=_HDR)
    assert r.status_code == 200, r.get_json()
    assert [i["amount"] for i in _plan(client, cid)["installments"]] == [150000.0, 100000.0,
                                                                         100000.0]


def test_cancelling_the_contract_cancels_open_invoices(client, apipay_on):
    cid, inst = _static_contract(client)
    _make_due(inst[0]["id"])
    contract_billing.issue_due_installments()

    r = client.delete(f"/api/manager/contracts/{cid}", headers=_HDR)
    assert r.status_code == 200

    assert apipay_on["cancelled"] == [apipay_on["created"][0]["id"]]
    plan = _plan(client, cid)
    assert {i["status"] for i in plan["installments"]} == {"cancelled"}
    assert plan["payment_status"] == "cancelled"
    _make_due(contract_id=cid)
    assert contract_billing.issue_due_installments() == 0


def test_replacing_a_plan_keeps_what_was_already_paid(client, apipay_on):
    cid, inst = _static_contract(client)
    _make_due(inst[0]["id"])
    contract_billing.issue_due_installments()
    _webhook(client, apipay_on["created"][0], "paid")

    r = client.put(f"/api/manager/contracts/{cid}/payment-plan",
                   json={"payment_plan": {"mode": "static", "due_dates": [
                       "2027-02-15", "2027-03-01", "2027-03-15", "2027-03-30"]}},
                   headers=_HDR)
    assert r.status_code == 200, r.get_json()
    live = [i for i in r.get_json()["data"]["installments"] if i["status"] != "cancelled"]
    assert [i["amount"] for i in live] == [150000.0, 75000.0, 75000.0, 75000.0, 75000.0]

    # Numbered among live installments, not by seq.
    _make_due(live[1]["id"])
    contract_billing.issue_due_installments()
    assert apipay_on["created"][-1]["description"] == f"Договор №{cid}: платёж 2 из 5"


def test_stopping_a_plan_cancels_the_unpaid_part(client, apipay_on):
    cid, inst = _static_contract(client)
    _make_due(inst[0]["id"])
    contract_billing.issue_due_installments()
    _webhook(client, apipay_on["created"][0], "paid")
    _make_due(inst[1]["id"])
    contract_billing.issue_due_installments()

    r = client.delete(f"/api/manager/contracts/{cid}/payment-plan", headers=_HDR)
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert [i["status"] for i in data["installments"]] == ["paid", "cancelled", "cancelled"]
    assert data["payment_status"] == "stopped"
    assert apipay_on["cancelled"] == [apipay_on["created"][1]["id"]]


# ---------------------------------------------------------------------------
# Conflicts, preview, and the add-bookings route
# ---------------------------------------------------------------------------

def test_check_slots_lists_every_conflicting_occurrence(client, apipay_on):
    _dynamic_contract(client, dates=("2027-01-05",))
    r = client.post("/api/manager/contracts/check-slots", json={"slots": [
        _slot("2027-01-05", "20:00", "22:00", repeat_mode="weekly", repeat_until="2027-01-19"),
    ]}, headers=_HDR)
    assert r.status_code == 200, r.get_json()
    data = r.get_json()["data"]
    assert data["free"] is False and data["occurrences"] == 3
    assert [c["date"] for c in data["conflicts"]] == ["2027-01-05"]


def test_conflicting_contract_is_refused_with_the_conflicts_listed(client, apipay_on):
    _dynamic_contract(client, dates=("2027-01-05",))
    r = _create(client, {"mode": "dynamic"}, slots=[_slot("2027-01-05", "20:00", "22:00")])
    assert r.status_code == 409
    assert r.get_json()["conflicts"][0]["date"] == "2027-01-05"


def test_preview_shows_the_installments_without_writing(client, apipay_on):
    r = client.post("/api/manager/contracts/payment-plan/preview", json={
        "price": 200000, "start_date": "2027-01-01", "end_date": "2027-01-31",
        "phone": _KASPI, "payment_plan": {"mode": "dynamic"},
        "slots": [_slot("2027-01-05", repeat_mode="weekly", repeat_until="2027-01-26")],
    }, headers=_HDR)
    assert r.status_code == 200, r.get_json()
    assert [i["amount"] for i in r.get_json()["data"]["installments"]] == [50000.0] * 4
    assert apipay_on["checked"] == []
    assert client.get("/api/manager/contracts", headers=_HDR).get_json()["data"] == []


def test_adding_bookings_to_a_contract_without_a_plan_works(client, apipay_on):
    body = {"customer_name": "Old", "phone": _KASPI, "start_date": "2027-01-01",
            "end_date": "2027-01-31", "price": 100000}
    cid = client.post("/api/manager/contracts", json=body, headers=_HDR).get_json()["data"]["contract_id"]
    r = client.post(f"/api/manager/contracts/{cid}/bookings/batch",
                    json={"slots": [_slot("2027-01-07")]}, headers=_HDR)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["data"]["created_count"] == 1
    assert r.get_json()["data"]["installments_added"] == 0


# ---------------------------------------------------------------------------
# The channel is decided at each send, not at creation
# ---------------------------------------------------------------------------

def test_contract_made_before_apipay_gets_kaspi_invoices_once_it_is_on(client, apipay_on,
                                                                       monkeypatch):
    monkeypatch.setattr(config, "APIPAY_ENABLED", False)
    cid, inst = _static_contract(client)
    assert _plan(client, cid)["payment_channel"] == "whatsapp_link"

    monkeypatch.setattr(config, "APIPAY_ENABLED", True)
    _make_due(inst[0]["id"])
    assert contract_billing.issue_due_installments() == 1

    assert len(apipay_on["created"]) == 1 and apipay_on["created"][0]["phone"] == _KASPI
    plan = _plan(client, cid)
    assert plan["payment_channel"] == "kaspi_invoice"
    assert plan["installments"][0]["channel"] == "kaspi_invoice"
    assert not [m for m in apipay_on["messages"] if m["key"] == "contract_payment_link"]


def test_send_now_upgrades_a_whatsapp_installment_to_a_kaspi_invoice(client, apipay_on,
                                                                     monkeypatch):
    monkeypatch.setattr(config, "APIPAY_ENABLED", False)
    cid, inst = _static_contract(client)
    iid = inst[0]["id"]
    _make_due(iid)
    contract_billing.issue_due_installments()
    assert apipay_on["messages"][-1]["key"] == "contract_payment_link"

    monkeypatch.setattr(config, "APIPAY_ENABLED", True)
    r = client.post(f"/api/manager/contracts/{cid}/installments/{iid}/send", headers=_HDR)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["data"]["channel"] == "kaspi_invoice"
    assert apipay_on["created"][0]["amount"] == 150000
    first = _plan(client, cid)["installments"][0]
    assert first["status"] == "issued" and first["channel"] == "kaspi_invoice"

    # The Kaspi invoice settles it as usual.
    _webhook(client, apipay_on["created"][0], "paid")
    assert _plan(client, cid)["installments"][0]["status"] == "paid"


def test_a_number_that_joins_kaspi_later_gets_kaspi_invoices(client, apipay_on):
    r = _create(client, {"mode": "static", "frequency": "monthly", "billing_phone": _NO_KASPI})
    cid = r.get_json()["data"]["contract_id"]
    assert r.get_json()["data"]["payment_plan"]["payment_channel"] == "whatsapp_link"

    apipay_on["no_kaspi"].clear()
    _make_due(contract_id=cid)
    contract_billing.issue_due_installments()
    assert len(apipay_on["created"]) == 3
    assert _plan(client, cid)["payment_channel"] == "kaspi_invoice"


def test_a_chosen_whatsapp_link_is_kept_even_with_kaspi(client, apipay_on):
    r = _create(client, {"mode": "static", "frequency": "monthly", "channel": "whatsapp_link"})
    cid = r.get_json()["data"]["contract_id"]
    assert r.get_json()["data"]["payment_plan"]["payment_channel_forced"] is True
    iid = r.get_json()["data"]["payment_plan"]["installments"][0]["id"]

    _make_due(iid)
    contract_billing.issue_due_installments()
    client.post(f"/api/manager/contracts/{cid}/installments/{iid}/send", headers=_HDR)

    assert apipay_on["created"] == []
    assert [m["key"] for m in apipay_on["messages"]] == ["contract_payment_link"] * 2
    assert apipay_on["checked"] == []        # never even asked


def test_a_failed_kaspi_check_at_send_leaves_the_installment_scheduled(client, apipay_on):
    cid, inst = _static_contract(client)
    iid = inst[0]["id"]
    _make_due(iid)
    apipay_on["fail_check"] = True

    assert contract_billing.issue_due_installments() == 0
    assert _plan(client, cid)["installments"][0]["status"] == "scheduled"
    r = client.post(f"/api/manager/contracts/{cid}/installments/{iid}/send", headers=_HDR)
    assert r.status_code == 502 and r.get_json()["code"] == "PAYMENT_PROVIDER_ERROR"

    apipay_on["fail_check"] = False
    assert contract_billing.issue_due_installments() == 1
    assert len(apipay_on["created"]) == 1
