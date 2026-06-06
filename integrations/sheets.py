"""Google Sheets integration — flat one-row-per-booking view.

PostgreSQL is the source of truth. This module keeps a single "Bookings"
worksheet in sync as a human-facing view that managers operate on through the
Apps Script manager UI (which calls the manager_api, not this module).

Sheet layout (row 1 = fixed header):
    A booking_id | B field | C date | D start | E end |
    F customer   | G notes | H status | I last_synced
"""

import logging
import threading
from typing import Any

import config
from integrations.repo import booking_repo
from utils import now_almaty, today_almaty
import datetime

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_HEADERS = ["booking_id", "field", "date", "start", "end",
            "customer", "notes", "status", "last_synced"]
_COL_COUNT = len(_HEADERS)  # 9

# DB state → sheet status label (uppercase, matches Apps Script dropdown).
_STATE_DISPLAY = {
    "draft":            "DRAFT",
    "awaiting_payment": "AWAITING_PAYMENT",
    "confirmed":        "CONFIRMED",
    "cancelled":        "CANCELLED",
    "failed":           "FAILED",
}

_STATES_RUSSIAN = {
    "draft":            "ЧЕРНОВИК",
    "awaiting_payment": "ОЖИДАЕТ ОПЛАТЫ",
    "confirmed":        "ПОДТВЕРЖДЕНО",
    "cancelled":        "ОТМЕНЕНО",
    "failed":           "ПРОВАЛИЛОСЬ",
}


_client: Any = None
_spreadsheet: Any = None
_worksheet: Any = None
_ws_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_spreadsheet():
    global _client, _spreadsheet
    if _spreadsheet is None:
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_file(
            config.GOOGLE_CREDENTIALS_PATH, scopes=_SCOPES
        )
        _client = gspread.authorize(creds)
        raw = config.GOOGLE_SPREADSHEET_ID
        if "/spreadsheets/d/" in raw:
            raw = raw.split("/spreadsheets/d/")[1].split("/")[0].split("?")[0]
        _spreadsheet = _client.open_by_key(raw)
    return _spreadsheet


def _get_worksheet():
    global _worksheet
    with _ws_lock:
        if _worksheet is None:
            ss = _get_spreadsheet()
            name = config.GOOGLE_WORKSHEET_NAME
            try:
                _worksheet = ss.worksheet(name)
            except Exception:
                _worksheet = ss.add_worksheet(title=name, rows=1000, cols=_COL_COUNT)
                _worksheet.update("A1", [_HEADERS])
        return _worksheet


def _booking_to_row(b: dict) -> list:
    return [
        str(b["id"]),
        b.get("field", ""),
        str(b["date"])[:10],
        str(b.get("time_start", ""))[:5],
        str(b.get("time_end", ""))[:5],
        b.get("customer_name", "") or "",
        b.get("notes", "") or "",
        _STATE_DISPLAY.get(b.get("state", ""), b.get("state", "")),
        now_almaty().strftime("%Y-%m-%d %H:%M"),
    ]


def _last_col_letter() -> str:
    return chr(ord("A") + _COL_COUNT - 1)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upsert_booking_row(booking: dict) -> None:
    """Insert or update the row for a single booking (matched by booking_id in col A)."""
    if not config.GOOGLE_SPREADSHEET_ID:
        return
    try:
        ws = _get_worksheet()
        row_values = _booking_to_row(booking)
        col_a = ws.col_values(1)  # includes header in row 1
        target = str(booking["id"])
        try:
            idx = col_a.index(target) + 1  # 1-based sheet row
            ws.update(f"A{idx}:{_last_col_letter()}{idx}", [row_values],
                      value_input_option="USER_ENTERED")
        except ValueError:
            ws.append_row(row_values, value_input_option="USER_ENTERED")
    except Exception as exc:
        logger.error("Sheets upsert_booking_row failed for booking %s: %s",
                     booking.get("id"), exc)


def update_booking_row(booking_id: int, fields: dict) -> None:
    """Patch specific cells of an existing booking row (used by manager edits)."""
    if not config.GOOGLE_SPREADSHEET_ID:
        return
    col_for = {"field": 2, "date": 3, "time_start": 4, "time_end": 5,
               "customer_name": 6, "notes": 7, "state": 8}
    try:
        ws = _get_worksheet()
        col_a = ws.col_values(1)
        idx = col_a.index(str(booking_id)) + 1
        for key, value in fields.items():
            col = col_for.get(key)
            if not col:
                continue
            if key == "state":
                value = _STATE_DISPLAY.get(value, value)
            ws.update_cell(idx, col, value)
        ws.update_cell(idx, 9, now_almaty().strftime("%Y-%m-%d %H:%M"))
    except ValueError:
        logger.warning("Sheets update_booking_row: booking %s not found", booking_id)
    except Exception as exc:
        logger.error("Sheets update_booking_row failed for booking %s: %s", booking_id, exc)


def refresh_all_bookings() -> None:
    """Rewrite the whole sheet from PostgreSQL (header + all active bookings)."""
    if not config.GOOGLE_SPREADSHEET_ID:
        return
    try:
        rows = booking_repo.get_bookings_for_sheet()
        ws = _get_worksheet()
        ws.clear()
        data = [_HEADERS] + [_booking_to_row(b) for b in rows]
        ws.update(f"A1:{_last_col_letter()}{len(data)}", data,
                  value_input_option="USER_ENTERED")
        logger.info("Refreshed Bookings sheet — %d rows.", len(rows))
    except Exception as exc:
        logger.error("Sheets refresh_all_bookings failed: %s", exc)


def setup_sheet_template() -> None:
    """Apply header, column widths, and the status dropdown (column H). Run once."""
    if not config.GOOGLE_SPREADSHEET_ID:
        return
    col_widths = [110, 60, 110, 70, 70, 200, 240, 160, 150]
    try:
        ws = _get_worksheet()
        ws.update("A1", [_HEADERS])
        sheet_id = ws.id
        requests = [
            *[
                {
                    "updateDimensionProperties": {
                        "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                  "startIndex": i, "endIndex": i + 1},
                        "properties": {"pixelSize": w},
                        "fields": "pixelSize",
                    }
                }
                for i, w in enumerate(col_widths)
            ],
            {
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "backgroundColor": {"red": 0.18, "green": 0.34, "blue": 0.62},
                    }},
                    "fields": "userEnteredFormat(textFormat,backgroundColor)",
                }
            },
            {"updateSheetProperties": {
                "properties": {"sheetId": sheet_id,
                               "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }},
            {
                "setDataValidation": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 1000,
                              "startColumnIndex": 7, "endColumnIndex": 8},
                    "rule": {
                        "condition": {"type": "ONE_OF_LIST",
                                      "values": [{"userEnteredValue": v}
                                                 for v in _STATE_DISPLAY.values()]},
                        "showCustomUi": True, "strict": False,
                    },
                }
            },
        ]
        ws.spreadsheet.batch_update({"requests": requests})
        logger.info("Bookings sheet template applied.")
    except Exception as exc:
        logger.error("setup_sheet_template failed: %s", exc)


_WEEK_SHEET_NAMES = [(1, "Поле 1"), (2, "Поле 2"), (3, "Поле 3")]
_TIME_SLOTS = [f'{h:02d}:{m:02d}' for h in range(24) for m in [0, 30]]
_DAY_HEADERS = ["Time", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


_week_worksheets: dict[int, Any] = {}
_week_ws_lock = threading.Lock()


def _get_week_worksheet(field_num : int):
    global _week_worksheets
    with _week_ws_lock:
        if field_num not in _week_worksheets:
            ss = _get_spreadsheet()
            sheet_name = dict(_WEEK_SHEET_NAMES)[field_num]
            try:
                _week_worksheets[field_num] = ss.worksheet(sheet_name)
            except Exception:
                _week_worksheets[field_num] = ss.add_worksheet(
                    title=sheet_name, rows=50, cols=8
                )
                _week_worksheets[field_num].update("A1:H1", [_DAY_HEADERS])
        return _week_worksheets[field_num]


def _get_current_week_bookings(field_num: int) -> list[dict]:
    today = today_almaty()
    last_day = today + datetime.timedelta(days=6)

    bookings = booking_repo.get_bookings_in_range(
        str(today), str(last_day), states=("awaiting_payment", "confirmed")
    )

    filtered_bookings = []
    for booking in bookings:
        if booking['field'] == field_num:
            filtered_bookings.append(booking)
    return filtered_bookings


def _display_week_dates() -> list[datetime.date]:
    today = today_almaty()
    monday = today - datetime.timedelta(days=today.weekday())
    dates = []
    for weekday in range(7):
        d = monday + datetime.timedelta(days=weekday)
        if d < today:
            d = d + datetime.timedelta(days=7)

        dates.append(d)

    return dates


def _build_weekly_sheet(worksheet) -> None:
    dates = _display_week_dates()
    header = ['Time']
    i = 0
    for d in dates:
        header.append(f"{_DAY_HEADERS[i + 1]} {d.strftime('%d.%m')}")
        i += 1

    rows = [header]
    for slot in _TIME_SLOTS:
        rows.append([slot] + [""] * 7)

    worksheet.clear()
    worksheet.update(f"A1:H{len(rows)}", rows, value_input_option="USER_ENTERED")

    requests = []
    requests.append(_get_paint_background_request(worksheet.id, 0, 49, 0, 8, 1, 1, 1))
    requests.append(_get_border_request(worksheet.id, 0, 49, 0, 8))
    requests.append(_get_paint_background_request(worksheet.id, 0, 1, 1, 8, 1, 0.67, 0.1))
    requests.append(_get_unmerge_request(worksheet.id, 0, 49, 0, 8))

    worksheet.spreadsheet.batch_update({'requests': requests})


def _get_paint_background_request(worksheet_id, start_row_id, end_row_id, start_col_id, end_col_id, r, g, b) -> dict:
    return {
        'repeatCell': {
            "range": {
                "sheetId": worksheet_id,
                "startRowIndex": start_row_id,
                "endRowIndex": end_row_id,
                "startColumnIndex": start_col_id,
                "endColumnIndex": end_col_id
            },
            'cell': {
                "userEnteredFormat": {
                    "backgroundColor": {
                        "red": r,
                        "green": g,
                        "blue": b
                    },
                },
                "note": "",
            },
            "fields": "userEnteredFormat.backgroundColor, note"
        }
    }


def _get_unmerge_request(worksheet_id, start_row_id, end_row_id, start_col_id, end_col_id,) -> dict:
    return {
        "unmergeCells": {
            "range": {
                "sheetId": worksheet_id,
                "startRowIndex": start_row_id,
                "endRowIndex": end_row_id,
                "startColumnIndex": start_col_id,
                "endColumnIndex": end_col_id,
            }
        }
    }


def _floor_time_to_30_minutes(t: datetime.time) -> str:
    hour = t.hour
    if t.minute < 15:
        minute = 0
    elif t.minute > 45:
        minute = 0
        hour += 1
    else:
        minute = 30
    return f"{hour:02d}:{minute:02d}"


def _paint_confirmed_booking(worksheet, booking, requests) -> None:
    booking_date = booking['date']
    booking_start_time = _floor_time_to_30_minutes(booking['time_start'])
    booking_end_time = _floor_time_to_30_minutes(booking['time_end'])

    col = booking_date.weekday() + 2
    start_slot_index = _TIME_SLOTS.index(booking_start_time)
    end_slot_index = _TIME_SLOTS.index(booking_end_time)

    sheet_row = start_slot_index + 2

    cell_text = (
        f"{booking.get('customer_name') or 'No Customer name'}\n"
        f"{booking_start_time} - {booking_end_time}\n"
        f"{booking.get('notes') or ''}"
    ).strip()

    worksheet.update_cell(sheet_row, col, cell_text)

    note_text = (
        f"Booking ID: {booking.get('id')}\n"
        f"Клиент: {booking.get('customer_name') or 'No customer name'}\n"
        f"Номер Телефона: {booking.get('phone') or '-'}\n"
        f"Заметки: {booking.get('notes') or '-'}\n"
        f"Цена: {booking.get('price_total') or '-'}\n"
        f"Статус: {_STATES_RUSSIAN[booking.get('state')] or '-'}"
    )

    requests.append({
        'repeatCell': {
            "range": {
                "sheetId": worksheet.id,
                "startRowIndex": start_slot_index + 1,
                "endRowIndex": end_slot_index + 1,
                "startColumnIndex": col - 1,
                "endColumnIndex": col
            },
            'cell': {
                "note": note_text,
                "userEnteredFormat": {
                    "backgroundColor": {
                        "red": 0.65,
                        "green": 0.95,
                        "blue": 0.65
                    },
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                    "wrapStrategy": "WRAP",
                    "textFormat": {
                        "bold": True,
                        "fontSize": 9
                    }
                }
            },
            "fields": "note, userEnteredFormat"
        }
    })

    requests.append({
        "mergeCells": {
            "range": {
                "sheetId": worksheet.id,
                "startRowIndex": start_slot_index + 1,
                "endRowIndex": end_slot_index + 1,
                "startColumnIndex": col - 1,
                "endColumnIndex": col
            },
            'mergeType': 'MERGE_ALL'
        }
    })


def _paint_weekly_bookings(worksheet, bookings):
    requests = []
    for booking in bookings:
        _paint_confirmed_booking(worksheet, booking, requests)

    if requests:
        worksheet.spreadsheet.batch_update({"requests": requests})


def refresh_week_sheet() -> None:
    try:
        for i in range(3):
            ws = _get_week_worksheet(i + 1)

            _build_weekly_sheet(ws)
            bookings = _get_current_week_bookings(i + 1)
            _paint_weekly_bookings(ws, bookings)

    except Exception as exc:
        logger.exception("REFRESH_WEEK_SHEET FAILSED: %s", exc)


def _get_border_request(worksheet_id, start_row_id, end_row_id, start_col_id, end_col_id) -> dict:
    border = {
        'style' : 'SOLID',
        'width' : 1,
        'color' : {
            'red': 0,
            'green' : 0,
            'blue' : 0
        }
    }

    return {
        "updateBorders": {
            "range": {
                "sheetId": worksheet_id,
                "startRowIndex": start_row_id,
                "endRowIndex": end_row_id,
                "startColumnIndex": start_col_id,
                "endColumnIndex": end_col_id
            },
            'top' : border,
            'bottom' : border,
            'left' : border,
            'right' : border,
            'innerHorizontal' : border,
            'innerVertical' : border,
        }
    }


def col_index_to_letter(index):
    index += 1
    letters = ""

    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters

    return letters


def _single_table_write(booking):
    field = booking.get('field')
    worksheet = _get_week_worksheet(field)

    requests = []
    _paint_confirmed_booking(worksheet, booking, requests)

    if requests:
        worksheet.spreadsheet.batch_update({'requests': requests})


def _single_table_erase(booking) -> None:
    field = booking.get('field')
    worksheet = _get_week_worksheet(field)

    booking_date = booking['date']
    booking_start_time = _floor_time_to_30_minutes(booking['time_start'])
    booking_end_time = _floor_time_to_30_minutes(booking['time_end'])

    col = booking_date.weekday() + 1
    start_slot_index = _TIME_SLOTS.index(booking_start_time) + 1
    end_slot_index = _TIME_SLOTS.index(booking_end_time) + 1

    col_letter = col_index_to_letter(col)
    range_name = f"{col_letter}{start_slot_index}:{col_letter}{end_slot_index}"

    worksheet.batch_clear([range_name])

    requests = []
    requests.append(_get_unmerge_request(worksheet.id, start_slot_index, end_slot_index, col, col + 1))
    requests.append(_get_border_request(worksheet.id, start_slot_index, end_slot_index, col, col + 1))
    requests.append(_get_paint_background_request(worksheet.id, start_slot_index, end_slot_index, col, col + 1, 1, 1, 1))
    worksheet.spreadsheet.batch_update({'requests': requests})

