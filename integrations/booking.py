"""Booking business logic — slot generation, free slots, context formatting."""

import logging
from datetime import date, datetime, time, timedelta

import config
from integrations import postgres
from utils import now_almaty, today_almaty

logger = logging.getLogger(__name__)

_WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _parse_time(t: str) -> time:
    return datetime.strptime(t, "%H:%M").time()


def _snap_up(t: time, step_minutes: int) -> time:
    """Round `t` up to the next multiple of `step_minutes` from midnight."""
    if step_minutes <= 0:
        return t
    total = t.hour * 60 + t.minute + (1 if t.second or t.microsecond else 0)
    snapped = ((total + step_minutes - 1) // step_minutes) * step_minutes
    snapped = min(snapped, 23 * 60 + 59)
    return time(snapped // 60, snapped % 60)


def get_week_range() -> tuple[date, date]:
    today = today_almaty()
    return today, today + timedelta(days=6)


# ---------------------------------------------------------------------------
# Slot generation
# ---------------------------------------------------------------------------

def generate_all_slots(week_start: date, week_end: date) -> list[dict]:
    """Generate every possible booking slot for the given date range."""
    open_time = _parse_time(config.BOOKING_OPEN_TIME)
    close_time = _parse_time(config.BOOKING_CLOSE_TIME)
    duration = timedelta(minutes=config.BOOKING_SLOT_DURATION)

    slots = []
    current_date = week_start
    while current_date <= week_end:
        current_dt = datetime.combine(current_date, open_time)
        close_dt = datetime.combine(current_date, close_time)
        while current_dt + duration <= close_dt:
            for field in config.BOOKING_FIELDS:
                slots.append({
                    "date": current_date,
                    "time_start": current_dt.time(),
                    "time_end": (current_dt + duration).time(),
                    "field": field["id"],
                    "format": field["format"],
                })
            current_dt += duration
        current_date += timedelta(days=1)
    return slots


def get_all_booked(week_start: date, week_end: date) -> list[dict]:
    """Booked slots from PostgreSQL (the single source of truth) for a date range."""
    return postgres.get_booked_slots(str(week_start), str(week_end))


def is_range_free(booked: list[dict], date_str: str, time_start: str, time_end: str, field_id: int) -> bool:
    """Return True if no existing booking overlaps [time_start, time_end) on the given field."""
    req_start = datetime.strptime(time_start, "%H:%M").time()
    req_end   = datetime.strptime(time_end,   "%H:%M").time()
    for b in booked:
        if str(b["date"]) != date_str or int(b["field"]) != int(field_id):
            continue
        b_start = datetime.strptime(str(b["time_start"])[:5], "%H:%M").time()
        b_end   = datetime.strptime(str(b["time_end"])[:5],   "%H:%M").time()
        if req_start < b_end and req_end > b_start:
            return False
    return True


def get_free_windows() -> list[dict]:
    """
    Compute exact contiguous free time windows for the next 7 days.

    Subtracts all booked ranges from [open_time, close_time] directly,
    so any arbitrary start/end (e.g. 10:20–11:45) is shown accurately.
    Each returned dict: {date, time_start (time), time_end (time), field, format}
    """
    week_start, week_end = get_week_range()
    booked   = get_all_booked(week_start, week_end)
    open_t   = _parse_time(config.BOOKING_OPEN_TIME)
    close_t  = _parse_time(config.BOOKING_CLOSE_TIME)
    today    = today_almaty()
    now_time = now_almaty().time()

    result = []
    current = week_start
    while current <= week_end:
        for field_conf in config.BOOKING_FIELDS:
            field_id = field_conf["id"]

            # Bookings for this day/field, sorted by start time
            day_booked = sorted(
                [
                    b for b in booked
                    if str(b["date"]) == str(current) and int(b["field"]) == field_id
                ],
                key=lambda b: datetime.strptime(str(b["time_start"])[:5], "%H:%M").time(),
            )

            # For today, don't show time that has already passed. Snap up to the
            # next slot boundary so the LLM/UI never offers a non-aligned start
            # (e.g. 10:47 → 11:00 when slot duration is 60 min).
            if current == today:
                floor = max(open_t, _snap_up(now_time, config.BOOKING_SLOT_DURATION))
            else:
                floor = open_t
            cursor = floor

            for b in day_booked:
                b_start = datetime.strptime(str(b["time_start"])[:5], "%H:%M").time()
                b_end   = datetime.strptime(str(b["time_end"])[:5],   "%H:%M").time()
                if b_start > cursor:
                    result.append({
                        "date":       current,
                        "time_start": cursor,
                        "time_end":   b_start,
                        "field":      field_id,
                        "format":     field_conf["format"],
                    })
                if b_end > cursor:
                    cursor = b_end

            if cursor < close_t:
                result.append({
                    "date":       current,
                    "time_start": cursor,
                    "time_end":   close_t,
                    "field":      field_id,
                    "format":     field_conf["format"],
                })

        current += timedelta(days=1)
    return result


# ---------------------------------------------------------------------------
# Context formatting for LLM injection
# ---------------------------------------------------------------------------

def format_availability_context(free_windows: list[dict]) -> str:
    if not free_windows:
        return "Свободных слотов на ближайшие 7 дней нет."

    by_date: dict[date, dict] = {}
    for w in free_windows:
        by_date.setdefault(w["date"], {}) \
               .setdefault((w["field"], w["format"]), []) \
               .append(w)

    lines = ["Свободные окна на ближайшие 7 дней:"]
    for d in sorted(by_date):
        day_label = f"{_WEEKDAY_RU[d.weekday()]} {d.strftime('%d.%m')}"
        field_lines = []
        for (field, fmt) in sorted(by_date[d]):
            windows = sorted(by_date[d][(field, fmt)], key=lambda w: w["time_start"])
            range_str = ", ".join(
                f"{w['time_start'].strftime('%H:%M')}–{w['time_end'].strftime('%H:%M')}"
                for w in windows
            )
            field_lines.append(f"    поле {field} ({fmt}): {range_str}")
        lines.append(f"  {day_label}:\n" + "\n".join(field_lines))
    return "\n".join(lines)


def format_user_booking_context(bookings: list[dict]) -> str:
    if not bookings:
        return "У этого пользователя нет предстоящих броней."

    status_map = {
        "awaiting_payment": "⏳ ожидает оплату",
        "confirmed": "✅ оплачено",
    }

    lines = ["Брони этого пользователя:"]
    for b in bookings:
        d = b["date"] if isinstance(b["date"], date) else \
            datetime.strptime(str(b["date"]), "%Y-%m-%d").date()
        ts = str(b["time_start"])[:5]
        te = str(b["time_end"])[:5]
        day_label = f"{_WEEKDAY_RU[d.weekday()]} {d.strftime('%d.%m')}"
        status_str = status_map.get(b.get("state", ""), b.get("state", ""))
        lines.append(
            f"  {day_label} {ts}–{te} | Поле {b['field']} ({b['format']}) | "
            f"{b.get('players', '?')} игр. | {status_str}"
        )
    return "\n".join(lines)


def find_free_field(booked: list[dict], date_str: str, time_start: str, time_end: str, format_: str) -> int | None:
    """Return a free field id for the given date/time-range/format, or None if all are taken."""
    for f in config.BOOKING_FIELDS:
        if f["format"] == format_ and is_range_free(booked, date_str, time_start, time_end, f["id"]):
            return f["id"]
    return None
