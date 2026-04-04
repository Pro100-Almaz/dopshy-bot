"""Booking business logic — slot generation, free slots, context formatting."""

import logging
from datetime import date, datetime, time, timedelta

import config
from integrations import postgres

logger = logging.getLogger(__name__)

_WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _parse_time(t: str) -> time:
    return datetime.strptime(t, "%H:%M").time()


def get_week_range() -> tuple[date, date]:
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    return week_start, week_start + timedelta(days=6)


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


def get_free_slots() -> list[dict]:
    """Return free future slots for the current week."""
    week_start, week_end = get_week_range()
    all_slots = generate_all_slots(week_start, week_end)
    booked = postgres.get_booked_slots(str(week_start), str(week_end))

    booked_keys = {
        (str(b["date"]), str(b["time_start"])[:5], int(b["field"]))
        for b in booked
    }

    today = date.today()
    now_time = datetime.now().time()

    free = []
    for s in all_slots:
        # Skip past slots
        if s["date"] < today:
            continue
        if s["date"] == today and s["time_start"] <= now_time:
            continue
        key = (str(s["date"]), s["time_start"].strftime("%H:%M"), s["field"])
        if key not in booked_keys:
            free.append(s)
    return free


# ---------------------------------------------------------------------------
# Context formatting for LLM injection
# ---------------------------------------------------------------------------

def format_availability_context(free_slots: list[dict]) -> str:
    if not free_slots:
        return "Свободных слотов на этой неделе больше нет."

    by_date: dict[date, list] = {}
    for s in free_slots:
        by_date.setdefault(s["date"], []).append(s)

    lines = ["Свободные слоты на текущую неделю:"]
    for d in sorted(by_date):
        day_label = f"{_WEEKDAY_RU[d.weekday()]} {d.strftime('%d.%m')}"
        slot_strs = [
            f"{s['time_start'].strftime('%H:%M')}–{s['time_end'].strftime('%H:%M')} "
            f"(поле {s['field']}, {s['format']})"
            for s in sorted(by_date[d], key=lambda x: (x["time_start"], x["field"]))
        ]
        lines.append(f"  {day_label}: {', '.join(slot_strs)}")
    return "\n".join(lines)


def format_user_booking_context(bookings: list[dict]) -> str:
    if not bookings:
        return "У этого пользователя нет предстоящих броней."

    status_map = {
        "awaiting_payment": "⏳ ожидает оплату",
        "paid": "✅ оплачено",
        "completed": "✅ завершено",
    }

    lines = ["Брони этого пользователя:"]
    for b in bookings:
        d = b["date"] if isinstance(b["date"], date) else \
            datetime.strptime(str(b["date"]), "%Y-%m-%d").date()
        ts = str(b["time_start"])[:5]
        te = str(b["time_end"])[:5]
        day_label = f"{_WEEKDAY_RU[d.weekday()]} {d.strftime('%d.%m')}"
        status_str = status_map.get(b.get("status", ""), b.get("status", ""))
        lines.append(
            f"  {day_label} {ts}–{te} | Поле {b['field']} ({b['format']}) | "
            f"{b.get('players', '?')} игр. | {status_str}"
        )
    return "\n".join(lines)


def find_free_field(free_slots: list[dict], date_str: str, time_str: str, format_: str) -> int | None:
    """Return a free field id for the given date/time/format, or None if none available."""
    for s in free_slots:
        if (
            str(s["date"]) == date_str
            and s["time_start"].strftime("%H:%M") == time_str
            and s["format"] == format_
        ):
            return s["field"]
    return None
