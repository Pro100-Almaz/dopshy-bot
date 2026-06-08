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
