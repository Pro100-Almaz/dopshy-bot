"""Shared utility helpers for the Dopshy bot."""

from datetime import date, datetime
from zoneinfo import ZoneInfo


import config


def now_almaty() -> datetime:
    """Return the current tz-aware datetime in config.BOOKING_TIMEZONE."""
    return datetime.now(tz=ZoneInfo(config.BOOKING_TIMEZONE))


def today_almaty() -> date:
    """Return the current date in config.BOOKING_TIMEZONE."""
    return now_almaty().date()


def is_past_booking_time(date_str: str, time_start_str: str | None = None) -> bool:
    """True if the booking date (+ optional start time) has already passed in BOOKING_TIMEZONE."""
    now = now_almaty()
    try:
        booking_date = date.fromisoformat(str(date_str))
    except (ValueError, TypeError):
        return False
    if booking_date < now.date():
        return True
    if booking_date == now.date() and time_start_str:
        try:
            ts = datetime.strptime(str(time_start_str)[:5], "%H:%M").time()
        except (ValueError, TypeError):
            return False
        if ts <= now.time():
            return True
    return False
