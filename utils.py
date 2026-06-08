"""Shared utility helpers for the Dopshy bot."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo


import config


def now_almaty() -> datetime:
    """Return the current tz-aware datetime in config.BOOKING_TIMEZONE."""
    return datetime.now(tz=ZoneInfo(config.BOOKING_TIMEZONE))


def today_almaty() -> date:
    """Return the current date in config.BOOKING_TIMEZONE."""
    return now_almaty().date()


def fmt_date(weekday_dict: dict, date_str: str, lang: str = "ru") -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return f"{weekday_dict[lang][d.weekday()]} {d.strftime('%d.%m.%Y')}"
    except (ValueError, TypeError):
        return date_str or "?"


def pad_time(t: str) -> str:
    """Ensure HH:MM format (zero-pad single-digit hours)."""
    h, m = t.split(":")
    return f"{int(h):02d}:{m}"


def fmt_time(value: str | datetime) -> str | time:
    if isinstance(value, str):
        return datetime.strptime(value, "%H:%M").time()
    return datetime.strftime(value, "%H:%M")
