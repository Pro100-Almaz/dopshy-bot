from datetime import date, datetime, time
from decimal import Decimal

import pytest
from flask import Flask

import config
from blueprints.document_api import document_api


pytestmark = pytest.mark.no_db

_KEY = "test-key"
_HDR = {"Authorization": f"Bearer {_KEY}"}


def _client():
    config.X_SERVICE_TOKEN = _KEY
    app = Flask(__name__)
    app.register_blueprint(document_api)
    return app.test_client()


def test_extract_data_rejects_unsupported_bot_type():
    client = _client()

    response = client.post(
        "/api/manager/documents/extract-data",
        json={"bot_type": "boxing", "start_date": "2026-08-01", "end_date": "2026-08-31"},
        headers=_HDR,
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "detail": "Only arena document extraction is supported now."
    }


def test_extract_data_rejects_end_before_start():
    client = _client()

    response = client.post(
        "/api/manager/documents/extract-data",
        json={"bot_type": "arena", "start_date": "2026-09-01", "end_date": "2026-08-31"},
        headers=_HDR,
    )

    assert response.status_code == 400
    assert response.get_json()["detail"] == "start_date must be before or equal to end_date."


def test_extract_data_builds_report_payload(monkeypatch):
    client = _client()

    def fake_report_bookings(start, end, states):
        assert start == "2026-08-01"
        assert end == "2026-08-31"
        assert set(states) == {"confirmed", "completed"}
        return [
            {
                "id": 1,
                "customer_name": "Aruzhan",
                "field": 1,
                "date": date(2026, 8, 3),
                "time_start": time(10, 0),
                "time_end": time(12, 0),
                "state": "confirmed",
                "price_total": Decimal("40000"),
                "paid_cash": Decimal("10000"),
                "paid_kaspi_qr": None,
                "paid_avans": Decimal("5000"),
                "source": "manager",
            },
            {
                "id": 2,
                "customer_name": "Dias",
                "field": 2,
                "date": date(2026, 8, 4),
                "time_start": time(23, 0),
                "time_end": time(23, 59, 59),
                "state": "completed",
                "price_total": Decimal("20000"),
                "paid_cash": "",
                "paid_kaspi_qr": Decimal("7000"),
                "paid_avans": None,
                "source": "whatsapp",
            },
        ]

    monkeypatch.setattr(
        "integrations.document_service.booking_repo.get_report_bookings_in_range",
        fake_report_bookings,
    )
    monkeypatch.setattr(
        "integrations.document_service.booking_repo.get_fields_info",
        lambda: [{"id": 1}, {"id": 2}, {"id": 3}],
    )
    monkeypatch.setattr(
        "integrations.document_service.booking_service.get_payments",
        lambda: [{"booking_id": 1, "amount": Decimal("25000")}],
    )

    response = client.post(
        "/api/manager/documents/extract-data",
        json={"bot_type": "arena", "start_date": "2026-08-01", "end_date": "2026-08-31"},
        headers=_HDR,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["included_statuses"] == ["completed", "confirmed"]
    assert data["active_field_count"] == 3
    assert data["summary"]["booking_count"] == 2
    assert data["summary"]["total_booked_hours"] == 3
    assert data["summary"]["total_paid_amount"] == 47000
    assert data["summary"]["load_level"] == 0.0013
    assert data["summary"]["load_level_percent"] == 0.13
    assert data["bookings"][0]["paid_api"] == 25000
    assert data["bookings"][0]["total_paid_amount"] == 40000
    assert data["bookings"][1]["duration_hours"] == 1


def test_extract_data_aggregates_multiple_payments_and_receipt_strings(monkeypatch):
    client = _client()

    monkeypatch.setattr(
        "integrations.document_service.booking_repo.get_report_bookings_in_range",
        lambda *_: [
            {
                "id": 1,
                "customer_name": "Aruzhan",
                "field": 1,
                "date": "2026-08-03",
                "time_start": "10:00:00",
                "time_end": "11:30:00",
                "state": "CONFIRMED",
                "price_total": "30000",
                "paid_cash": "1000",
                "paid_kaspi_qr": "2000",
                "paid_avans": "3000",
                "source": None,
            },
            {
                "id": 2,
                "customer_name": "Cancelled",
                "field": 1,
                "date": "2026-08-03",
                "time_start": "12:00",
                "time_end": "13:00",
                "state": "cancelled",
                "price_total": "30000",
                "source": None,
            },
        ],
    )
    monkeypatch.setattr(
        "integrations.document_service.booking_repo.get_fields_info",
        lambda: [{"id": 1}],
    )
    monkeypatch.setattr(
        "integrations.document_service.booking_service.get_payments",
        lambda: [
            {
                "booking_id": 1,
                "amount": "4000.50",
                "receipt_date": "2026-08-03T10:00:00+05:00",
            },
            {
                "booking_id": 1,
                "amount": Decimal("500.50"),
                "receipt_date": datetime(2026, 8, 3, 11, 0),
            },
            {"booking_id": None, "amount": "999"},
        ],
    )

    response = client.post(
        "/api/manager/documents/extract-data",
        json={"bot_type": "arena", "start_date": "2026-08-01", "end_date": "2026-08-01"},
        headers=_HDR,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["summary"]["booking_count"] == 1
    assert data["summary"]["total_booked_hours"] == 1.5
    assert data["bookings"][0]["state"] == "CONFIRMED"
    assert data["bookings"][0]["source"] == ""
    assert data["bookings"][0]["paid_api"] == 4501
    assert data["bookings"][0]["total_paid_amount"] == 10501


def test_extract_data_returns_zero_summary_for_empty_period(monkeypatch):
    client = _client()

    monkeypatch.setattr(
        "integrations.document_service.booking_repo.get_report_bookings_in_range",
        lambda *_: [],
    )
    monkeypatch.setattr(
        "integrations.document_service.booking_repo.get_fields_info",
        lambda: [{"id": 1}, {"id": 2}, {"id": 3}],
    )
    monkeypatch.setattr(
        "integrations.document_service.booking_service.get_payments",
        lambda: [],
    )

    response = client.post(
        "/api/manager/documents/extract-data",
        json={"bot_type": "arena", "start_date": "2026-08-01", "end_date": "2026-08-31"},
        headers=_HDR,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["bookings"] == []
    assert data["summary"] == {
        "total_paid_amount": 0,
        "booking_count": 0,
        "total_booked_hours": 0,
        "load_level": 0,
        "load_level_percent": 0,
        "per_field_load": [],
    }
