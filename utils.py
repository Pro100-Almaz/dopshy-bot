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


_END_OF_DAY = ("23:59", "24:00")


def normalize_end_time(time_start: str, time_end: str) -> str:
    """Normalize a midnight end-time to end-of-day.

    A booking ending at 00:00 means "until the end of the day", not a
    day-crossing (transitive) range, so it should be stored as a single
    booking. Return 23:59 in that case. A true zero-duration 00:00-00:00
    range (start is also midnight) is left untouched so the caller can
    reject it as invalid.
    """
    ts, te = str(time_start)[:5], str(time_end)[:5]
    if te == "00:00" and ts != "00:00":
        return "23:59"
    return te


def display_end_time(time_end) -> str:
    """Show an end-of-day end time to users as 00:00.

    A booking that runs to the end of the day is stored as 23:59 (or the
    transitive first-half boundary 23:59:59, which floors to 24:00). Display
    that as 00:00, which reads as midnight on the sheet and in WhatsApp.
    """
    te = str(time_end)[:5]
    return "00:00" if te in _END_OF_DAY else te


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
