"""Booking business logic — slot generation, free slots, context formatting."""

import logging
from datetime import date, datetime, time, timedelta


import config
from integrations.repo import postgres, academy_repo
from utils import now_almaty, today_almaty

logger = logging.getLogger(__name__)

_WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _parse_time(t: str) -> time:
    return datetime.strptime(t, "%H:%M").time()


def _get_closest_date(n: int):
    today = today_almaty()
    return today + timedelta(days=(n-today.weekday()+7)%7)


def get_trial_daytime(bot_name: str, days: list | None ) -> list[dict]:
    """
    Get available trial lessons for the next 7 days.
    Each returned dict: {date, time_start (time), time_end (time), group_id}
    """
    if days is None:
        days = [int(i) for i in range(7)]

    all_group_info = academy_repo.get_group_info(bot_name=bot_name)  # method needed which returns [{group_id, training_day, time_start, time_end}]
    result = []
    for group_id, day, time_start, time_end in all_group_info:
        if day in days:
            result.append({
                "group_id": group_id,
                "date": _get_closest_date(day),
                "time_start": _parse_time(time_start),
                "time_end": _parse_time(time_end),
            })
    return result

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
        for g in sorted(by_date[d]):
            windows = sorted(by_date[d], key=lambda w: w["time_start"])
            range_str = ", ".join(
                f"{w['time_start'].strftime('%H:%M')}–{w['time_end'].strftime('%H:%M')}"
                for w in windows
            )
            field_lines.add(f"{range_str}")
        lines.append(f"  {day_label}:\n" + "\n".join(field_lines))
    return "\n".join(lines)
