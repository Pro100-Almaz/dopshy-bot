"""Tests for the academy/boxing manager API blueprint."""

from flask import Flask

import config
from blueprints.manager_api import manager_api
from blueprints.manager_boxing_api import manager_boxing_api


_KEY = "test-key"
_HDR = {"X-API-Key": _KEY}


def _trial_row(**overrides):
    row = {
        "id": 10,
        "child_name": "Ali",
        "child_age": 8,
        "language": "ru",
        "phone": "77001234567",
        "group_id": 3,
        "trial_day": "2026-08-12",
        "start_time": "10:00",
        "end_time": "11:00",
        "state": "confirmed",
        "notes": "",
        "attended": False,
        "subscribed": False,
        "user_id": None,
    }
    row.update(overrides)
    return row


def _user_row(**overrides):
    row = {
        "id": 7,
        "child_name": "Ali",
        "child_age": 8,
        "child_birth_date": "2018-01-01",
        "parent_phone": "77001234567",
        "total_trials": 1,
        "assigned_group_id": 3,
        "subscribed": False,
    }
    row.update(overrides)
    return row


def _app():
    config.X_SERVICE_TOKEN = _KEY
    app = Flask(__name__)
    app.register_blueprint(manager_api)
    app.register_blueprint(manager_boxing_api)
    return app


def test_boxing_rejects_missing_key():
    client = _app().test_client()
    r = client.get("/api/manager/academy_groups")
    assert r.status_code == 401
    assert r.get_json()["code"] == "UNAUTHORIZED"


def test_boxing_accepts_bearer_token(monkeypatch):
    monkeypatch.setattr(
        "blueprints.manager_boxing_api.academy_repo.get_all_groups_for_frontend",
        lambda: [],
    )

    r = _app().test_client().get(
        "/api/manager/academy_groups",
        headers={"Authorization": f"Bearer {_KEY}"},
    )

    assert r.status_code == 200
    assert r.get_json()["ok"]


def test_boxing_rejects_bad_bearer_token(monkeypatch):
    monkeypatch.setattr(
        "blueprints.manager_boxing_api.academy_repo.get_all_groups_for_frontend",
        lambda: [],
    )

    r = _app().test_client().get(
        "/api/manager/academy_groups",
        headers={"Authorization": "Bearer wrong"},
    )

    assert r.status_code == 401
    assert r.get_json()["code"] == "UNAUTHORIZED"


def test_boxing_lists_groups(monkeypatch):
    monkeypatch.setattr(
        "blueprints.manager_boxing_api.academy_repo.get_all_groups_for_frontend",
        lambda: [{
            "id": 3,
            "group_name": "Kids",
            "group_type": "boxing",
            "max_cap": 12,
            "curr_cap": 4,
            "training_day": 0,
            "time_start": "10:00",
            "time_end": "11:00",
        }],
    )

    r = _app().test_client().get("/api/manager/academy_groups", headers=_HDR)

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"]
    assert data["data"]["groups"]["boxing"][0]["group_id"] == 3
    assert data["data"]["groups"]["boxing"][0]["training_day_label"] == "Понедельник"


def test_boxing_group_trials_not_found(monkeypatch):
    monkeypatch.setattr("blueprints.manager_boxing_api.academy_repo.get_group_by_id", lambda group_id: None)

    r = _app().test_client().get("/api/manager/academy_groups/99/trials", headers=_HDR)

    assert r.status_code == 404
    assert r.get_json() == {"ok": False, "code": "NOT_FOUND", "message": "Group not found."}


def test_boxing_patch_attended_validates_boolean():
    r = _app().test_client().patch(
        "/api/manager/academy_trials/10/attended",
        json={"attended": "yes"},
        headers=_HDR,
    )

    assert r.status_code == 400
    assert r.get_json()["code"] == "INVALID"


def test_boxing_patch_attended_updates(monkeypatch):
    monkeypatch.setattr("blueprints.manager_boxing_api.refresh_all_trials", lambda: None)
    monkeypatch.setattr(
        "blueprints.manager_boxing_api.academy_repo.update_trial_attended",
        lambda trial_id, attended: _trial_row(id=trial_id, attended=attended),
    )

    r = _app().test_client().patch(
        "/api/manager/academy_trials/10/attended",
        json={"attended": True},
        headers=_HDR,
    )

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"]
    assert data["data"]["trial_id"] == 10
    assert data["data"]["attended"] is True


def test_lists_football_trials(monkeypatch):
    seen = {}

    def fake_trials(group_type):
        seen["group_type"] = group_type
        return [_trial_row(id=21, child_name="Dias")]

    monkeypatch.setattr(
        "blueprints.manager_boxing_api.academy_repo.get_trials_with_users_by_type",
        fake_trials,
    )

    r = _app().test_client().get(
        "/api/manager/academy_trials?group_type=football",
        headers=_HDR,
    )

    assert r.status_code == 200
    data = r.get_json()
    assert seen["group_type"] == "football"
    assert data["ok"]
    assert data["data"]["trials"][0]["trial_id"] == 21
    assert data["data"]["trials"][0]["child_name"] == "Dias"


def test_lists_trials_rejects_unknown_group_type():
    r = _app().test_client().get(
        "/api/manager/academy_trials?group_type=tennis",
        headers=_HDR,
    )

    assert r.status_code == 400
    assert r.get_json()["code"] == "INVALID"


def test_get_trial_detail(monkeypatch):
    monkeypatch.setattr(
        "blueprints.manager_boxing_api.academy_repo.get_trial_with_user_by_id",
        lambda trial_id: _trial_row(id=trial_id, user_id=7, user_child_name="Ali"),
    )

    r = _app().test_client().get("/api/manager/academy_trials/10", headers=_HDR)

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"]
    assert data["data"]["trial_id"] == 10
    assert data["data"]["user"]["id"] == 7


def test_lists_football_users(monkeypatch):
    seen = {}

    def fake_users(group_type):
        seen["group_type"] = group_type
        return [_user_row(id=12, child_name="Dias")]

    monkeypatch.setattr(
        "blueprints.manager_boxing_api.academy_repo.get_users_by_type",
        fake_users,
    )

    r = _app().test_client().get(
        "/api/manager/academy_users?group_type=football",
        headers=_HDR,
    )

    assert r.status_code == 200
    data = r.get_json()
    assert seen["group_type"] == "football"
    assert data["ok"]
    assert data["data"]["users"][0]["id"] == 12
    assert data["data"]["users"][0]["name"] == "Dias"


def test_get_user_detail_with_trials(monkeypatch):
    monkeypatch.setattr(
        "blueprints.manager_boxing_api.academy_repo.get_user_by_id",
        lambda user_id: _user_row(id=user_id),
    )
    monkeypatch.setattr(
        "blueprints.manager_boxing_api.academy_repo.get_all_user_trials",
        lambda user_id: [_trial_row(id=31, child_name="Ali")],
    )

    r = _app().test_client().get("/api/manager/academy_users/7", headers=_HDR)

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"]
    assert data["data"]["user"]["id"] == 7
    assert data["data"]["trials"][0]["trial_id"] == 31


def test_boxing_patch_user_subscribed_updates(monkeypatch):
    monkeypatch.setattr("blueprints.manager_boxing_api.refresh_all_trials", lambda: None)
    monkeypatch.setattr(
        "blueprints.manager_boxing_api.academy_repo.update_user_subscribed",
        lambda user_id, subscribed: _user_row(id=user_id, subscribed=subscribed),
    )

    r = _app().test_client().patch(
        "/api/manager/academy_users/7/subscribed",
        json={"subscribed": True},
        headers=_HDR,
    )

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"]
    assert data["data"]["id"] == 7
    assert data["data"]["subscribed"] is True
