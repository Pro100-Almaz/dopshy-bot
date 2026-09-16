"""Booking business logic — slot generation, free slots, context formatting."""

import logging
from datetime import date, datetime, time, timedelta

from integrations.repo import academy_repo
from utils import today_almaty

logger = logging.getLogger(__name__)

_WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_WEEKDAY_KK = ["Дс", "Сс", "Ср", "Бс", "Жм", "Сб", "Жс"]

_T = {
    "my_trials":  {"ru": "📋 Ваши записи:", "kk": "📋 Сіздің жазылымдарыңыз:"},
    "no_trials":  {"ru": "У вас нет активных записей на пробное занятие.",
                   "kk": "Сізде белсенді сынақ сабағына жазылым жоқ."},
    "draft":      {"ru": "не завершена", "kk": "аяқталмаған"},
    "confirmed":  {"ru": "подтверждена", "kk": "расталған"},
}


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

    all_group_info = academy_repo.get_groups_info(bot_name=bot_name)
    result = []
    for info in all_group_info:
        if info["training_day"] in days:
            result.append({
                "group_id": info["group_id"],
                "group_name": info.get("group_name"),
                "date": _get_closest_date(info["training_day"]),
                "time_start": info["time_start"],
                "time_end": info["time_end"],
                "max_cap": info.get("max_cap"),
                "curr_cap": info.get("curr_cap"),
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
        for window in sorted(by_date[d], key=lambda x: x["time_start"]):
            range_str = ", ".join(
                f"{window['time_start'].strftime('%H:%M')}–{window['time_end'].strftime('%H:%M')}"
            )
            field_lines.add(f"{range_str}")
        lines.append(f"  {day_label}:\n" + "\n".join(field_lines))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Class lookup for the LLM trial flow
#
# The arena flow searches a free-form grid (date x time_start x time_end x
# field). An academy has no such grid: a parent picks one of a handful of
# existing weekly classes. So a single primitive — filter the week's classes by
# whatever the parent has named so far — covers every rule the flow needs.
# ---------------------------------------------------------------------------

def fmt_hhmm(t) -> str:
    """Render a DB time (or an already-formatted string) as HH:MM."""
    return t.strftime("%H:%M") if hasattr(t, "strftime") else str(t)[:5]


def _norm_hhmm(value) -> str | None:
    """Normalise user/extractor time input to HH:MM, or None if unparseable."""
    if not value:
        return None
    try:
        return datetime.strptime(fmt_hhmm(value), "%H:%M").strftime("%H:%M")
    except (ValueError, TypeError):
        return None


def find_classes(bot_name: str, date_str: str | None = None,
                 time_start: str | None = None) -> list[dict]:
    """Classes in the next 7 days matching whatever is known so far.

    Both filters optional, which is what lets one function serve every rule:
      date only        -> that day's classes
      time only        -> the days that have a class starting then
      date + time      -> the candidates for a concrete booking (0, 1 or many)

    A date beyond the 7-day horizon simply matches nothing: get_trial_daytime
    projects each weekly slot onto its next occurrence only.
    """
    day = None
    if date_str:
        try:
            day = date.fromisoformat(str(date_str))
        except (ValueError, TypeError):
            return []

    classes = get_trial_daytime(bot_name, [day.weekday()] if day else None)

    if day:
        classes = [c for c in classes if c["date"] == day]

    wanted = _norm_hhmm(time_start)
    if wanted:
        classes = [c for c in classes if fmt_hhmm(c["time_start"]) == wanted]

    return sorted(classes, key=lambda c: (c["date"], c["time_start"]))


def has_capacity(cls: dict) -> bool:
    """False only when the class is provably full; unknown caps stay bookable."""
    max_cap, curr_cap = cls.get("max_cap"), cls.get("curr_cap")
    if max_cap is None or curr_cap is None:
        return True
    return int(curr_cap) < int(max_cap)


def format_user_trials(trials: list[dict], lang: str = "ru") -> str:
    """Render a parent's own trial registrations.

    The academy counterpart of booking.format_user_booking_context: the LLM
    cannot look these up itself, so trial_status is answered from the DB rather
    than left to the RAG pipeline, where the persona would invent an answer.
    """
    if not trials:
        return _T["no_trials"][lang]

    weekdays = _WEEKDAY_RU if lang == "ru" else _WEEKDAY_KK
    lines = [_T["my_trials"][lang]]

    for t in sorted(trials, key=lambda x: (x["trial_day"] is None, x["trial_day"])):
        day = t.get("trial_day")
        if day is None:
            continue
        d = day if isinstance(day, date) else datetime.strptime(str(day), "%Y-%m-%d").date()
        ts = str(t.get("start_time") or "")[:5]
        te = str(t.get("end_time") or "")[:5]
        state = _T.get(t.get("state", ""), {}).get(lang, t.get("state", ""))
        child = t.get("child_name") or "?"
        lines.append(
            f"  {weekdays[d.weekday()]} {d.strftime('%d.%m')} {ts}–{te} | {child} | {state}"
        )

    return "\n".join(lines) if len(lines) > 1 else _T["no_trials"][lang]
