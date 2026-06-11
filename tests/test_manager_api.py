"""Tests for the manager_api blueprint."""

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
    config.MANAGER_API_KEY = _KEY
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
