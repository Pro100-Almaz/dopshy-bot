"""Validation helpers for academy user/student API payloads."""

from __future__ import annotations


USER_WRITABLE_FIELDS = {
    "child_name",
    "child_birth_year",
    "parent_phone",
    "total_trials",
    "assigned_group_id",
    "subscribed",
    "experience",
    "school_shift",
}

_ALIASES = {
    "name": "child_name",
    "birth_year": "child_birth_year",
}


def normalize_user_payload(body: dict, *, creating: bool) -> tuple[dict | None, str | None]:
    if not isinstance(body, dict):
        return None, "body must be an object."

    patch = {}
    for key, value in body.items():
        field = _ALIASES.get(key, key)
        if field in USER_WRITABLE_FIELDS:
            patch[field] = value

    if creating:
        if not str(patch.get("child_name") or "").strip():
            return None, "name is required."
    elif not patch:
        return None, "No fields to update."

    if "child_name" in patch:
        name = str(patch["child_name"]).strip()
        if not name:
            return None, "name cannot be empty."
        if len(name) > 40:
            return None, "name must be at most 40 characters."
        patch["child_name"] = name

    if "parent_phone" in patch:
        phone = patch["parent_phone"]
        patch["parent_phone"] = str(phone).strip() if phone not in (None, "") else None
        if patch["parent_phone"] and len(patch["parent_phone"]) > 15:
            return None, "parent_phone must be at most 15 characters."

    if "child_birth_year" in patch:
        year = patch["child_birth_year"]
        if year in (None, ""):
            patch["child_birth_year"] = None
        else:
            try:
                year = int(year)
            except (TypeError, ValueError):
                return None, "birth_year must be an integer."
            if year < 1900 or year > 2100:
                return None, "birth_year must be between 1900 and 2100."
            patch["child_birth_year"] = year

    if "assigned_group_id" in patch:
        group_id = patch["assigned_group_id"]
        if group_id in (None, ""):
            patch["assigned_group_id"] = None
        else:
            try:
                patch["assigned_group_id"] = int(group_id)
            except (TypeError, ValueError):
                return None, "assigned_group_id must be an integer."

    if "total_trials" in patch:
        try:
            total = int(patch["total_trials"])
        except (TypeError, ValueError):
            return None, "total_trials must be an integer."
        if total < 0:
            return None, "total_trials cannot be negative."
        patch["total_trials"] = total

    if "subscribed" in patch and not isinstance(patch["subscribed"], bool):
        return None, "subscribed must be a boolean."

    if "experience" in patch:
        value = patch["experience"]
        if value in (None, ""):
            patch["experience"] = None
        elif value not in {"Beginner", "Intermediate", "Advanced"}:
            return None, "experience must be Beginner, Intermediate, or Advanced."

    if "school_shift" in patch:
        value = patch["school_shift"]
        if value in (None, ""):
            patch["school_shift"] = None
        elif value not in {"morning", "afternoon"}:
            return None, "school_shift must be morning or afternoon."

    return patch, None
