"""
Client self-service edit handler — invoked when the LLM calls the
`edit_booking` tool. Looks up the user's editable bookings, picks the most
likely target (soonest in-window), calls booking_service.client_edit_booking,
and formats a bilingual reply.

All policy (48h window, once-only, slot clash) lives in the service layer;
this module is glue: target selection + message formatting + Sheets sync.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone

from chat.conversation import clear_history
from integrations import booking_service, sheets
from integrations.repo import postgres, academy_repo
from integrations.repo.academy_repo import get_all_active_trials, cancel_all_trials

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bilingual message catalogue
# ---------------------------------------------------------------------------

# Each entry is (ru, kk). Curly placeholders are filled at format time.
_EDIT_REJECT_MESSAGES: dict[str, tuple[str, str]] = {
    "NO_TRIAL": (
        "У вас нет активной записи на пробный урок, которую можно изменить.",
        "Сізде өзгертуге болатын белсенді сынақ сабағы жоқ.",
    ),
    "ALREADY_EDITED": (
        "❌ Эту бронь уже один раз меняли. Следующее изменение — через администратора.",
        "❌ Бұл бронь бір рет өзгертілген. Келесі өзгерісті әкімші арқылы жасаңыз.",
    ),
    "SLOT_TAKEN": (
        "❌ Это время уже занято. Выберите другое время или поле.",
        "❌ Бұл уақыт алынған. Басқа уақыт немесе алаң таңдаңыз.",
    ),
    "NO_CHANGE": (
        "Я не понял, что именно изменить. Напишите новое время, дату, "
        "поле или количество игроков.",
        "Нақты не өзгерту керек екенін түсінбедім. Жаңа уақытты, күнді, "
        "алаңды немесе ойыншы санын жазыңыз.",
    ),
    "INVALID_STATE": (
        "❌ Эту бронь уже нельзя изменить.",
        "❌ Бұл бронды енді өзгертуге болмайды.",
    ),
    "NOT_FOUND": (
        "❌ Бронь не найдена.",
        "❌ Брон табылмады.",
    ),
}

_REJECT_FALLBACK = (
    "❌ Не удалось изменить бронь. Свяжитесь с администратором.",
    "❌ Бронды өзгерту мүмкін болмады. Әкімшімен хабарласыңыз.",
)

_CANCEL_MESSAGES = {
    "SUCCESS": (
        "Ваша запись на пробное занятие было успешно отменено.",
        "Жазылым сәтті өшірілді."
    ),
    "NOT_FOUND": (
        "Записи не найдены на ваш номер",
        "Сіздің нөміріңізге сабаққа жазылым табылмады"
    )
}


def _bilingual(ru: str, kk: str) -> str:
    return f"{ru}\n\n— — —\n\n{kk}"


def _format_reject(code: str) -> str:
    ru, kk = _EDIT_REJECT_MESSAGES.get(code, _REJECT_FALLBACK)
    return _bilingual(ru, kk)


def _format_cancel(code: str) -> str:
    ru, kk = _CANCEL_MESSAGES.get(code)
    return _bilingual(ru, kk)


def _format_success(result_data: dict) -> str:
    """Render the diff so the user sees exactly what changed."""
    print("result_data:", result_data)
    # Build a per-field "old → new" line only for fields that actually changed.

    # Current booking card (resulting state)
    ts = str(result_data["start_time"])[:5]
    te = str(result_data["end_time"])[:5]

    summary_ru = (
        f"✅ Запись обновлена!\n\n"
        f"📅 {result_data['trial_day']}\n"
        f"⏰ {ts}–{te}\n"
        f"👤 Имя: {result_data.get('child_name', '')}\n"
        f"🎂 Возраст: {result_data.get('child_age', '')}"
    )
    summary_kk = (
        f"✅ Жазылым жаңартылды!\n\n"
        f"📅 {result_data['trial_day']}\n"
        f"⏰ {ts}–{te}\n"
        f"👤 Аты: {result_data.get('child_name', '')}\n"
        f"🎂 Жасы: {result_data.get('child_age', '')}\n\n"
    )
    return _bilingual(summary_ru, summary_kk)


# ---------------------------------------------------------------------------
# Target selection
# ---------------------------------------------------------------------------

def _pick_target(trials: list[dict]) -> dict | None:
    """Pick the most likely booking the user wants to edit.

    Strategy: soonest booking that's still in the edit window and hasn't been
    edited before. Falls back to the soonest overall so the service layer
    surfaces a specific error (EDIT_WINDOW_CLOSED / ALREADY_EDITED) instead
    of a generic NO_BOOKING.
    """
    if not trials:
        return None
    cutoff = datetime.now(timezone.utc) + timedelta(hours=48)
    eligible = [
        t for t in trials
        if t.get("start_at") and t["start_at"] > cutoff
        and t.get("predecessor_booking_id") is None
    ]
    return eligible[0] if eligible else trials[0]


# ---------------------------------------------------------------------------
# Sheets sync (background)
# ---------------------------------------------------------------------------

def _sync_sheets(old_booking_id: int, new_booking: dict, phone: str) -> None:
    """Push both the cancelled old row and the new row to Google Sheets."""
    old_row = postgres.get_booking(old_booking_id) or {}
    rows = [
        {
            "id":            old_booking_id,
            "field":         old_row.get("field"),
            "date":          str(old_row.get("date") or ""),
            "time_start":    str(old_row.get("time_start") or "")[:5],
            "time_end":      str(old_row.get("time_end") or "")[:5],
            "customer_name": old_row.get("customer_name", ""),
            "phone":         phone,
            "players":       None,
            "state":         "cancelled",
            "notes":         old_row.get("notes", ""),
        },
        {
            "id":            new_booking["id"],
            "field":         new_booking["field"],
            "date":          str(new_booking["date"]),
            "time_start":    str(new_booking["time_start"])[:5],
            "time_end":      str(new_booking["time_end"])[:5],
            "customer_name": new_booking.get("customer_name", ""),
            "phone":         phone,
            "players":       new_booking.get("players"),
            "state":         new_booking["state"],
            "notes":         "",
        },
    ]

    def _run():
        for r in rows:
            try:
                sheets.upsert_booking_row(r)
            except Exception as exc:  # noqa: BLE001
                logger.error("[EDIT] Sheets sync failed for booking %s: %s", r["id"], exc)

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def handle_edit_request(chat_id: str, sender_phone: str, diff: dict, bot_name: str) -> str:
    """Process a single edit_booking tool-call payload from the LLM."""
    diff = {k: v for k, v in (diff or {}).items() if v not in (None, "")}
    logger.info("[EDIT_TRIAL] chat_id=%s phone=%s diff=%s", chat_id, sender_phone, diff)

    target = None
    all_trials = academy_repo.get_all_active_trials(sender_phone, bot_name)
    for trial in all_trials:
        if trial["state"] == "confirmed":
            target = trial
            break

    if target is None:
        logger.info("[EDIT_TRIAL] No editable trial for %s — rejecting", sender_phone)
        return _format_reject("NO_TRIAL")

    if not diff:
        logger.info("[EDIT_TRIAL] trial_id=%d — empty diff, asking user", target["id"])
        return _format_reject("NO_CHANGE")

    result = postgres.update_draft(bot_name, target["id"], 'confirmed', **diff)
    if not result["ok"]:
        logger.info(
            "[EDIT_TRIAL] trial_id=%d rejected code=%s msg=%s",
            target["id"], result["code"], result["message"],
        )
        return _format_reject(result["code"])

    logger.info(
        "[EDIT_TRIAL] trial_id=%d → new id=%d successful", target["id"], result["data"]["object_id"]
    )
    trial = academy_repo.get_trial(result["data"]["object_id"])
    # _sync_sheets(target["id"], result["data"]["new_booking"], sender_phone)
    return _format_success(trial)


def handle_cancel_trial_request(chat_id: str, sender_phone: str, bot_name: str) -> str:
    trials = get_all_active_trials(sender_phone, bot_name)

    trial_ids = [t["id"] for t in trials]
    clear_history(chat_id)
    postgres.delete_session(bot_name, chat_id)

    if len(trial_ids) == 0:
        return _format_cancel("NOT_FOUND")
    cancel_all_trials(trial_ids)



    logger.info("[CANCEL_TRIAL] successful",)
    return _format_cancel("SUCCESS")




