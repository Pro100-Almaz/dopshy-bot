"""Booking business logic — slot generation, free slots, context formatting."""

import logging
from datetime import date, datetime, time, timedelta

from integrations.repo import academy_repo
from utils import today_almaty

logger = logging.getLogger(__name__)

_WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _parse_time(t: str) -> time:
    return datetime.strptime(t, "%H:%M").time()


def _get_closest_date(n: int):
    today = today_almaty()
    return today + timedelta(days=(n-today.weekday()+7)%7)


def get_trial_daytime(
    bot_name: str,
    days: list | None,
    school_shift: str | None = None,
) -> list[dict]:
    """
    Get available trial lessons for the next 7 days.
    Each returned dict: {date, time_start (time), time_end (time), group_id}
    """
    if days is None:
        days = [int(i) for i in range(7)]

    all_group_info = academy_repo.get_groups_info(bot_name=bot_name)  # method needed which returns [{group_id, training_day, time_start, time_end}]
    result = []
    for info in all_group_info:
        if info["training_day"] not in days:
            continue

        start = info["time_start"]
        end = info["time_end"]
        if school_shift == "morning" and start < time(12, 0):
            continue
        if school_shift == "afternoon" and end > time(12, 0):
            continue

        if info["training_day"] in days:
            result.append({
                "group_id": info["group_id"],
                "date": _get_closest_date(info["training_day"]),
                "time_start": start,
                "time_end": end,
            })
    return result


def get_eligible_trial_slots(
    bot_name: str,
    child_birth_year: int,
    school_shift: str,
) -> list[dict]:
    """Return schedule slots from groups eligible for the child.

    This is intentionally stricter than the generic availability context:
    date/time can only be selected after group eligibility is known.
    """
    result = []
    for info in academy_repo.get_groups_info(bot_name=bot_name):
        birth_years = info.get("birth_years") or []
        if birth_years and int(child_birth_year) not in [int(y) for y in birth_years]:
            continue

        max_cap = info.get("max_cap")
        curr_cap = info.get("curr_cap") or 0
        if max_cap is not None and int(curr_cap) >= int(max_cap):
            continue

        start = info["time_start"]
        end = info["time_end"]
        if school_shift == "morning" and start < time(12, 0):
            continue
        if school_shift == "afternoon" and end > time(12, 0):
            continue

        result.append({
            **info,
            "date": _get_closest_date(info["training_day"]),
        })
    return result


def match_preferred_slot(slots: list[dict], draft: dict) -> dict | None:
    preferred_date = draft.get("preferred_date")
    preferred_weekday = draft.get("preferred_weekday")
    preferred_start = draft.get("preferred_time_start")
    preferred_end = draft.get("preferred_time_end")

    for slot in slots:
        if preferred_date and str(slot["date"]) != str(preferred_date):
            continue
        if preferred_weekday is not None and int(slot["training_day"]) != int(preferred_weekday):
            continue
        if preferred_start and str(slot["time_start"])[:5] != str(preferred_start)[:5]:
            continue
        if preferred_end and str(slot["time_end"])[:5] != str(preferred_end)[:5]:
            continue
        return slot
    return None

def format_availability_context(free_windows: list[dict]) -> str:
    if not free_windows:
        return "Свободных слотов на ближайшие 7 дней нет."

    by_date: dict[date, list] = {}
    for w in free_windows:
        by_date.setdefault(w["date"], []).append(w)

    lines = ["Пробные занятия на ближайшие 7 дней:"]
    for d in sorted(by_date):
        day_label = f"{_WEEKDAY_RU[d.weekday()]} {d.strftime('%d.%m')}"
        field_lines = set()
        for window in sorted(by_date[d], key=lambda x: x["time_start"]):
            range_str = ", ".join(
                f"{window['time_start'].strftime('%H:%M')}–{window['time_end'].strftime('%H:%M')}"
            )
            field_lines.add(f"{range_str}")
        lines.append(f"  {day_label}:\n" + "\n".join(field_lines))
    return "\n".join(lines)
