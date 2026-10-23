"""The adult group outside the signup flow: trial status texts, the manager
API and the Sheets rows all carry the trial's audience (children/adults)."""
from datetime import date, time

import pytest
from flask import Flask

import config
from blueprints.academy_api import academy_api, _group, _trial
from handlers import edit_trial
from integrations.sheets import trial_sheets

pytestmark = pytest.mark.no_db

_KEY = "test-key"
_HDR = {"X-API-Key": _KEY}

_ADULT_TRIAL = {
    "id": 9, "state": "confirmed", "audience": "adult", "child_name": "Айдос",
    "child_birth_year": None, "experience": None, "school_shift": None,
    "trial_day": date(2026, 10, 24), "start_time": time(12, 0), "end_time": time(13, 15),
    "language": "kk", "phone": "77000000000", "group_id": 30, "notes": None,
    "attended": False, "subscribed": False,
}
_CHILD_TRIAL = {**_ADULT_TRIAL, "audience": "child", "child_birth_year": 2015,
                "experience": "Beginner", "school_shift": "morning",
                "start_time": time(17, 0), "end_time": time(18, 0)}


@pytest.fixture()
def client():
    config.X_SERVICE_TOKEN = _KEY
    app = Flask(__name__)
    app.register_blueprint(academy_api)
    return app.test_client()


def test_adult_trial_status_has_no_child_fields_or_end_time(monkeypatch):
    monkeypatch.setattr(edit_trial, "_active_trials", lambda bot, phone: [dict(_ADULT_TRIAL)])

    reply = edit_trial.handle_trial_status_request("77000000000", "dopsy_boxing", "kk")

    assert "Ересектер тобы" in reply and "Сб 24.10.2026" in reply and "12:00" in reply
    assert "13:15" not in reply
    assert "Туған жылы" not in reply and "Деңгейі" not in reply and "ауысымы" not in reply


def test_child_trial_status_is_unchanged(monkeypatch):
    monkeypatch.setattr(edit_trial, "_active_trials", lambda bot, phone: [dict(_CHILD_TRIAL)])

    reply = edit_trial.handle_trial_status_request("77000000000", "dopsy_boxing", "ru")

    assert "17:00-18:00" in reply and "Год рождения: 2015" in reply
    assert "Взрослая группа" not in reply


def test_adult_draft_summary_asks_only_name_and_slot():
    draft = {**_ADULT_TRIAL, "state": "draft", "child_name": None,
             "trial_day": None, "start_time": None, "end_time": None}

    summary = edit_trial._draft_summary(draft, lang="ru")

    assert "имя" in summary and "время пробного занятия" in summary
    assert "год рождения" not in summary and "школьная смена" not in summary


def test_api_payloads_carry_audience():
    assert _trial(_ADULT_TRIAL)["audience"] == "adult"
    assert _trial({**_ADULT_TRIAL, "audience": None})["audience"] == "child"
    assert _group({"id": 30, "audience": "adult", "training_day": 1, "time_start": time(19, 15)})["audience"] == "adult"


def test_create_group_accepts_audience(monkeypatch, client):
    seen = {}
    monkeypatch.setattr("blueprints.academy_api.refresh_all_groups", lambda: None)
    monkeypatch.setattr("blueprints.academy_api.academy_repo.create_or_update_group",
                        lambda **kwargs: seen.update(kwargs) or 42)
    monkeypatch.setattr("blueprints.academy_api.academy_repo.replace_group_schedules",
                        lambda group_id, schedules: [])

    response = client.post("/api/boxing/groups", json={
        "group_name": "Box Adult", "max_cap": 25, "audience": "adult",
        "training_days": [{"training_day_value": 1, "start_time": "19:15", "end_time": "20:30"}],
    }, headers=_HDR)

    assert response.status_code == 201
    assert seen["audience"] == "adult"


def test_group_audience_is_validated(monkeypatch, client):
    monkeypatch.setattr("blueprints.academy_api.academy_repo.get_group_by_id",
                        lambda group_id: {"id": group_id, "group_type": "boxing"})

    created = client.post("/api/boxing/groups", json={
        "group_name": "X", "max_cap": 5, "audience": "teens",
        "training_days": [{"training_day_value": 1, "start_time": "19:15", "end_time": "20:30"}],
    }, headers=_HDR)
    patched = client.patch("/api/boxing/groups/30", json={"audience": "teens"}, headers=_HDR)

    assert created.status_code == 400 and patched.status_code == 400


def test_patch_group_sets_audience(monkeypatch, client):
    seen = {}
    monkeypatch.setattr("blueprints.academy_api.refresh_all_groups", lambda: None)
    monkeypatch.setattr("blueprints.academy_api.academy_repo.get_group_by_id",
                        lambda group_id: {"id": group_id, "group_type": "boxing"})
    monkeypatch.setattr("blueprints.academy_api.academy_repo.on_manual_group_edit",
                        lambda **kwargs: seen.update(kwargs) or {"ok": True, "group_id": 30})

    response = client.patch("/api/boxing/groups/30", json={"audience": "adult"}, headers=_HDR)

    assert response.status_code == 200
    assert seen["audience"] == "adult"


def test_sheet_rows_end_with_audience():
    trial_row = trial_sheets._trial_to_row(_ADULT_TRIAL)
    group_row = trial_sheets._group_to_row({
        "id": 30, "group_name": "Box Adult", "max_cap": 25, "audience": "adult",
        "training_day": 1, "time_start": time(19, 15), "time_end": time(20, 30),
    })

    assert trial_row[-1] == "ВЗРОСЛЫЕ" and len(trial_row) == len(trial_sheets._HEADERS["trials"])
    assert group_row[-1] == "ВЗРОСЛЫЕ" and len(group_row) == len(trial_sheets._HEADERS["groups"])
    assert trial_sheets._trial_to_row(_CHILD_TRIAL)[-1] == "ДЕТИ"
