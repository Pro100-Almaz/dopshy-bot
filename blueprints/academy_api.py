"""Sport-scoped academy API consumed by the frontend gateway."""

from datetime import date, datetime
from decimal import Decimal
import uuid

from flask import Blueprint, jsonify, request

from blueprints.manager_api import _authenticate
from integrations.repo import academy_repo
from integrations.sheets.trial_sheets import refresh_all_trials


academy_api = Blueprint("academy_api", __name__)
academy_api.before_request(_authenticate)


def _ok(data):
    return jsonify({"ok": True, "data": data}), 200


def _invalid(message: str):
    return jsonify({"ok": False, "code": "INVALID", "message": message}), 400


def _not_found(message: str):
    return jsonify({"ok": False, "code": "NOT_FOUND", "message": message}), 404


def _serialize_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return str(value)[:5]
    return value


def _serialize(row: dict) -> dict:
    return {key: _serialize_value(value) for key, value in row.items()}


def _sport_arg(sport: str) -> tuple[str | None, tuple | None]:
    if sport not in {"football", "boxing"}:
        return None, _invalid("sport must be football or boxing.")
    return sport, None


def _subscribed_filter(rows: list[dict]) -> list[dict]:
    raw = request.args.get("subscribed")
    if raw in (None, ""):
        return rows
    value = raw.strip().lower()
    if value not in {"true", "false", "1", "0"}:
        return rows
    expected = value in {"true", "1"}
    return [row for row in rows if bool(row.get("subscribed")) is expected]


def _shift_label(value: str | None) -> str | None:
    if value == "morning":
        return "Таңертең"
    if value == "afternoon":
        return "Түстен кейін"
    return value


def _school_time(row: dict) -> str | None:
    start = row.get("preferred_time_start")
    end = row.get("preferred_time_end")
    if start and end:
        return f"{str(start)[:5]}-{str(end)[:5]}"
    return None


def _group(row: dict) -> dict:
    return _serialize({
        "group_id": row.get("id"),
        "group_name": row.get("group_name"),
        "group_type": row.get("group_type"),
        "max_cap": row.get("max_cap"),
        "curr_cap": row.get("curr_cap", 0),
        "birth_years": row.get("birth_years") or [],
        "location": row.get("location"),
        "level": row.get("level") or [],
        "trainer": row.get("trainer"),
        "field": row.get("field"),
        "training_day": row.get("training_day"),
        "start_time": row.get("time_start"),
        "end_time": row.get("time_end"),
        "age_min": row.get("age_min"),
        "age_max": row.get("age_max"),
        "shift": row.get("shift"),
        "is_active": row.get("is_active"),
    })


def _trial(row: dict) -> dict:
    return _serialize({
        "trial_id": row.get("id"),
        "child_name": row.get("child_name"),
        "child_birth_year": row.get("child_birth_year"),
        "parent_phone": row.get("phone"),
        "phone": row.get("phone"),
        "group_id": row.get("group_id"),
        "trial_day": row.get("trial_day"),
        "trial_date": row.get("trial_day"),
        "start_time": row.get("start_time"),
        "end_time": row.get("end_time"),
        "state": row.get("state"),
        "notes": row.get("notes"),
        "attended": row.get("attended"),
        "attendance_state": row.get("attendance_state") or ("attended" if row.get("attended") else "pending"),
        "subscribed": row.get("subscribed"),
        "created_at": row.get("created_at"),
        "shift": _shift_label(row.get("school_shift")),
        "school_time": _school_time(row),
    })


def _student(row: dict) -> dict:
    return _serialize({
        "student_id": row.get("id"),
        "id": row.get("id"),
        "child_name": row.get("child_name"),
        "name": row.get("child_name"),
        "child_birth_year": row.get("child_birth_year"),
        "birth_year": row.get("child_birth_year"),
        "parent_phone": row.get("parent_phone"),
        "total_trials": row.get("total_trials"),
        "assigned_group_id": row.get("assigned_group_id"),
        "subscribed": row.get("subscribed"),
    })


@academy_api.get("/api/<string:sport>/groups")
def list_groups(sport: str):
    group_type, error = _sport_arg(sport)
    if error:
        return error
    rows = academy_repo.get_groups_by_type_for_frontend(group_type)
    return _ok({"groups": [_group(row) for row in rows]})


@academy_api.get("/api/<string:sport>/trials")
def list_trials(sport: str):
    group_type, error = _sport_arg(sport)
    if error:
        return error
    rows = academy_repo.get_trials_with_users_by_type(group_type)
    return _ok({"trials": [_trial(row) for row in _subscribed_filter(rows)]})


@academy_api.get("/api/<string:sport>/students")
def list_students(sport: str):
    group_type, error = _sport_arg(sport)
    if error:
        return error
    rows = academy_repo.get_users_by_type(group_type)
    return _ok({"students": [_student(row) for row in _subscribed_filter(rows)]})


@academy_api.patch("/api/<string:sport>/trials/<int:trial_id>/attended")
def patch_trial_attended(sport: str, trial_id: int):
    group_type, error = _sport_arg(sport)
    if error:
        return error
    if not academy_repo.trial_belongs_to_type(trial_id, group_type):
        return _not_found("Trial not found.")

    body = request.get_json(silent=True) or {}
    if "attendance_state" in body:
        state = body.get("attendance_state")
        if state not in {"pending", "attended", "missed"}:
            return _invalid("attendance_state must be pending, attended, or missed.")
        trial = academy_repo.update_trial_attendance_state(trial_id, state)
    elif isinstance(body.get("attended"), bool):
        trial = academy_repo.update_trial_attended(trial_id, body["attended"])
    else:
        return _invalid("attended boolean or attendance_state is required.")

    if not trial:
        return _not_found("Trial not found.")
    refresh_all_trials()
    return _ok({"trial": _trial(trial)})


@academy_api.patch("/api/<string:sport>/trials/<int:trial_id>/subscribed")
def patch_trial_subscribed(sport: str, trial_id: int):
    group_type, error = _sport_arg(sport)
    if error:
        return error
    if not academy_repo.trial_belongs_to_type(trial_id, group_type):
        return _not_found("Trial not found.")
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("subscribed"), bool):
        return _invalid("subscribed must be a boolean.")
    trial = academy_repo.update_trial_subscribed(trial_id, body["subscribed"])
    if not trial:
        return _not_found("Trial not found.")
    refresh_all_trials()
    return _ok({"trial": _trial(trial)})


@academy_api.patch("/api/<string:sport>/students/<int:student_id>/subscribed")
def patch_student_subscribed(sport: str, student_id: int):
    group_type, error = _sport_arg(sport)
    if error:
        return error
    if not academy_repo.user_belongs_to_type(student_id, group_type):
        return _not_found("Student not found.")
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("subscribed"), bool):
        return _invalid("subscribed must be a boolean.")
    student = academy_repo.update_user_subscribed(student_id, body["subscribed"])
    if not student:
        return _not_found("Student not found.")
    refresh_all_trials()
    return _ok({"student": _student(student)})


@academy_api.get("/api/<string:sport>/payments")
def list_payments(sport: str):
    group_type, error = _sport_arg(sport)
    if error:
        return error
    return _ok({"payments": [_serialize(row) for row in academy_repo.list_academy_payments(group_type)]})


@academy_api.patch("/api/<string:sport>/payments/<int:payment_id>/confirmed")
def patch_payment_confirmed(sport: str, payment_id: int):
    group_type, error = _sport_arg(sport)
    if error:
        return error
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("confirmed"), bool):
        return _invalid("confirmed must be a boolean.")
    payment = academy_repo.update_academy_payment_confirmed(payment_id, group_type, body["confirmed"])
    if not payment:
        return _not_found("Payment not found.")
    return _ok({"payment": _serialize(payment)})


@academy_api.patch("/api/<string:sport>/payments/<int:payment_id>")
def patch_payment(sport: str, payment_id: int):
    group_type, error = _sport_arg(sport)
    if error:
        return error
    body = request.get_json(silent=True) or {}
    patch = {key: body[key] for key in ("due_date", "is_active", "notes", "check_status") if key in body}
    if "is_active" in patch and not isinstance(patch["is_active"], bool):
        return _invalid("is_active must be a boolean.")
    payment = academy_repo.update_academy_payment(payment_id, group_type, **patch)
    if not payment:
        return _not_found("Payment not found.")
    return _ok({"payment": _serialize(payment)})


@academy_api.get("/api/<string:sport>/bot-content")
def get_bot_content(sport: str):
    group_type, error = _sport_arg(sport)
    if error:
        return error
    return _ok(academy_repo.get_bot_content(group_type))


@academy_api.put("/api/<string:sport>/bot-content")
def put_bot_content(sport: str):
    group_type, error = _sport_arg(sport)
    if error:
        return error
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return _invalid("body must be an object.")
    return _ok(academy_repo.save_bot_content(group_type, body))
