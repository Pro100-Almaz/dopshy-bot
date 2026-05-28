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
from integrations import postgres
from utils import now_almaty

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
        rows = postgres.get_bookings_for_sheet()
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
