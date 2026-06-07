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

from integrations import booking_service, sheets
from integrations.repo import postgres
from integrations.sheets.booking_sheets import upsert_booking_row

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bilingual message catalogue
# ---------------------------------------------------------------------------

# Each entry is (ru, kk). Curly placeholders are filled at format time.
_EDIT_REJECT_MESSAGES: dict[str, tuple[str, str]] = {
    "NO_TRIAL": (
        "У вас нет активной записи на пробный урок, которое можно изменить.",
        "Сізде өзгертуге болатын белсенді жазылым жоқ.",
    ),
    "ALREADY_EDITED": (
        "❌ Эту бронь уже один раз меняли. Следующее изменение — через администратора.",
        "❌ Бұл бронь бір рет өзгертілген. Келесі өзгерісті әкімші арқылы жасаңыз.",
    ),
    "NO_CHANGE": (
        "Я не понял, что именно изменить. Напишите новое время, дату, ",
        "Нақты не өзгерту керек екенін түсінбедім. Жаңа уақытты, күнді, "
    ),
    "INVALID_STATE": (
        "❌ Эту бронь уже нельзя изменить.",
        "❌ Бұл бронды енді өзгертуге болмайды.",
    ),
    "NOT_FOUND": (
        "❌ Пробное занятие не найдено.",
        "❌ Жахылым табылмады.",
    ),
}

_REJECT_FALLBACK = (
    "❌ Не удалось изменить бронь. Свяжитесь с администратором.",
    "❌ Бронды өзгерту мүмкін болмады. Әкімшімен хабарласыңыз.",
)


def _bilingual(ru: str, kk: str) -> str:
    return f"{ru}\n\n— — —\n\n{kk}"


def _format_reject(code: str) -> str:
    ru, kk = _EDIT_REJECT_MESSAGES.get(code, _REJECT_FALLBACK)
    return _bilingual(ru, kk)


def _format_success(result_data: dict) -> str:
    """Render the diff so the user sees exactly what changed."""
    src = result_data["from"]
    dst = result_data["to"]
    new = result_data["new_booking"]

    # Build a per-field "old → new" line only for fields that actually changed.
    field_labels_ru = {
        "date":          "Дата",
        "time_start":    "Время начала",
        "time_end":      "Время окончания",
        "field":         "Поле",
        "players":       "Игроков",
        "customer_name": "Имя",
    }
    field_labels_kk = {
        "date":          "Күні",
        "time_start":    "Басталу уақыты",
        "time_end":      "Аяқталу уақыты",
        "field":         "Алаң",
        "players":       "Ойыншылар",
        "customer_name": "Аты",
    }
    diff_lines_ru: list[str] = []
    diff_lines_kk: list[str] = []
    for k in field_labels_ru:
        if src.get(k) != dst.get(k):
            diff_lines_ru.append(f"  • {field_labels_ru[k]}: {src.get(k)} → {dst.get(k)}")
            diff_lines_kk.append(f"  • {field_labels_kk[k]}: {src.get(k)} → {dst.get(k)}")

    # Current booking card (resulting state)
    ts = str(new["time_start"])[:5]
    te = str(new["time_end"])[:5]
    summary_ru = (
        f"✅ Бронь обновлена!\n\n"
        f"📅 {new['date']}\n"
        f"⏰ {ts}–{te}\n"
        f"⚽ Поле {new['field']} ({new['format']})\n"
        f"👥 Игроков: {new.get('players', '?')}\n"
        f"👤 Имя: {new.get('customer_name', '')}\n\n"
        f"Что изменилось:\n" + "\n".join(diff_lines_ru)
    )
    summary_kk = (
        f"✅ Брон жаңартылды!\n\n"
        f"📅 {new['date']}\n"
        f"⏰ {ts}–{te}\n"
        f"⚽ Алаң {new['field']} ({new['format']})\n"
        f"👥 Ойыншылар: {new.get('players', '?')}\n"
        f"👤 Аты: {new.get('customer_name', '')}\n\n"
        f"Не өзгерді:\n" + "\n".join(diff_lines_kk)
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
                upsert_booking_row(r)
            except Exception as exc:  # noqa: BLE001
                logger.error("[EDIT] Sheets sync failed for booking %s: %s", r["id"], exc)

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def handle_edit_request(chat_id: str, sender_phone: str, diff: dict, bot_name: str) -> str:
    # return _format_reject("NO_TRIAL")
    """Process a single edit_booking tool-call payload from the LLM."""
    diff = {k: v for k, v in (diff or {}).items() if v not in (None, "")}
    logger.info("[EDIT_TRIAL] chat_id=%s phone=%s diff=%s", chat_id, sender_phone, diff)

    target = postgres.get_user_editable_booking(sender_phone, bot_name)
    if target is None:
        logger.info("[EDIT_TRIAL] No editable trial for %s — rejecting", sender_phone)
        return _format_reject("NO_TRIAL")

    if not diff:
        logger.info("[EDIT_TRIAL] trial_id=%d — empty diff, asking user", target["id"])
        return _format_reject("NO_CHANGE")

    result = booking_service.client_edit_booking(
        target["id"], actor_id=chat_id, **diff
    )
    if not result["ok"]:
        logger.info(
            "[EDIT_TRIAL] trial_id=%d rejected code=%s msg=%s",
            target["id"], result["code"], result["message"],
        )
        return _format_reject(result["code"])

    logger.info(
        "[EDIT_TRIAL] trial_id=%d → new id=%d successful", target["id"], result["data"]["trial_id"]
    )
    _sync_sheets(target["id"], result["data"]["new_booking"], sender_phone)
    return _format_success(result["data"])


