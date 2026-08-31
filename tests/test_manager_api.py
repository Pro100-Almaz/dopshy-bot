"""Tests for the manager_api blueprint."""

import time
import uuid

import pytest
from flask import Flask

import config
from blueprints.manager_api import manager_api
from integrations.repo.postgres import _conn

_KEY = "test-key"
_HDR = {"X-API-Key": _KEY}


@pytest.fixture
def client():
    config.X_SERVICE_TOKEN = _KEY
    app = Flask(__name__)
    app.register_blueprint(manager_api)
    return app.test_client()


def _events(booking_id):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT event FROM booking_events WHERE booking_id = %s", (booking_id,))
            return [r[0] for r in cur.fetchall()]


def test_rejects_missing_key(client):
    r = client.get("/api/manager/bookings")
    assert r.status_code == 401


def test_rejects_wrong_key(client):
    r = client.get("/api/manager/bookings", headers={"X-API-Key": "nope"})
    assert r.status_code == 401


def test_create_and_get(client):
    body = {"field": 1, "date": "2026-09-01", "time_start": "10:00",
            "time_end": "11:00", "repeat": "none",
            "customer": "Манагер", "client_token": str(uuid.uuid4())}
    r = client.post("/api/manager/bookings", json=body, headers=_HDR)
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"]
    bid = data["data"]["booking_id"]

    g = client.get(f"/api/manager/bookings/{bid}", headers=_HDR)
    assert g.status_code == 200
    assert g.get_json()["data"]["state"] == "confirmed"


def test_create_slot_taken_returns_409(client):
    base = {"field": 1, "date": "2026-09-02", "time_start": "10:00",
            "time_end": "11:00", "repeat": "none"}
    assert client.post("/api/manager/bookings", json=base, headers=_HDR).status_code == 200
    overlap = {"field": 1, "date": "2026-09-02", "time_start": "10:30",
               "time_end": "11:30", "repeat": "none"}
    r = client.post("/api/manager/bookings", json=overlap, headers=_HDR)
    assert r.status_code == 409
    assert r.get_json()["code"] == "SLOT_TAKEN"


def test_patch_updates_and_records_event(client):
    body = {"field": 2, "date": "2026-09-03", "time_start": "12:00",
            "time_end": "13:00", "repeat": "none"}
    bid = client.post("/api/manager/bookings", json=body, headers=_HDR).get_json()["data"]["booking_id"]

    r = client.patch(f"/api/manager/bookings/{bid}", json={"notes": "VIP"}, headers=_HDR)
    assert r.status_code == 200 and r.get_json()["ok"]
    assert "manager_updated" in _events(bid)

    g = client.get(f"/api/manager/bookings/{bid}", headers=_HDR)
    assert g.get_json()["data"]["notes"] == "VIP"


def test_delete_cancels(client):
    body = {"field": 3, "date": "2026-09-04", "time_start": "14:00",
            "time_end": "15:00", "repeat": "none"}
    bid = client.post("/api/manager/bookings", json=body, headers=_HDR).get_json()["data"]["booking_id"]
    r = client.delete(f"/api/manager/bookings/{bid}", headers=_HDR)
    assert r.status_code == 200 and r.get_json()["ok"]
    g = client.get(f"/api/manager/bookings/{bid}", headers=_HDR)
    assert g.get_json()["data"]["state"] == "cancelled"


def test_contract_create_links_confirmed_zero_price_bookings(client, monkeypatch):
    monkeypatch.setattr("blueprints.manager_api._single_table_write", lambda row: None)
    body = {
        "customer_name": "Big Co",
        "phone": "77001234567",
        "start_date": "2026-12-01",
        "end_date": "2026-12-31",
        "price": 550000,
        "slots": [
            {"field": 1, "date": "2026-12-01", "time_start": "10:00", "time_end": "11:00"},
            {"field": 1, "date": "2026-12-08", "time_start": "10:00", "time_end": "11:00"},
        ],
    }

    r = client.post("/api/manager/contracts", json=body, headers=_HDR)

    assert r.status_code == 200, r.get_json()
    data = r.get_json()["data"]
    assert data["created_count"] == 2

    contract = client.get(f"/api/manager/contracts/{data['contract_id']}", headers=_HDR)
    assert contract.status_code == 200
    assert contract.get_json()["data"]["price"] == 550000.0
    assert contract.get_json()["data"]["booking_ids"] == data["booking_ids"]

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, price_total FROM bookings WHERE id = ANY(%s) ORDER BY id",
                (data["booking_ids"],),
            )
            rows = cur.fetchall()
    assert [row[0] for row in rows] == ["confirmed", "confirmed"]
    assert [float(row[1]) for row in rows] == [0.0, 0.0]


def test_contract_delete_cancels_linked_bookings(client, monkeypatch):
    monkeypatch.setattr("blueprints.manager_api._single_table_write", lambda row: None)
    monkeypatch.setattr("blueprints.manager_api.refresh_week_sheet", lambda: None)
    monkeypatch.setattr("integrations.apipay_service.on_bookings_cancelled", lambda *args, **kwargs: None)
    body = {
        "customer_name": "Big Co",
        "start_date": "2027-01-01",
        "end_date": "2027-01-31",
        "price": 900000,
        "slots": [
            {"field": 2, "date": "2027-01-05", "time_start": "18:00", "time_end": "19:00"},
            {"field": 2, "date": "2027-01-12", "time_start": "18:00", "time_end": "19:00"},
        ],
    }
    created = client.post("/api/manager/contracts", json=body, headers=_HDR).get_json()["data"]

    r = client.delete(f"/api/manager/contracts/{created['contract_id']}", headers=_HDR)

    assert r.status_code == 200
    assert sorted(r.get_json()["data"]["cancelled_ids"]) == sorted(created["booking_ids"])
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM contracts WHERE id = %s", (created["contract_id"],))
            assert cur.fetchone()[0] == "cancelled"
            cur.execute("SELECT DISTINCT state FROM bookings WHERE id = ANY(%s)", (created["booking_ids"],))
            assert {row[0] for row in cur.fetchall()} == {"cancelled"}


# ---------------------------------------------------------------------------
# Client notifications — a manager acts, the client is not in the room
# ---------------------------------------------------------------------------

_CLIENT = "87001234567"          # as a manager types it into the sheet
_CLIENT_E164 = "+77001234567"    # as a provider will accept it


@pytest.fixture
def quiet_client(client, monkeypatch):
    """`client`, with the Google Sheet stubbed out.

    The notification tests assert on WhatsApp, not on the sheet — and the real
    writer reaches the network, which turns them into slow flakes.
    """
    for name in ("_single_table_write", "_single_table_erase",
                 "upsert_booking_row"):
        monkeypatch.setattr(f"blueprints.manager_api.{name}", lambda row: None)
    monkeypatch.setattr("blueprints.manager_api.refresh_week_sheet", lambda: None)
    return client


@pytest.fixture
def sent(monkeypatch):
    """Every WhatsApp message the request sends, as (channel, to, text)."""
    messages = []
    monkeypatch.setattr("handlers.whatsapp_client.send_text_message",
                        lambda channel, to, text: messages.append((channel, to, text)))
    return messages


def _wait_for(messages, count=1, timeout=2.5):
    """Notifications go out on a daemon thread so the manager UI never waits on
    WhatsApp — which means the test has to."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(messages) < count:
        time.sleep(0.02)
    return messages


def _book(client, date, ts="10:00", te="11:00", field=1, phone=_CLIENT):
    body = {"field": field, "date": date, "time_start": ts, "time_end": te,
            "repeat": "none", "phone": phone, "customer": "Асхат"}
    r = client.post("/api/manager/bookings", json=body, headers=_HDR)
    assert r.status_code == 200, r.get_json()
    return r.get_json()["data"]["booking_id"]


def test_manager_cancel_tells_the_client(quiet_client, sent):
    bid = _book(quiet_client, "2026-10-01", "20:00", "21:00")

    assert quiet_client.delete(f"/api/manager/bookings/{bid}", headers=_HDR).status_code == 200

    _wait_for(sent)
    assert len(sent) == 1
    _, to, text = sent[0]
    assert to == _CLIENT_E164, "YCloud rejects anything but E.164"
    assert "отменена" in text and "жойылды" in text, "no inbound message → both languages"
    assert "20:00–21:00" in text, "the client has to know WHICH booking"


def test_second_cancel_does_not_message_the_client_again(quiet_client, sent):
    """A repeated DELETE also answers ok — it must not answer twice on WhatsApp."""
    bid = _book(quiet_client, "2026-10-02")

    quiet_client.delete(f"/api/manager/bookings/{bid}", headers=_HDR)
    _wait_for(sent)
    quiet_client.delete(f"/api/manager/bookings/{bid}", headers=_HDR)
    _wait_for(sent, count=2)          # gives a wrong second send time to land

    assert len(sent) == 1


def test_manager_status_change_tells_the_client(quiet_client, sent):
    bid = _book(quiet_client, "2026-10-03")

    r = quiet_client.patch(f"/api/manager/bookings/{bid}",
                           json={"status": "unpaid"}, headers=_HDR)
    assert r.status_code == 200 and r.get_json()["ok"]

    _wait_for(sent)
    assert len(sent) == 1
    assert "оплата не поступила" in sent[0][2]


def test_restating_the_same_status_says_nothing(quiet_client, sent):
    """Managers re-save rows. Bookkeeping is not news."""
    bid = _book(quiet_client, "2026-10-04")
    quiet_client.patch(f"/api/manager/bookings/{bid}",
                       json={"status": "cancelled"}, headers=_HDR)
    _wait_for(sent)
    assert len(sent) == 1

    quiet_client.patch(f"/api/manager/bookings/{bid}",
                       json={"status": "cancelled"}, headers=_HDR)
    _wait_for(sent, count=2)
    assert len(sent) == 1


def test_edits_that_are_not_status_changes_say_nothing(quiet_client, sent):
    """Notes and prices are internal — the client hears about state, not admin."""
    bid = _book(quiet_client, "2026-10-05")

    quiet_client.patch(f"/api/manager/bookings/{bid}",
                       json={"notes": "VIP", "price_total": 30000}, headers=_HDR)

    _wait_for(sent)
    assert sent == []


def test_a_booking_without_a_phone_is_not_a_failure(quiet_client, sent):
    """Walk-ins are entered with no number at all; there is simply nobody to
    tell, and that must not break the cancellation."""
    bid = _book(quiet_client, "2026-10-06", phone=None)

    r = quiet_client.delete(f"/api/manager/bookings/{bid}", headers=_HDR)

    assert r.status_code == 200 and r.get_json()["ok"]
    _wait_for(sent)
    assert sent == []


def test_cancelling_a_series_sends_one_message_listing_the_dates(quiet_client, sent):
    body = {"field": 2, "date": "2026-11-02", "time_start": "19:00",
            "time_end": "20:00", "repeat": "weekly", "end_date": "2026-11-23",
            "phone": _CLIENT, "customer": "Асхат"}
    assert quiet_client.post("/api/manager/bookings", json=body,
                             headers=_HDR).status_code == 200
    # Create answers with the LAST occurrence, and cancel-all only reaches
    # forward from the row it is given — so the series is cancelled from its
    # first date, which is what a manager clicking "cancel all" does.
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM bookings WHERE date = '2026-11-02'")
            bid = cur.fetchone()[0]

    r = quiet_client.delete(f"/api/manager/bookings/all/{bid}", headers=_HDR)
    assert r.status_code == 200 and r.get_json()["ok"]

    _wait_for(sent)
    assert len(sent) == 1, "four cancelled dates, one message"
    text = sent[0][2]
    assert "2 ноября" in text and "23 ноября" in text


def test_academy_group_patch_omits_group_name(monkeypatch, client):
    captured = {}

    def fake_edit(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "group_id": kwargs["group_id"]}

    monkeypatch.setattr("blueprints.manager_api.on_manual_group_edit", fake_edit)
    monkeypatch.setattr("blueprints.manager_api.refresh_all_groups", lambda: None)

    r = client.patch("/api/manager/academy_groups/7", json={"max_cap": 14}, headers=_HDR)

    assert r.status_code == 200
    assert captured == {"group_id": 7, "group_name": None, "max_cap": 14, "level": None, "trainer": None}


def test_academy_group_create_accepts_multiple_schedules(monkeypatch, client):
    seen = {"schedules": []}

    def fake_create(**kwargs):
        seen["group"] = kwargs
        return 7

    def fake_schedule(group_id, training_day, time_start, time_end):
        seen["schedules"].append({
            "group_id": group_id,
            "training_day": training_day,
            "time_start": time_start,
            "time_end": time_end,
        })
        return len(seen["schedules"])

    monkeypatch.setattr("blueprints.manager_api.create_or_update_group", fake_create)
    monkeypatch.setattr("blueprints.manager_api.setting_training_time", fake_schedule)
    monkeypatch.setattr("blueprints.manager_api.refresh_all_groups", lambda: None)

    r = client.post(
        "/api/manager/academy_groups",
        json={
            "group_type": "football",
            "group_name": "Kids",
            "max_cap": 12,
            "level": "Beginner",
            "schedules": [
                {"training_day": 0, "time_start": "16:00", "time_end": "17:30"},
                {"training_day": 2, "time_start": "18:00", "time_end": "19:30"},
            ],
        },
        headers=_HDR,
    )

    assert r.status_code == 201
    assert seen["group"]["level"] == "Beginner"
    assert seen["schedules"] == [
        {"group_id": 7, "training_day": 0, "time_start": "16:00", "time_end": "17:30"},
        {"group_id": 7, "training_day": 2, "time_start": "18:00", "time_end": "19:30"},
    ]
    assert len(r.get_json()["data"]["schedules"]) == 2


def test_academy_group_patch_updates_schedule_time(monkeypatch, client):
    captured = {}

    def fake_schedule_edit(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "group_id": kwargs["group_id"],
            "schedule_id": 9,
            "training_day": kwargs["training_day"],
            "time_start": kwargs["time_start"],
            "time_end": None,
        }

    monkeypatch.setattr("blueprints.manager_api.on_manual_group_schedule_edit", fake_schedule_edit)
    monkeypatch.setattr("blueprints.manager_api.refresh_all_groups", lambda: None)

    r = client.patch(
        "/api/manager/academy_groups/7",
        json={"training_day": 2, "time_start": "16:30"},
        headers=_HDR,
    )

    assert r.status_code == 200
    assert captured == {
        "group_id": 7,
        "training_day": 2,
        "new_training_day": None,
        "time_start": "16:30",
        "time_end": None,
    }


def test_academy_group_patch_updates_schedule_weekday(monkeypatch, client):
    captured = {}

    def fake_schedule_edit(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "group_id": kwargs["group_id"],
            "schedule_id": 9,
            "training_day": kwargs["new_training_day"],
            "time_start": "16:30",
            "time_end": "18:00",
        }

    monkeypatch.setattr("blueprints.manager_api.on_manual_group_schedule_edit", fake_schedule_edit)
    monkeypatch.setattr("blueprints.manager_api.refresh_all_groups", lambda: None)

    r = client.patch(
        "/api/manager/academy_groups/7",
        json={"previous_training_day": 2, "training_day": 4},
        headers=_HDR,
    )

    assert r.status_code == 200
    assert captured == {
        "group_id": 7,
        "training_day": 2,
        "new_training_day": 4,
        "time_start": None,
        "time_end": None,
    }


@pytest.mark.no_db
def test_contacts_legacy_list_response_is_preserved(client, monkeypatch):
    monkeypatch.setattr(
        "blueprints.manager_api._list_conversation_contacts",
        lambda: [{"chat_id": "wa:77000000001", "updated_at": "2026-08-29 10:00:00"}],
    )
    monkeypatch.setattr("blueprints.manager_api.repo.get_booking_customers", lambda: [])
    monkeypatch.setattr(
        "blueprints.manager_api.get_statuses",
        lambda phones: {phone: {"paused": False, "paused_reason": None} for phone in phones},
    )

    r = client.get("/api/manager/contacts", headers=_HDR)

    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, list)
    assert data[0]["phone"] == "77000000001"


@pytest.mark.no_db
def test_contacts_can_be_paginated(client, monkeypatch):
    monkeypatch.setattr(
        "blueprints.manager_api._list_conversation_contacts",
        lambda: [
            {"chat_id": "wa:77000000001", "updated_at": "2026-08-29 10:00:00"},
            {"chat_id": "wa:77000000002", "updated_at": "2026-08-30 10:00:00"},
            {"chat_id": "wa:77000000003", "updated_at": "2026-08-28 10:00:00"},
        ],
    )
    monkeypatch.setattr(
        "blueprints.manager_api.repo.get_booking_customers",
        lambda: [
            {
                "phone": "77000000004",
                "customer_name": "Client Four",
                "last_at": "2026-08-27 10:00:00",
            },
        ],
    )
    monkeypatch.setattr(
        "blueprints.manager_api.get_statuses",
        lambda phones: {
            phone: {"paused": phone == "77000000003", "paused_reason": "manual" if phone == "77000000003" else None}
            for phone in phones
        },
    )

    r = client.get("/api/manager/contacts?page=2&page_size=2", headers=_HDR)

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["page"] == 2
    assert data["page_size"] == 2
    assert data["total"] == 4
    assert data["total_pages"] == 2
    assert [row["phone"] for row in data["data"]] == ["77000000003", "77000000004"]
    assert data["data"][0]["paused"] is True
