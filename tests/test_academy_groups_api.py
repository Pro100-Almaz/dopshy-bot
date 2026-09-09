"""Tests for sport-scoped academy group routes."""

import pytest
from flask import Flask

import config
from blueprints.manager_api import manager_api
from blueprints.academy_api import academy_api


_KEY = "test-key"
_HDR = {"X-API-Key": _KEY}


@pytest.fixture()
def client():
    config.X_SERVICE_TOKEN = _KEY
    app = Flask(__name__)
    app.register_blueprint(academy_api)
    return app.test_client()


@pytest.fixture()
def manager_client():
    config.X_SERVICE_TOKEN = _KEY
    app = Flask(__name__)
    app.register_blueprint(manager_api)
    return app.test_client()


@pytest.mark.no_db
def test_create_group_uses_sport_and_training_days(monkeypatch, client):
    seen = {}
    monkeypatch.setattr("blueprints.academy_api.refresh_all_groups", lambda: None)

    def fake_create_or_update_group(**kwargs):
        seen["group"] = kwargs
        return 42

    def fake_replace_group_schedules(group_id, schedules):
        seen["group_id"] = group_id
        seen["schedules"] = schedules
        return [{"id": 5, "group_id": group_id, **schedules[0]}]

    monkeypatch.setattr("blueprints.academy_api.academy_repo.create_or_update_group", fake_create_or_update_group)
    monkeypatch.setattr("blueprints.academy_api.academy_repo.replace_group_schedules", fake_replace_group_schedules)

    response = client.post(
        "/api/football/groups",
        json={
            "group_name": "Футбол 7-9",
            "max_cap": 12,
            "training_days": [{
                "training_day": "Понедельник",
                "training_day_value": 0,
                "start_time": "09:30",
                "end_time": "10:30",
            }],
            "age_min": 7,
            "age_max": 9,
            "shift": "morning",
            "is_active": True,
        },
        headers=_HDR,
    )

    assert response.status_code == 201
    assert response.get_json()["data"] == {"group_id": 42}
    assert seen["group"]["group_type"] == "football"
    assert seen["group"]["age_min"] == 7
    assert seen["schedules"] == [{
        "training_day": 0,
        "time_start": "09:30",
        "time_end": "10:30",
        "field": None,
    }]


@pytest.mark.no_db
def test_patch_group_replaces_training_days(monkeypatch, client):
    seen = {}
    monkeypatch.setattr("blueprints.academy_api.refresh_all_groups", lambda: None)
    monkeypatch.setattr(
        "blueprints.academy_api.academy_repo.get_group_by_id",
        lambda group_id: {"id": group_id, "group_type": "boxing"},
    )
    monkeypatch.setattr(
        "blueprints.academy_api.academy_repo.on_manual_group_edit",
        lambda **kwargs: {"ok": True, "group_id": kwargs["group_id"]},
    )

    def fake_replace(group_id, schedules):
        seen["group_id"] = group_id
        seen["schedules"] = schedules
        return [{"id": 9, "group_id": group_id, **schedules[0]}]

    monkeypatch.setattr("blueprints.academy_api.academy_repo.replace_group_schedules", fake_replace)

    response = client.patch(
        "/api/boxing/groups/3",
        json={
            "group_name": "Boxing kids",
            "training_days": [{
                "training_day_value": 4,
                "start_time": "16:00",
                "end_time": "17:00",
            }],
        },
        headers=_HDR,
    )

    assert response.status_code == 200
    assert response.get_json()["data"] == {"group_id": 3}
    assert seen["group_id"] == 3
    assert seen["schedules"][0]["training_day"] == 4


@pytest.mark.no_db
def test_delete_group_soft_deletes(monkeypatch, client):
    monkeypatch.setattr("blueprints.academy_api.refresh_all_groups", lambda: None)
    monkeypatch.setattr(
        "blueprints.academy_api.academy_repo.get_group_by_id",
        lambda group_id: {"id": group_id, "group_type": "football"},
    )
    monkeypatch.setattr(
        "blueprints.academy_api.academy_repo.deactivate_group_repo",
        lambda group_id: {"ok": True, "group_id": group_id},
    )

    response = client.delete("/api/football/groups/7", headers=_HDR)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


@pytest.mark.no_db
def test_assign_student_to_group_returns_group_fields(monkeypatch, client):
    monkeypatch.setattr("blueprints.academy_api.refresh_all_trials", lambda: None)
    monkeypatch.setattr(
        "blueprints.academy_api.academy_repo.assign_user_to_group",
        lambda student_id, group_id, group_type: {
            "id": student_id,
            "child_name": "Ali",
            "child_birth_year": 2018,
            "parent_phone": "77001234567",
            "total_trials": 1,
            "assigned_group_id": group_id,
            "assigned_group_name": "Футбол 7-9",
            "assigned_group_type": group_type,
            "subscribed": True,
        },
    )

    response = client.post(
        "/api/football/groups/7/students",
        json={"student_id": "123"},
        headers=_HDR,
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


@pytest.mark.no_db
def test_students_read_returns_assigned_group(monkeypatch, client):
    monkeypatch.setattr(
        "blueprints.academy_api.academy_repo.get_users_by_type",
        lambda group_type: [{
            "id": 1,
            "child_name": "Ali",
            "child_birth_year": 2018,
            "parent_phone": "77001234567",
            "total_trials": 1,
            "assigned_group_id": 7,
            "assigned_group_name": "Футбол 7-9",
            "assigned_group_type": group_type,
            "subscribed": True,
        }],
    )

    response = client.get("/api/football/students", headers=_HDR)

    assert response.status_code == 200
    student = response.get_json()["data"]["students"][0]
    assert student["assigned_group_name"] == "Футбол 7-9"
    assert student["assigned_group"] == "Футбол 7-9"


@pytest.mark.no_db
def test_groups_read_returns_frontend_day_fields(monkeypatch, client):
    monkeypatch.setattr(
        "blueprints.academy_api.academy_repo.get_groups_by_type_for_frontend",
        lambda group_type: [{
            "id": 45,
            "schedule_id": 88,
            "group_name": "Футбол 7-9",
            "group_type": group_type,
            "max_cap": 12,
            "curr_cap": 8,
            "training_day": 0,
            "time_start": "09:30",
            "time_end": "10:30",
            "age_min": 7,
            "age_max": 9,
            "shift": "morning",
            "is_active": True,
        }],
    )

    response = client.get("/api/football/groups", headers=_HDR)

    assert response.status_code == 200
    group = response.get_json()["data"]["groups"][0]
    assert group["id"] == 88
    assert group["group_id"] == 45
    assert group["training_day"] == "Понедельник"
    assert group["training_day_value"] == 0
    assert group["training_day_label"] == "Понедельник"


@pytest.mark.no_db
def test_manager_create_group_returns_minimal_response(monkeypatch, manager_client):
    monkeypatch.setattr("blueprints.manager_api.refresh_all_groups", lambda: None)
    monkeypatch.setattr("blueprints.manager_api.create_or_update_group", lambda **kwargs: 123)
    monkeypatch.setattr("blueprints.manager_api.setting_training_time", lambda *args: 99)

    response = manager_client.post(
        "/api/manager/academy_groups",
        json={
            "group_name": "Футбол 7-9",
            "group_type": "football",
            "max_cap": 12,
            "training_days": [{
                "training_day_value": 0,
                "start_time": "09:30",
                "end_time": "10:30",
            }],
            "is_active": True,
        },
        headers=_HDR,
    )

    assert response.status_code == 201
    assert response.get_json() == {"ok": True, "data": {"group_id": 123}}


@pytest.mark.no_db
def test_manager_patch_group_returns_minimal_response(monkeypatch, manager_client):
    monkeypatch.setattr("blueprints.manager_api.refresh_all_groups", lambda: None)
    monkeypatch.setattr(
        "blueprints.manager_api.on_manual_group_edit",
        lambda **kwargs: {"ok": True, "group_id": kwargs["group_id"]},
    )
    monkeypatch.setattr(
        "blueprints.manager_api.academy_repo.replace_group_schedules",
        lambda group_id, schedules: [{"id": 1, "group_id": group_id, **schedules[0]}],
    )

    response = manager_client.patch(
        "/api/manager/academy_groups/123",
        json={
            "max_cap": 14,
            "training_days": [{
                "training_day_value": 4,
                "start_time": "16:00",
                "end_time": "17:00",
            }],
        },
        headers=_HDR,
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "data": {"group_id": 123}}


@pytest.mark.no_db
def test_manager_delete_group_returns_ok(monkeypatch, manager_client):
    monkeypatch.setattr("blueprints.manager_api.refresh_all_groups", lambda: None)
    monkeypatch.setattr(
        "blueprints.manager_api.deactivate_group_repo",
        lambda group_id: {"ok": True, "group_id": group_id},
    )

    response = manager_client.delete("/api/manager/academy_groups/123", headers=_HDR)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


@pytest.mark.no_db
def test_manager_assign_student_returns_ok(monkeypatch, manager_client):
    monkeypatch.setattr("blueprints.manager_api.refresh_all_trials", lambda: None)
    monkeypatch.setattr(
        "blueprints.manager_api.academy_repo.assign_user_to_group",
        lambda student_id, group_id: {"id": student_id, "assigned_group_id": group_id},
    )

    response = manager_client.post(
        "/api/manager/academy_groups/45/students",
        json={"student_id": "123"},
        headers=_HDR,
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
