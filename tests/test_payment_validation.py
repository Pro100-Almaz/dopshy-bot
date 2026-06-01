"""Validation tests: recipient match, amount, date window, dedup."""

import os
import uuid

import pytest

import config
from integrations import booking_service as svc
from integrations import payment_validation, postgres

_RECEIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "receipts")


def _pdf(name):
    with open(os.path.join(_RECEIPTS, name), "rb") as fh:
        return fh.read()


def _awaiting(field=1, ts="18:00", te="19:00", phone="7700"):
    """Create an awaiting_payment booking (field 1 = 5x5 = 35000/h → price_total=35000 for 1h)."""
    token = str(uuid.uuid4())
    bid = svc.create_draft("c", phone, token, date="2026-07-01", time_start=ts, time_end=te,
                           field=field, format="5x5", players=8, customer_name="T")["data"]["booking_id"]
    svc.request_payment(bid, token)
    return postgres.get_booking(bid)


@pytest.fixture
def no_date_limit(monkeypatch):
    """Disable the date-age check so sample receipts (dated in the past) still validate."""
    monkeypatch.setattr(config, "PAYMENT_RECEIPT_MAX_AGE_HOURS", 10 ** 9)


def test_price_total_computed():
    b = _awaiting(field=1, ts="18:00", te="19:00")
    assert float(b["price_total"]) == 35000  # 1h * 35000


def test_accept_genuine_kaspi(no_date_limit):
    b = _awaiting()  # price_total 35000, min 17500
    res = payment_validation.validate_receipt(b, _pdf("receipt.pdf"))  # genuine, 20000
    assert res["ok"], res


def test_reject_wrong_recipient(no_date_limit, monkeypatch):
    # Pin recipients to only the legacy DOPSHY BIN so the fixture receipt
    # (bin 250740003149) is treated as unknown.
    monkeypatch.setattr(
        payment_validation.postgres,
        "get_payment_recipients",
        lambda: [{"bank": "kaspi", "bin": "870203301478", "name": "DOPSHY", "phone": None}],
    )
    b = _awaiting()
    res = payment_validation.validate_receipt(b, _pdf("receipt (3).pdf"))  # bin 250740003149
    assert not res["ok"]
    assert res["code"] == "recipient"


def test_reject_insufficient_amount(no_date_limit):
    b = _awaiting()  # min 17500
    res = payment_validation.validate_receipt(b, _pdf("download.pdf"))  # genuine but only 5000
    assert not res["ok"]
    assert res["code"] == "amount"


def test_reject_stale_date():
    b = _awaiting()  # default 24h window; sample dated 2026-05-20
    res = payment_validation.validate_receipt(b, _pdf("receipt.pdf"))
    assert not res["ok"]
    assert res["code"] == "date"


def test_accept_genuine_halyk(no_date_limit):
    b = _awaiting()  # min 17500; halyk receipt is 20000 to the right phone
    res = payment_validation.validate_receipt(b, _pdf("halyk_receipt_3250927742.pdf"))
    assert res["ok"], res


def test_receipt_dedup():
    b1 = _awaiting(field=1, ts="18:00", te="19:00")
    b2 = _awaiting(field=2, ts="18:00", te="19:00", phone="7711")
    parsed = {"bank": "kaspi", "amount": 35000, "ref": "DUP-REF-1", "date": None}
    r1 = svc.submit_payment_proof(b1["id"], parsed=parsed)
    r2 = svc.submit_payment_proof(b2["id"], parsed=parsed)
    assert r1["ok"]
    assert not r2["ok"]
    assert r2["code"] == "PAYMENT_DUPLICATE"
