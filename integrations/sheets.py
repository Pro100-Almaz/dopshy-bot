"""Google Sheets integration — bot writes, admin reads."""

import logging
import threading
from datetime import date, datetime, timedelta

import config
from integrations import postgres

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_HEADERS = ["Дата", "Начало", "Конец", "Поле", "Формат", "Игроков",
            "Клиент", "Телефон", "Статус", "Примечание"]
_STATUS_MAP = {
    "awaiting_payment": "⏳ Ожидает оплату",
    "paid":             "✅ Оплачено",
    "cancelled":        "❌ Отменено",
    "completed":        "✅ Завершено",
}
_WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

_client = None
_worksheet = None
_ws_lock = threading.Lock()


def _get_worksheet():
    global _client, _worksheet
    with _ws_lock:
        if _worksheet is None:
            import gspread
            from google.oauth2.service_account import Credentials

            creds = Credentials.from_service_account_file(
                config.GOOGLE_CREDENTIALS_PATH, scopes=_SCOPES
            )
            _client = gspread.authorize(creds)
            spreadsheet = _client.open_by_key(config.GOOGLE_SPREADSHEET_ID)
            _worksheet = spreadsheet.worksheet(config.GOOGLE_WORKSHEET_NAME)
    return _worksheet


def _booking_to_row(b: dict) -> list:
    d = b["date"] if isinstance(b["date"], date) else \
        datetime.strptime(str(b["date"]), "%Y-%m-%d").date()
    date_str = f"{d.strftime('%d.%m.%Y')} ({_WEEKDAY_RU[d.weekday()]})"
    ts = str(b.get("time_start", ""))[:5]
    te = str(b.get("time_end", ""))[:5]
    status = _STATUS_MAP.get(b.get("status", ""), b.get("status", ""))
    return [
        date_str,
        ts,
        te,
        b.get("field", ""),
        b.get("format", ""),
        b.get("players", ""),
        b.get("customer_name", ""),
        b.get("phone", ""),
        status,
        b.get("notes", ""),
    ]


def _ensure_headers(ws) -> None:
    if not ws.get_all_values():
        ws.append_row(_HEADERS, value_input_option="USER_ENTERED")


def append_booking(booking: dict) -> int:
    """Append a booking row. Returns the 1-based row index (0 on failure)."""
    if not config.GOOGLE_SPREADSHEET_ID:
        return 0
    try:
        ws = _get_worksheet()
        _ensure_headers(ws)
        ws.append_row(_booking_to_row(booking), value_input_option="USER_ENTERED")
        return len(ws.get_all_values())
    except Exception as exc:
        logger.error("Sheets append_booking failed: %s", exc)
        return 0


def update_booking_status_in_sheet(sheet_row: int, status: str) -> None:
    """Update only the status cell (column I = index 9) for a given row."""
    if not sheet_row or not config.GOOGLE_SPREADSHEET_ID:
        return
    try:
        ws = _get_worksheet()
        ws.update_cell(sheet_row, 9, _STATUS_MAP.get(status, status))
    except Exception as exc:
        logger.error("Sheets update_status failed: %s", exc)


def maybe_refresh_week() -> None:
    """
    On the first booking action of a new week, clear the sheet and rewrite
    all active bookings for the current week from PostgreSQL.
    This is called from a background thread so it never blocks a reply.
    """
    if not config.GOOGLE_SPREADSHEET_ID:
        return

    today = date.today()
    week_start = today - timedelta(days=today.weekday())

    if postgres.is_week_synced_to_sheets(str(week_start)):
        return

    try:
        week_end = week_start + timedelta(days=6)
        bookings = postgres.get_booked_slots(str(week_start), str(week_end))

        ws = _get_worksheet()
        ws.clear()
        ws.append_row(_HEADERS, value_input_option="USER_ENTERED")

        if bookings:
            rows = [_booking_to_row(b) for b in bookings]
            ws.append_rows(rows, value_input_option="USER_ENTERED")

        postgres.mark_week_synced_to_sheets(str(week_start))
        logger.info("Weekly Sheets refresh complete for week starting %s.", week_start)
    except Exception as exc:
        logger.error("Weekly Sheets refresh failed: %s", exc)
