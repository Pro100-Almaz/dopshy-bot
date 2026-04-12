"""Google Sheets integration — one worksheet per field; Sheets is source of truth."""

import logging
import threading
from datetime import date, datetime, timedelta
from typing import Any

import config
from integrations import postgres

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Per-field sheet columns (Поле & Формат are implied by the sheet tab name)
_HEADERS = ["Дата", "Начало", "Конец", "Игроков", "Клиент", "Телефон", "Статус", "Примечание"]
_COL_COUNT = len(_HEADERS)  # 8

_STATUS_MAP = {
    "awaiting_payment": "⏳ Ожидает оплату",
    "paid":             "✅ Оплачено",
    "cancelled":        "❌ Отменено",
    "completed":        "✅ Завершено",
}
_WEEKDAY_RU      = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_WEEKDAY_FULL_RU = ["Понедельник", "Вторник", "Среда", "Четверг",
                    "Пятница", "Суббота", "Воскресенье"]

_COLOR_DAY_HEADER  = {"red": 0.13, "green": 0.26, "blue": 0.48}
_COLOR_COL_HEADER  = {"red": 0.18, "green": 0.34, "blue": 0.62}
_COLOR_WHITE       = {"red": 1.0,  "green": 1.0,  "blue": 1.0}
_COLOR_NO_BOOKINGS = {"red": 0.95, "green": 0.95, "blue": 0.95}

_client: Any = None
_spreadsheet: Any = None
_worksheets: dict[int, Any] = {}
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


def _field_sheet_name(field_id: int) -> str:
    conf = next((f for f in config.BOOKING_FIELDS if f["id"] == field_id), None)
    return f"Поле {field_id} ({conf['format']})" if conf else f"Поле {field_id}"


def _get_worksheet(field_id: int):
    with _ws_lock:
        if field_id not in _worksheets:
            ss         = _get_spreadsheet()
            sheet_name = _field_sheet_name(field_id)
            try:
                _worksheets[field_id] = ss.worksheet(sheet_name)
            except Exception:
                _worksheets[field_id] = ss.add_worksheet(
                    title=sheet_name, rows=1000, cols=_COL_COUNT
                )
    return _worksheets[field_id]


def _booking_to_row(b: dict) -> list:
    d = b["date"] if isinstance(b["date"], date) else \
        datetime.strptime(str(b["date"]), "%Y-%m-%d").date()
    date_str = f"{d.strftime('%d.%m.%Y')} ({_WEEKDAY_RU[d.weekday()]})"
    ts     = str(b.get("time_start", ""))[:5]
    te     = str(b.get("time_end",   ""))[:5]
    status = _STATUS_MAP.get(b.get("status", ""), b.get("status", ""))
    return [
        date_str,
        ts,
        te,
        b.get("players", ""),
        b.get("customer_name", ""),
        b.get("phone", ""),
        status,
        b.get("notes", ""),
    ]


def _repeat_cell_request(sheet_id, start_row, end_row, bg_color,
                         text_color=None, bold=False, font_size=10, h_align="LEFT") -> dict:
    fmt = {
        "backgroundColor": bg_color,
        "textFormat": {
            "bold": bold,
            "fontSize": font_size,
            **({"foregroundColor": text_color} if text_color else {}),
        },
        "horizontalAlignment": h_align,
        "verticalAlignment": "MIDDLE",
    }
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row,
                "endRowIndex": end_row,
                "startColumnIndex": 0,
                "endColumnIndex": _COL_COUNT,
            },
            "cell": {"userEnteredFormat": fmt},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
        }
    }


def _merge_row_request(sheet_id, row_0based) -> dict:
    return {
        "mergeCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": row_0based,
                "endRowIndex": row_0based + 1,
                "startColumnIndex": 0,
                "endColumnIndex": _COL_COUNT,
            },
            "mergeType": "MERGE_ALL",
        }
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_booked_slots(week_start: str, week_end: str) -> list[dict]:
    """
    Read non-cancelled bookings from all field worksheets for the given date range.
    Field id and format are injected from the worksheet context (not parsed from rows).
    """
    if not config.GOOGLE_SPREADSHEET_ID:
        return []

    start = datetime.strptime(week_start, "%Y-%m-%d").date()
    end   = datetime.strptime(week_end,   "%Y-%m-%d").date()
    result = []

    for field_conf in config.BOOKING_FIELDS:
        field_id = field_conf["id"]
        fmt      = field_conf["format"]
        try:
            ws   = _get_worksheet(field_id)
            rows = ws.get_all_values()
            for row in rows:
                if len(row) < 3:
                    continue
                # Skip rows where column A can't be parsed as a booking date
                try:
                    d = datetime.strptime(row[0].split(" ")[0], "%d.%m.%Y").date()
                except (ValueError, IndexError):
                    continue
                if not (start <= d <= end):
                    continue
                status = row[6] if len(row) > 6 else ""
                if "Отменено" in status or "❌" in status:
                    continue
                result.append({
                    "date":       d,
                    "time_start": row[1],
                    "time_end":   row[2],
                    "field":      field_id,
                    "format":     fmt,
                })
        except Exception as exc:
            logger.error("Sheets get_booked_slots field %d failed: %s", field_id, exc)

    return result


def setup_sheet_template() -> None:
    """
    Apply structural formatting to every field worksheet:
      - Column widths
      - Status dropdown validation on column G
    Row-level formatting (day headers, sub-headers) is applied by maybe_refresh_week().
    """
    if not config.GOOGLE_SPREADSHEET_ID:
        return

    # Column widths (px): Дата, Начало, Конец, Игроков, Клиент, Телефон, Статус, Примечание
    col_widths = [140, 70, 70, 70, 160, 130, 170, 210]

    for field_conf in config.BOOKING_FIELDS:
        field_id = field_conf["id"]
        try:
            ws       = _get_worksheet(field_id)
            sheet_id = ws.id
            requests = [
                *[
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": i,
                                "endIndex": i + 1,
                            },
                            "properties": {"pixelSize": w},
                            "fields": "pixelSize",
                        }
                    }
                    for i, w in enumerate(col_widths)
                ],
                # Status dropdown — column G (index 6)
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1000,
                            "startColumnIndex": 6,
                            "endColumnIndex": 7,
                        },
                        "rule": {
                            "condition": {
                                "type": "ONE_OF_LIST",
                                "values": [{"userEnteredValue": v} for v in _STATUS_MAP.values()],
                            },
                            "showCustomUi": True,
                            "strict": False,
                        },
                    }
                },
            ]
            ws.spreadsheet.batch_update({"requests": requests})
            logger.info("Template applied to sheet '%s'.", _field_sheet_name(field_id))
        except Exception as exc:
            logger.error("setup_sheet_template field %d failed: %s", field_id, exc)


def append_booking(booking: dict) -> int:
    """Append a booking row to the correct field's worksheet. Returns 1-based row index."""
    if not config.GOOGLE_SPREADSHEET_ID:
        return 0
    field_id = int(booking.get("field", 0))
    try:
        ws = _get_worksheet(field_id)
        ws.append_row(_booking_to_row(booking), value_input_option="USER_ENTERED")
        return len(ws.get_all_values())
    except Exception as exc:
        logger.error("Sheets append_booking field %d failed: %s", field_id, exc)
        return 0


def update_booking_status_in_sheet(sheet_row: int, field_id: int, status: str) -> None:
    """Update the status cell (column G = 7) for a booking row on the given field's sheet."""
    if not sheet_row or not config.GOOGLE_SPREADSHEET_ID:
        return
    try:
        ws = _get_worksheet(field_id)
        ws.update_cell(sheet_row, 7, _STATUS_MAP.get(status, status))
    except Exception as exc:
        logger.error("Sheets update_status field %d failed: %s", field_id, exc)


def maybe_refresh_week(force: bool = False, target_date: date | None = None) -> None:
    """
    Rewrite every field's worksheet as a structured weekly view.

        ┌──────────────────────────────────────┐
        │  ПОНЕДЕЛЬНИК  —  07.04.2026          │  dark navy, merged
        ├───────────┬───────┬───────┬── … ──────┤
        │   Дата    │Начало │ Конец │    …       │  medium blue
        ├───────────┼───────┼───────┼── … ──────┤
        │ booking rows …                        │
        └──────────────────────────────────────┘
             (spacer)
        ┌──────────────────────────────────────┐
        │  ВТОРНИК  —  08.04.2026              │
        …

    target_date: refresh the calendar week that contains this date (defaults to today).
                 Pass the booking date so bookings near the end of the week that fall
                 in the next calendar week are written to the correct sheet.
    force=True bypasses the already-synced guard (used by the Monday scheduler).
    """
    if not config.GOOGLE_SPREADSHEET_ID:
        return

    anchor     = target_date or date.today()
    week_start = anchor - timedelta(days=anchor.weekday())

    if not force and postgres.is_week_synced_to_sheets(str(week_start)):
        return

    try:
        week_end = week_start + timedelta(days=6)
        all_bookings = postgres.get_booked_slots(str(week_start), str(week_end))

        for field_conf in config.BOOKING_FIELDS:
            field_id = field_conf["id"]
            bookings = [b for b in all_bookings if int(b["field"]) == field_id]

            # Group by ISO date
            by_date: dict[str, list] = {}
            for b in bookings:
                by_date.setdefault(str(b["date"]), []).append(b)

            ws       = _get_worksheet(field_id)
            sheet_id = ws.id
            ws.clear()

            all_rows: list[list]     = []
            fmt_requests: list[dict] = []
            row = 0  # 0-based

            for day_offset in range(7):
                d            = week_start + timedelta(days=day_offset)
                day_label    = f"{_WEEKDAY_FULL_RU[d.weekday()].upper()}  —  {d.strftime('%d.%m.%Y')}"
                day_bookings = by_date.get(str(d), [])

                # Day header row
                all_rows.append([day_label] + [""] * (_COL_COUNT - 1))
                fmt_requests.append(_merge_row_request(sheet_id, row))
                fmt_requests.append(_repeat_cell_request(
                    sheet_id, row, row + 1,
                    bg_color=_COLOR_DAY_HEADER, text_color=_COLOR_WHITE,
                    bold=True, font_size=11, h_align="CENTER",
                ))
                row += 1

                # Column sub-headers
                all_rows.append(list(_HEADERS))
                fmt_requests.append(_repeat_cell_request(
                    sheet_id, row, row + 1,
                    bg_color=_COLOR_COL_HEADER, text_color=_COLOR_WHITE,
                    bold=True, font_size=10, h_align="CENTER",
                ))
                row += 1

                # Booking rows
                if day_bookings:
                    for b in day_bookings:
                        all_rows.append(_booking_to_row(b))
                        row += 1
                else:
                    all_rows.append(["Нет броней"] + [""] * (_COL_COUNT - 1))
                    fmt_requests.append(_repeat_cell_request(
                        sheet_id, row, row + 1, bg_color=_COLOR_NO_BOOKINGS,
                    ))
                    row += 1

                # Spacer
                all_rows.append([""] * _COL_COUNT)
                row += 1

            ws.update(f"A1:{chr(ord('A') + _COL_COUNT - 1)}{len(all_rows)}", all_rows)
            if fmt_requests:
                ws.spreadsheet.batch_update({"requests": fmt_requests})

            logger.info("Refreshed sheet '%s'.", _field_sheet_name(field_id))

        postgres.mark_week_synced_to_sheets(str(week_start))
        logger.info("Weekly Sheets refresh complete for week starting %s.", week_start)
    except Exception as exc:
        logger.error("Weekly Sheets refresh failed: %s", exc)
