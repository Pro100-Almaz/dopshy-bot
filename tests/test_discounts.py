from decimal import Decimal

import config
import pytest
from flask import Flask
from blueprints.manager_api import manager_api
from integrations import booking_service
from integrations.repo import booking_repo, customer_discount_repo


_KEY = "test-secret"
_HDR = {"X-API-Key": _KEY}


@pytest.fixture
def client():
    config.X_SERVICE_TOKEN = _KEY
    app = Flask(__name__)
    app.register_blueprint(manager_api)
    return app.test_client()


def _customer(phone="+7 (707) 111-22-33"):
    return customer_discount_repo.create_customer("Алия", phone, True, "admin@example.com")


def _discount(customer_id, amount=10_000, status="approved", usage_limit=5):
    return customer_discount_repo.create_discount(
        customer_id, amount, status=status, usage_limit=usage_limit,
        source="super@example.com",
    )


def test_customer_phone_is_normalized_and_existing_record_is_reused(client):
    first = client.post(
        "/api/manager/customers",
        json={"name": "Алия", "phone": "+7 (707) 111-22-33"}, headers=_HDR,
    )
    assert first.status_code == 201
    assert first.json["data"]["phone"] == "77071112233"

    existing = client.post(
        "/api/manager/customers",
        json={"phone": "7 707 111 22 33"}, headers=_HDR,
    )
    assert existing.status_code == 201
    assert existing.json["data"]["id"] == first.json["data"]["id"]


def test_pending_discount_cannot_be_applied():
    customer = _customer()
    discount = _discount(customer["id"], status="pending")

    result = booking_service.manager_create_booking(
        field=1, date="2027-06-01", end_date="2027-06-01",
        time_start="10:00", time_end="11:00", phone="77071112233",
        discount_id=discount["id"],
    )

    assert result["ok"] is False
    assert result["code"] == "DISCOUNT_UNAVAILABLE"
    assert customer_discount_repo.get_discount(discount["id"])["usages_left"] == 5


def test_approved_discount_applies_price_and_deactivates_at_zero():
    customer = _customer()
    discount = _discount(customer["id"], amount=20_000, usage_limit=1)

    result = booking_service.manager_create_booking(
        field=1, date="2027-06-02", end_date="2027-06-02",
        time_start="10:00", time_end="11:00", phone="+7 707 111 22 33",
        price_total=13_500, discount_id=discount["id"],
    )

    assert result["ok"] is True
    booking = booking_repo.get_booking(result["data"]["booking_id"])
    assert booking["phone"] == "77071112233"
    assert booking["customer_id"] == customer["id"]
    assert booking["discount_id"] == discount["id"]
    assert booking["discount_amount"] == Decimal("20000")
    assert booking["price_before_discount"] == Decimal("13500")
    assert booking["price_total"] == Decimal("0")
    used = customer_discount_repo.get_discount(discount["id"])
    assert used["usages_left"] == 0
    assert used["usages_count"] == 1
    assert used["is_active"] is False


def test_removing_discount_keeps_manager_deactivation():
    customer = _customer()
    discount = _discount(customer["id"])
    created = booking_service.manager_create_booking(
        field=1, date="2027-06-04", end_date="2027-06-04",
        time_start="10:00", time_end="11:00", phone="77071112233",
        discount_id=discount["id"],
    )
    customer_discount_repo.update_discount(discount["id"], is_active=False)

    booking_service.manager_update_booking(created["data"]["booking_id"], discount_id=None)

    returned = customer_discount_repo.get_discount(discount["id"])
    assert returned["usages_left"] == 5
    assert returned["is_active"] is False


def test_removing_discount_reactivates_exhausted_one():
    customer = _customer()
    discount = _discount(customer["id"], usage_limit=1)
    created = booking_service.manager_create_booking(
        field=1, date="2027-06-05", end_date="2027-06-05",
        time_start="10:00", time_end="11:00", phone="77071112233",
        discount_id=discount["id"],
    )

    booking_service.manager_update_booking(created["data"]["booking_id"], discount_id=None)

    returned = customer_discount_repo.get_discount(discount["id"])
    assert returned["usages_left"] == 1
    assert returned["is_active"] is True


def test_discount_must_match_booking_customer():
    customer = _customer()
    discount = _discount(customer["id"])

    result = booking_service.manager_create_booking(
        field=1, date="2027-06-03", end_date="2027-06-03",
        time_start="10:00", time_end="11:00", phone="77070000000",
        discount_id=discount["id"],
    )

    assert result["code"] == "DISCOUNT_CUSTOMER_MISMATCH"
    assert customer_discount_repo.get_discount(discount["id"])["usages_left"] == 5


def test_booking_requires_customer_id_or_phone():
    result = booking_service.manager_create_booking(
        field=1, date="2027-06-09", end_date="2027-06-09",
        time_start="10:00", time_end="11:00",
    )

    assert result["ok"] is False
    assert result["code"] == "CUSTOMER_REQUIRED"


def test_booking_accepts_explicit_customer_id_and_fills_phone():
    customer = _customer()

    result = booking_service.manager_create_booking(
        field=1, date="2027-06-10", end_date="2027-06-10",
        time_start="10:00", time_end="11:00", customer_id=customer["id"],
    )

    assert result["ok"] is True
    booking = booking_repo.get_booking(result["data"]["booking_id"])
    assert booking["customer_id"] == customer["id"]
    assert booking["phone"] == customer["phone"]


def test_batch_can_assign_discount_per_slot_atomically():
    customer = _customer()
    discount = _discount(customer["id"], usage_limit=2)
    slots = [
        {"field": 1, "date": "2027-06-04", "time_start": "10:00",
         "time_end": "11:00", "discount_id": discount["id"]},
        {"field": 1, "date": "2027-06-05", "time_start": "10:00",
         "time_end": "11:00", "discount_id": discount["id"]},
    ]

    result = booking_service.manager_create_bookings_batch(
        slots, phone="77071112233", prepayment=3_500,
    )

    assert result["ok"] is True
    assert customer_discount_repo.get_discount(discount["id"])["usages_left"] == 0
    bookings = [booking_repo.get_booking(bid) for bid in result["data"]["booking_ids"]]
    assert all(row["discount_id"] == discount["id"] for row in bookings)
    assert all(row["paid_avans"] == Decimal("3500") for row in bookings)


def test_batch_rolls_back_all_uses_when_coupon_runs_out():
    customer = _customer()
    discount = _discount(customer["id"], usage_limit=1)
    slots = [
        {"field": 1, "date": "2027-06-06", "time_start": "10:00",
         "time_end": "11:00", "discount_id": discount["id"]},
        {"field": 1, "date": "2027-06-07", "time_start": "10:00",
         "time_end": "11:00", "discount_id": discount["id"]},
    ]

    result = booking_service.manager_create_bookings_batch(slots, phone="77071112233")

    assert result["code"] == "DISCOUNT_UNAVAILABLE"
    assert customer_discount_repo.get_discount(discount["id"])["usages_left"] == 1


def test_manager_can_change_and_remove_booking_discount():
    customer = _customer()
    first = _discount(customer["id"], amount=5_000)
    second = _discount(customer["id"], amount=10_000)
    created = booking_service.manager_create_booking(
        field=1, date="2027-06-08", end_date="2027-06-08",
        time_start="10:00", time_end="11:00", phone="77071112233",
        price_total=13_500, discount_id=first["id"],
    )
    booking_id = created["data"]["booking_id"]

    changed = booking_service.manager_update_booking(booking_id, discount_id=second["id"])
    assert changed["ok"] is True
    row = booking_repo.get_booking(booking_id)
    assert row["discount_id"] == second["id"]
    assert row["price_total"] == Decimal("3500")
    assert customer_discount_repo.get_discount(first["id"])["usages_left"] == 5

    removed = booking_service.manager_update_booking(booking_id, discount_id=None)
    assert removed["ok"] is True
    row = booking_repo.get_booking(booking_id)
    assert row["discount_id"] is None
    assert row["price_total"] == Decimal("13500")
    assert customer_discount_repo.get_discount(second["id"])["usages_left"] == 5


def test_customer_and_discount_events_use_history_feed(client):
    created = client.post(
        "/api/manager/customers", json={"phone": "77071112233"}, headers=_HDR
    ).json["data"]
    discount = client.post(
        "/api/manager/discounts",
        json={"customer_id": created["id"], "discount_amount": 5000}, headers=_HDR,
    )
    assert discount.status_code == 201

    history = client.get("/api/manager/history?page_size=10", headers=_HDR).json["data"]
    assert any(row["customer_id"] == created["id"] for row in history)
    assert any(row["discount_id"] == discount.json["data"]["id"] for row in history)
