"""Manager endpoints for academy/boxing frontend views."""

from flask import Blueprint, jsonify, request

from blueprints.manager_api import _authenticate, _serialize
from integrations.repo import academy_repo
from integrations.sheets.trial_sheets import WEEKDAY_RU, _HEADERS, _STATES_RUSSIAN, refresh_all_trials


manager_boxing_api = Blueprint("manager_boxing_api", __name__)
manager_boxing_api.before_request(_authenticate)


def _ok(data):
    return jsonify({"ok": True, "data": data}), 200


def _not_found(message: str):
    return jsonify({"ok": False, "code": "NOT_FOUND", "message": message}), 404


def _invalid(message: str):
    return jsonify({"ok": False, "code": "INVALID", "message": message}), 400


def _sheet_group(row: dict) -> dict:
    training_day = row.get("training_day")
    return {
        "group_id": row.get("id"),
        "group_name": row.get("group_name"),
        "group_type": row.get("group_type"),
        "max_cap": row.get("max_cap"),
        "curr_cap": row.get("curr_cap", 0),
        "training_day": training_day,
        "training_day_label": WEEKDAY_RU.get(training_day, ""),
        "start_time": row.get("time_start"),
        "end_time": row.get("time_end"),
    }


def _trial_user(row: dict) -> dict | None:
    if row.get("user_id") is None:
        return None
    return {
        "id": row.get("user_id"),
        "name": row.get("user_child_name"),
        "age": row.get("user_child_age"),
        "birthdate": row.get("user_child_birth_date"),
        "parent_phone": row.get("user_parent_phone"),
        "total_trials": row.get("user_total_trials"),
        "assigned_group_id": row.get("user_assigned_group_id"),
        "subscribed": row.get("user_subscribed"),
    }


def _sheet_trial(row: dict) -> dict:
    return {
        "trial_id": row.get("id"),
        "child_name": row.get("child_name"),
        "child_age": row.get("child_age"),
        "language": row.get("language"),
        "phone": row.get("phone"),
        "group_id": row.get("group_id"),
        "trial_day": row.get("trial_day"),
        "start_time": row.get("start_time"),
        "end_time": row.get("end_time"),
        "state": row.get("state"),
        "state_label": _STATES_RUSSIAN.get(row.get("state"), row.get("state")),
        "notes": row.get("notes"),
        "attended": row.get("attended"),
        "subscribed": row.get("subscribed"),
        "user": _trial_user(row),
    }


def _academy_user(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "name": row.get("child_name"),
        "age": row.get("child_age"),
        "birthdate": row.get("child_birth_date"),
        "parent_phone": row.get("parent_phone"),
        "total_trials": row.get("total_trials"),
        "assigned_group_id": row.get("assigned_group_id"),
        "subscribed": row.get("subscribed"),
    }


def _required_bool(field: str) -> tuple[bool | None, tuple | None]:
    body = request.get_json(silent=True) or {}
    value = body.get(field)
    if not isinstance(value, bool):
        return None, _invalid(f"{field} must be a boolean.")
    return value, None


@manager_boxing_api.get("/api/manager/academy_groups")
def list_academy_groups():
    rows = academy_repo.get_all_groups_for_frontend()
    groups_by_type: dict[str, list[dict]] = {}
    for row in rows:
        item = _sheet_group(row)
        groups_by_type.setdefault(row.get("group_type") or "", []).append(item)

    return _ok({
            "headers": _HEADERS["groups"],
            "groups": {key: [_serialize(item) for item in value] for key, value in groups_by_type.items()},
    })


@manager_boxing_api.get("/api/manager/academy_groups/<int:group_id>/trials")
def get_group_trials(group_id: int):
    group = academy_repo.get_group_by_id(group_id)
    if not group:
        return _not_found("Group not found.")

    users = academy_repo.get_users_by_assigned_group(group_id)
    trials = academy_repo.get_trials_with_users_by_group(group_id)

    return _ok({
            "group": _serialize(group),
            "user_fields": [
                "name",
                "age",
                "birthdate",
                "parent_phone",
                "total_trials",
                "assigned_group_id",
                "subscribed",
            ],
            "trial_headers": _HEADERS["trials"],
            "users": [_serialize(_academy_user(user)) for user in users],
            "trials": [_serialize(_sheet_trial(trial)) for trial in trials],
    })


@manager_boxing_api.patch("/api/manager/academy_trials/<int:trial_id>/attended")
def patch_trial_attended(trial_id: int):
    attended, error = _required_bool("attended")
    if error:
        return error

    trial = academy_repo.update_trial_attended(trial_id, attended)
    if not trial:
        return _not_found("Trial not found.")

    refresh_all_trials()
    return _ok(_serialize(_sheet_trial(trial)))


@manager_boxing_api.patch("/api/manager/academy_trials/<int:trial_id>/subscribed")
def patch_trial_subscribed(trial_id: int):
    subscribed, error = _required_bool("subscribed")
    if error:
        return error

    trial = academy_repo.update_trial_subscribed(trial_id, subscribed)
    if not trial:
        return _not_found("Trial not found.")

    refresh_all_trials()
    return _ok(_serialize(_sheet_trial(trial)))


@manager_boxing_api.patch("/api/manager/academy_users/<int:user_id>/subscribed")
def patch_user_subscribed(user_id: int):
    subscribed, error = _required_bool("subscribed")
    if error:
        return error

    user = academy_repo.update_user_subscribed(user_id, subscribed)
    if not user:
        return _not_found("User not found.")

    refresh_all_trials()
    return _ok(_serialize(_academy_user(user)))
