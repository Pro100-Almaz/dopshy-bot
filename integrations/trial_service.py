"""Service layer for academy trial mutations.

Handlers decide conversation flow; this module owns database state changes and
side effects for trial drafts/confirmation.
"""

import logging
import uuid

from chat.conversation import clear_history
from integrations.repo import academy_repo, postgres
from integrations.repo.utils import _conn
from integrations.sheets.trial_sheets import refresh_all_trials, upsert_trial_row

logger = logging.getLogger(__name__)


def _ok(data: dict | None = None, message: str = "") -> dict:
    return {"ok": True, "code": "OK", "data": data or {}, "message": message}


def _err(code: str, message: str, data: dict | None = None) -> dict:
    return {"ok": False, "code": code, "data": data or {}, "message": message}


def create_or_get_draft(bot_name: str, chat_id: str, phone: str, lang: str) -> dict:
    draft = academy_repo.get_existing_trial_draft(phone, bot_name)
    if draft:
        return _ok({"trial": draft})

    # The trial limit is deliberately NOT checked here. A draft is created on the
    # first message the intent router classifies as a signup, and that router is
    # tuned to over-trigger, so gating draft creation meant a client who merely
    # asked about prices got "you have used up your trial lessons" instead of an
    # answer. The limit is enforced at confirm_trial() — the only point where a
    # trial lesson is actually taken.
    if academy_repo.has_active_trial(bot_name, phone):
        return _err("HAS_ACTIVE_TRIAL", "Active trial already exists.")

    result = postgres.create_draft(
        bot_name,
        chat_id=chat_id,
        phone=phone,
        client_token=str(uuid.uuid4()),
        language=lang,
    )
    if not result.get("ok"):
        return result

    trial = academy_repo.get_trial(result["data"]["trial_id"])
    if not trial:
        return _err("NOT_FOUND", "Draft trial was not found after creation.")
    return _ok({"trial": trial})


def update_intake(bot_name: str, trial_id: int, fields: dict) -> dict:
    result = postgres.update_draft(bot_name, trial_id, **fields)
    if not result.get("ok"):
        return result
    trial = academy_repo.get_trial(trial_id)
    if not trial:
        return _err("NOT_FOUND", "Trial not found.")
    return _ok({"trial": trial})


def assign_slot(bot_name: str, chat_id: str, trial_id: int, slot: dict, lang: str) -> dict:
    result = postgres.update_draft(
        bot_name,
        trial_id,
        trial_day=str(slot["date"]),
        start_time=str(slot["time_start"])[:5],
        end_time=str(slot["time_end"])[:5],
        group_id=slot["group_id"],
    )
    if not result.get("ok"):
        return result

    trial = academy_repo.get_trial(trial_id)
    if not trial:
        return _err("NOT_FOUND", "Trial not found.")

    postgres.upsert_session(
        bot_name,
        chat_id,
        "trial_confirm",
        {"trial_id": trial_id, "lang": lang},
        trial_id,
    )
    return _ok({"trial": trial})


def confirm_trial(bot_name: str, chat_id: str, trial_id: int) -> dict:
    trial = academy_repo.get_trial(trial_id)
    if not trial:
        return _err("NOT_FOUND", "Trial not found.")
    if trial.get("state") != "draft":
        return _err("TRIAL_WRONG_STATE", "Trial cannot be confirmed from this state.")
    if not all(trial.get(k) for k in ("group_id", "trial_day", "start_time", "end_time")):
        return _err("INVALID_SLOT", "Trial slot is incomplete.")

    # Order matters: a client who already holds a booking must hear that, not
    # that they are out of trials.
    phone = trial.get("phone")
    if phone:
        if academy_repo.has_active_trial(bot_name, phone):
            return _err("HAS_ACTIVE_TRIAL", "Active trial already exists.")
        if not academy_repo.check_trial_limits(bot_name, phone):
            return _err("LIMIT_REACHED", "Trial limit reached.")

    if not academy_repo.confirm_trial(trial_id):
        return _err("NOT_FOUND", "Trial not found.")

    confirmed = academy_repo.get_trial_with_user_by_id(trial_id)
    if confirmed:
        upsert_trial_row(confirmed)
    postgres.delete_session(bot_name, chat_id)
    clear_history(chat_id)

    trial = academy_repo.get_trial(trial_id)
    return _ok({"trial": trial})


def cancel_trial(bot_name: str, chat_id: str, trial_id: int, reason: str) -> dict:
    result = postgres.cancel_booking_trial(
        bot_name,
        trial_id,
        actor_type="chatbot:Бот",
        actor_id=chat_id,
        reason=reason,
    )
    postgres.delete_session(bot_name, chat_id)
    clear_history(chat_id)
    refresh_all_trials()
    return result


def get_active_trials(bot_name: str, phone: str) -> dict:
    return _ok({"trials": academy_repo.get_all_active_trials(phone, bot_name) or []})


def update_confirmed_trial(bot_name: str, trial_id: int, fields: dict) -> dict:
    result = postgres.update_draft(bot_name, trial_id, state="confirmed", **fields)
    if not result.get("ok"):
        return result
    trial = academy_repo.get_trial(trial_id)
    if not trial:
        return _err("NOT_FOUND", "Trial not found.")
    refresh_all_trials()
    return _ok({"trial": trial})


def replace_confirmed_trial_with_draft(
    bot_name: str,
    chat_id: str,
    trial_id: int,
    fields: dict,
    lang: str,
) -> dict:
    """Cancel a confirmed trial and create a new draft with merged intake data."""
    existing = academy_repo.get_trial(trial_id)
    if not existing:
        return _err("NOT_FOUND", "Trial not found.")
    if existing.get("state") != "confirmed":
        return _err("TRIAL_WRONG_STATE", "Trial cannot be replaced from this state.")

    allowed = {
        "child_name", "child_birth_year", "experience", "school_shift",
        "preferred_date", "preferred_weekday", "preferred_time_start",
        "preferred_time_end",
    }
    base = {
        key: existing.get(key)
        for key in allowed
        if existing.get(key) not in (None, "")
    }
    patch = {key: value for key, value in fields.items() if key in allowed and value not in (None, "")}
    base.update(patch)

    cancelled = postgres.cancel_booking_trial(
        bot_name,
        trial_id,
        actor_type="chatbot:Бот",
        actor_id=chat_id,
        reason="confirmed_trial_replaced_by_user_edit",
    )
    if not cancelled.get("ok") or not cancelled.get("data", {}).get("cancelled"):
        return _err("CANCEL_FAILED", cancelled.get("message") or "Could not cancel confirmed trial.")

    result = postgres.create_draft(
        bot_name,
        chat_id=chat_id,
        phone=existing.get("phone"),
        client_token=str(uuid.uuid4()),
        language=lang,
        **base,
    )
    if not result.get("ok"):
        return result

    trial = academy_repo.get_trial(result["data"]["trial_id"])
    if not trial:
        return _err("NOT_FOUND", "Replacement draft was not found after creation.")

    clear_history(chat_id)
    refresh_all_trials()
    return _ok({"trial": trial, "cancelled_trial_id": trial_id})


def reopen_confirmed_trial_for_reassignment(bot_name: str, trial_id: int, fields: dict) -> dict:
    allowed = {
        "child_name", "child_birth_year", "experience", "school_shift",
        "preferred_date", "preferred_weekday", "preferred_time_start",
        "preferred_time_end",
    }
    patch = {k: v for k, v in fields.items() if k in allowed}
    set_clause = ["state = 'draft'", "trial_day = NULL", "start_time = NULL", "end_time = NULL", "group_id = NULL", "updated_at = NOW()"]
    values = []
    for key, value in patch.items():
        set_clause.append(f"{key} = %s")
        values.append(value)
    values.append(trial_id)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE academy_trials SET {', '.join(set_clause)} "
                "WHERE id = %s AND state = 'confirmed' RETURNING id",
                values,
            )
            if not cur.fetchone():
                return _err("TRIAL_WRONG_STATE", "Trial cannot be reopened from this state.")
    trial = academy_repo.get_trial(trial_id)
    if not trial:
        return _err("NOT_FOUND", "Trial not found.")
    refresh_all_trials()
    return _ok({"trial": trial})
