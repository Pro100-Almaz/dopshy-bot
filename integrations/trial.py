"""Booking business logic — slot generation, free slots, context formatting."""

import logging
from datetime import date, datetime, time, timedelta

from integrations.repo import academy_repo
from utils import closest_weekday_date, is_past_booking_time, parse_weekdays, today_almaty

logger = logging.getLogger(__name__)

_WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_WEEKDAY_KK = ["Дс", "Сс", "Ср", "Бс", "Жм", "Сб", "Жс"]
_LEVEL_LABELS_RU = {"Beginner": "Начальный", "Intermediate": "Средний", "Advanced": "Продвинутый"}
_LEVEL_LABELS_KK = {"Beginner": "Бастапқы", "Intermediate": "Орта", "Advanced": "Жоғары"}
# Weekdays (Mon=0) a bot never offers for a trial, though its groups train then.
_NO_TRIAL_WEEKDAYS = {"dopsy_fs_school": {4, 5}}  # Friday, Saturday


def _trial_groups(bot_name: str, audience: str) -> list[dict]:
    """Groups a trial can be booked into: get_groups_info minus no-trial weekdays."""
    blocked = _NO_TRIAL_WEEKDAYS.get(bot_name, set())
    return [
        info for info in academy_repo.get_groups_info(bot_name=bot_name, audience=audience)
        if int(info["training_day"]) not in blocked
    ]

def _closed_dates(bot_name: str) -> dict:
    """{date: note} of upcoming days without training. Fails open: without
    the table (migration not applied yet) every day stays open."""
    try:
        return academy_repo.get_closed_dates(bot_name)
    except Exception as err:
        logger.warning("[TRIAL] Closed dates unavailable for %s: %s", bot_name, err)
        return {}


def _open_date(d: date, closed: dict) -> date:
    """`d`, or the same weekday of the first week the academy trains then."""
    while d in closed:
        d += timedelta(days=7)
    return d


def _is_ignored_day(info: dict) -> bool:
    """True when the group takes no trials on this schedule row's weekday."""
    return info["training_day"] in (info.get("ignore_days") or [])


def _parse_time(t: str) -> time:
    return datetime.strptime(t, "%H:%M").time()


def _get_closest_date(n: int):
    return closest_weekday_date(n)


def _next_class_date(training_day: int, time_start):
    """Closest date of a weekly class that has not started yet.

    A class later today is still offered; once its start time has passed, the
    next occurrence is a week later.
    """
    d = closest_weekday_date(training_day)
    if is_past_booking_time(d.isoformat(), str(time_start)[:5]):
        d += timedelta(days=7)
    return d


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

    # Children's classes only: the result feeds the child signup and the
    # "children's schedule" context given to the LLM.
    all_group_info = _trial_groups(bot_name, "child")
    closed = _closed_dates(bot_name)
    result = []
    for info in all_group_info:
        if info["training_day"] not in days or _is_ignored_day(info):
            continue

        start = info["time_start"]
        end = info["time_end"]
        if school_shift == "morning" and start < time(12, 0):
            continue
        if school_shift == "afternoon" and end > time(12, 0):
            continue
        # A kindergarten child ("kindergarten") has no school hours to avoid.

        if info["training_day"] in days:
            result.append({
                "group_id": info["group_id"],
                "group_name": info.get("group_name"),
                "date": _open_date(_get_closest_date(info["training_day"]), closed),
                "time_start": start,
                "time_end": end,
            })
    return result


def list_classes_by_weekday(bot_name: str, weekdays: list[int]) -> list[dict]:
    """Every class landing on any of `weekdays` in the next 7 days.

    For a "what trains on Wednesday and Thursday" lookup — a factual
    schedule question, not a booking eligibility check — so unlike
    get_eligible_trial_slots this ignores age/experience/shift/capacity and
    keeps level/trainer so the reply can name them.
    """
    closed = _closed_dates(bot_name)
    result = []
    for info in academy_repo.get_groups_info(bot_name=bot_name):
        if info["training_day"] not in weekdays:
            continue
        d = _get_closest_date(info["training_day"])
        # A holiday stays in the answer, marked: the client asked about that day.
        result.append({**info, "date": d, "closed": d in closed, "closed_note": closed.get(d)})
    return sorted(result, key=lambda s: (s["date"], s["time_start"]))


def get_eligible_trial_slots(
    bot_name: str,
    child_birth_year: int,
    school_shift: str,
    experience: str | None = None,
) -> list[dict]:
    """Return schedule slots from groups eligible for the child.

    This is intentionally stricter than the generic availability context:
    date/time can only be selected after group eligibility is known.
    """
    result = []
    allowed_levels = [experience] if experience else []

    closed = _closed_dates(bot_name)
    for info in _trial_groups(bot_name, "child"):
        if _is_ignored_day(info):
            continue
        birth_years = info.get("birth_years") or []
        if birth_years and int(child_birth_year) not in [int(y) for y in birth_years]:
            continue

        max_cap = info.get("max_cap")
        curr_cap = info.get("curr_cap") or 0
        if max_cap is not None and int(curr_cap) >= int(max_cap):
            continue

        levels = info.get("level") or []
        if isinstance(levels, str):
            levels = [levels]
        if allowed_levels and levels and not set(levels).intersection(allowed_levels):
            continue
        if allowed_levels and not levels:
            continue

        start = info["time_start"]
        end = info["time_end"]
        if school_shift == "morning" and start < time(12, 0):
            continue
        if school_shift == "afternoon" and end > time(12, 0):
            continue

        result.append({
            **info,
            "date": _open_date(_next_class_date(info["training_day"], start), closed),
        })
    return result


def get_birth_year_trial_slots(
    bot_name: str,
    child_birth_year: int,
) -> list[dict]:
    """Return available schedule slots filtered only by academy type and birth year."""
    result = []
    closed = _closed_dates(bot_name)
    for info in _trial_groups(bot_name, "child"):
        if _is_ignored_day(info):
            continue
        birth_years = info.get("birth_years") or []
        if birth_years and int(child_birth_year) not in [int(y) for y in birth_years]:
            continue

        max_cap = info.get("max_cap")
        curr_cap = info.get("curr_cap") or 0
        if max_cap is not None and int(curr_cap) >= int(max_cap):
            continue

        result.append({
            **info,
            "date": _open_date(_next_class_date(info["training_day"], info["time_start"]), closed),
        })
    return result


def child_age_range(bot_name: str, today: date | None = None) -> tuple[int, int] | None:
    """(youngest, oldest) age the active children's groups take, from their
    birth years — None when no children's group lists any."""
    years = {
        int(year)
        for info in academy_repo.get_groups_info(bot_name=bot_name, audience="child")
        for year in (info.get("birth_years") or [])
    }
    if not years:
        return None
    current = (today or today_almaty()).year
    return current - max(years), current - min(years)


def get_adult_trial_slots(bot_name: str) -> list[dict]:
    """Slots of the adult groups that still have room.

    No birth year, level or school shift: any adult can join.
    """
    result = []
    closed = _closed_dates(bot_name)
    for info in _trial_groups(bot_name, "adult"):
        max_cap = info.get("max_cap")
        curr_cap = info.get("curr_cap") or 0
        if max_cap is not None and int(curr_cap) >= int(max_cap):
            continue
        result.append({
            **info,
            "date": _open_date(_next_class_date(info["training_day"], info["time_start"]), closed),
        })
    return result


def is_trial_slot_eligible(bot_name: str, trial: dict) -> bool:
    """Return whether the trial's selected slot still matches its intake data."""
    adult = trial.get("audience") == "adult"
    required = ("group_id", "trial_day", "start_time", "end_time")
    if not adult:
        required += ("child_birth_year", "school_shift")
    if any(trial.get(key) in (None, "") for key in required):
        return False

    if adult:
        slots = get_adult_trial_slots(bot_name)
    else:
        slots = get_eligible_trial_slots(
            bot_name,
            int(trial["child_birth_year"]),
            trial["school_shift"],
            experience=trial.get("experience"),
        )
    for slot in slots:
        if int(slot["group_id"]) != int(trial["group_id"]):
            continue
        if str(slot["date"]) != str(trial["trial_day"]):
            continue
        if str(slot["time_start"])[:5] != str(trial["start_time"])[:5]:
            continue
        if str(slot["time_end"])[:5] != str(trial["end_time"])[:5]:
            continue
        return True
    return False


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

def format_closed_dates_context(bot_name: str) -> str:
    """The upcoming days without training, for the LLM (academy_rules 23)."""
    closed = _closed_dates(bot_name)
    if not closed:
        return ""
    lines = ["Дни без тренировок (праздники, отмены) — в эти дни занятий нет:"]
    for d in sorted(closed):
        note = f" — {closed[d]}" if closed[d] else ""
        lines.append(f"- {_WEEKDAY_RU[d.weekday()]} {d.strftime('%d.%m.%Y')}{note}")
    return "\n".join(lines)


def format_availability_context(free_windows: list[dict]) -> str:
    if not free_windows:
        return "Свободных слотов на ближайшие 7 дней нет."

    by_date: dict[date, list] = {}
    for w in free_windows:
        by_date.setdefault(w["date"], []).append(w)

    lines = [
        "Общее расписание пробных занятий на ближайшие 7 дней.",
        "Не обещай точную доступность без года рождения, уровня подготовки и школьной смены ребенка:",
    ]
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


def format_weekday_schedule(classes: list[dict], lang: str = "ru") -> str:
    """Render list_classes_by_weekday()'s result as a plain grouped list.

    This is a factual "what's on this day" answer, not a booking-eligibility
    one — no age/level gating, so it's safe to show even with no draft/intake
    in progress.
    """
    if not classes:
        return (
            "На эти дни занятий не нашлось." if lang != "kk"
            else "Бұл күндерге сабақ табылмады."
        )
    weekday_names = _WEEKDAY_KK if lang == "kk" else _WEEKDAY_RU
    level_labels = _LEVEL_LABELS_KK if lang == "kk" else _LEVEL_LABELS_RU

    by_date: dict[date, list] = {}
    for c in classes:
        by_date.setdefault(c["date"], []).append(c)

    lines = []
    for d in sorted(by_date):
        lines.append(f"{weekday_names[d.weekday()]} {d.strftime('%d.%m.%Y')}:")
        if any(c.get("closed") for c in by_date[d]):
            note = next((c["closed_note"] for c in by_date[d] if c.get("closed_note")), None)
            closed = "Тренировок нет" if lang != "kk" else "Жаттығу болмайды"
            lines.append(f"  ❌ {closed}" + (f" ({note})" if note else ""))
            continue
        for c in sorted(by_date[d], key=lambda x: x["time_start"]):
            levels = c.get("level") or []
            if isinstance(levels, str):
                levels = [levels]
            level_text = ", ".join(level_labels.get(lv, lv) for lv in levels)
            name = c.get("group_name") or f"Группа #{c.get('group_id')}"
            trainer = f" ({c['trainer']})" if c.get("trainer") else ""
            ts, te = fmt_hhmm(c["time_start"]), fmt_hhmm(c["time_end"])
            suffix = f" | {level_text}" if level_text else ""
            lines.append(f"  {name}{trainer} {ts}–{te}{suffix}")
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
