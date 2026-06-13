
import logging
import threading
from typing import Any

import config
from integrations.repo import booking_repo, academy_repo
from integrations.repo.academy_repo import get_group_by_id
from integrations.sheets.booking_sheets import _get_spreadsheet

from utils import now_almaty, today_almaty
import datetime

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


_HEADERS = {
        'groups' : ['group_id', 'group_name', 'max_cap', 'curr_cap', 'training_day', 'start_time',	'end_time'],
        'trials' : ['trial_id',	'child_name',	'child_age',	'language',	'phone',	'group_id',	'trial_day',
                    'start_time',	'end_time',	'state',	'notes',	'attended',	'subscribed']
    }

_GROUP_COL_COUNT = len(_HEADERS['groups'])
_TRIAL_COL_COUNT = len(_HEADERS['trials'])

# DB state → sheet status label (uppercase, matches Apps Script dropdown).

_STATES_RUSSIAN = {
    "draft":            "ЧЕРНОВИК",
    "confirmed":        "ПОДТВЕРЖДЕНО",
    "cancelled":        "ОТМЕНЕНО",
    "failed":           "ПРОВАЛИЛОСЬ",
}


_client: Any = None
_spreadsheet: Any = None
_worksheet: Any = None
_ws_lock = threading.Lock()


_WORKSHEETS = {
    'boxing' : {
        'groups' : 'Boxing_Groups',
        'trials' : 'Boxing_Trials'
    },
    'football' : {
        'groups' : 'Football_Groups',
        'trials' : 'Football_Trials'
    }
}

WEEKDAY_RU = {
    0: "Понедельник",
    1: "Вторник",
    2: "Среда",
    3: "Четверг",
    4: "Пятница",
    5: "Суббота",
    6: "Воскресенье",
}


def _get_worksheet(curriculum: str, object_type: str):
    #curriculum = 'boxing' or 'football'
    #object_type = 'groups' or 'trials'
    global _worksheet
    with _ws_lock:
        ss = _get_spreadsheet()
        object_ent = 'boxing' if curriculum == 'boxing' else 'football'
        name = _WORKSHEETS[object_ent][object_type]
        try:
            _worksheet = ss.worksheet(name)
        except Exception:
            _worksheet = ss.add_worksheet(title=name, rows=1000, cols=_GROUP_COL_COUNT if object_type == 'groups' else _TRIAL_COL_COUNT)
            _worksheet.update("A1", [_HEADERS])

        return _worksheet


def _group_to_row(g: dict) -> list:
    return [
        str(g["id"]),
        str(g["group_name"]),
        g["max_cap"],
        g.get("curr_cap", 0),
        WEEKDAY_RU[g["training_day"]],
        str(g["time_start"]),
        str(g["time_end"]),
    ]


def _trial_to_row(g: dict) -> list:
    return [
        str(g["id"]),
        str(g["child_name"]),
        g["child_age"],
        g["language"],
        str(g["phone"]),
        g["group_id"],
        str(g["trial_day"]),
        str(g["start_time"]),
        str(g["end_time"]),
        _STATES_RUSSIAN[g["state"]],
        str(g["notes"]),
        str(g["attended"]),
        str(g["subscribed"]),
    ]

def _last_col_letter(col_count: int) -> str:
    return chr(ord("A") + col_count - 1)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
# ---------Groups

def upsert_group_row(group: dict) -> None:
    """Insert or update the row for a single grouping (matched by group_id in col A)."""
    if not config.GOOGLE_SPREADSHEET_ID:
        return
    try:
        ws = _get_worksheet(group['group_type'], 'groups')
        row_values = _group_to_row(group)
        col_a = ws.col_values(1)  # includes header in row 1
        target = str(group["id"])
        try:
            idx = col_a.index(target) + 1  # 1-based sheet row
            ws.update(f"A{idx}:{_last_col_letter(_GROUP_COL_COUNT)}{idx}", [row_values],
                      value_input_option="USER_ENTERED")
        except ValueError:
            ws.append_row(row_values, value_input_option="USER_ENTERED")
    except Exception as exc:
        logger.error("Sheets upsert_group_row failed for academy_groups %s: %s",
                     group.get("id"), exc)


# refreshes the specific group worksheet
# pastes the headers and puts all the data(all active bookings) in the worksheet
def refresh_all_groups() -> None:
    if not config.GOOGLE_SPREADSHEET_ID:
        return
    try:
        for group_type in ['boxing', 'football']:
            rows = academy_repo.get_groups_for_refresh(group_type)
            ws = _get_worksheet(curriculum=group_type, object_type='groups')
            ws.clear()
            data = [_HEADERS['groups']] + [_group_to_row(b) for b in rows]
            ws.update(f"A1:{_last_col_letter(_GROUP_COL_COUNT)}{len(data)}", data,
                      value_input_option="USER_ENTERED")
            logger.info("Refreshed GROUP sheets — %d rows.", len(rows))
    except Exception as exc:
        logger.error("Sheets refresh_all_groups failed: %s", exc)


# ------------ Trials

def upsert_trial_row(trial: dict) -> None:
    """Insert or update the row for a single grouping (matched by booking_id in col A)."""
    if not config.GOOGLE_SPREADSHEET_ID:
        return
    try:
        curriculum = get_group_by_id(trial['group_id'])['curriculum']
        ws = _get_worksheet(curriculum, 'trials')
        row_values = _trial_to_row(trial)
        col_a = ws.col_values(1)  # includes header in row 1
        target = str(trial["id"])
        try:
            idx = col_a.index(target) + 1  # 1-based sheet row
            ws.update(f"A{idx}:{_last_col_letter(_GROUP_COL_COUNT)}{idx}", [row_values],
                      value_input_option="USER_ENTERED")
        except ValueError:
            ws.append_row(row_values, value_input_option="USER_ENTERED")
    except Exception as exc:
        logger.error("Sheets upsert_trial_row failed for trials %s: %s",
                     trial.get("id"), exc)


# refreshes the specific group worksheet
# pastes the headers and puts all the data(all active bookings) in the worksheet
def refresh_all_trials() -> None:
    if not config.GOOGLE_SPREADSHEET_ID:
        return
    try:
        for group_type in ['boxing', 'football']:
            rows = academy_repo.get_trials_by_type(group_type)
            ws = _get_worksheet(curriculum=group_type, object_type='trials')
            ws.clear()
            data = [_HEADERS['trials']] + [_trial_to_row(trial) for trial in rows]
            ws.update(f"A1:{_last_col_letter(_TRIAL_COL_COUNT)}{len(data)}", data,
                      value_input_option="USER_ENTERED")
            logger.info("Refreshed TRIAL sheets — %d rows.", len(rows))
    except Exception as exc:
        logger.error("Sheets refresh_all_trials failed: %s", exc)




