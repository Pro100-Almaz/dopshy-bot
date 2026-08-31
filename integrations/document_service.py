from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import config
from integrations import booking_service
from integrations.repo import booking_repo


REPORT_INCLUDED_STATUSES = {"confirmed", "completed"}
MAX_REPORT_RANGE_DAYS = 366


class DocumentValidationError(ValueError):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def build_arena_extract_data(payload: dict | None) -> dict:
    request_data = _validate_extract_payload(payload)
    rows = booking_repo.get_report_bookings_in_range(
        request_data["start_date"].isoformat(),
        request_data["end_date"].isoformat(),
        tuple(sorted(REPORT_INCLUDED_STATUSES)),
    )
    rows = [
        row for row in rows
        if str(row.get("state") or "").lower() in REPORT_INCLUDED_STATUSES
    ]
    rows = _attach_payment_totals(rows, booking_service.get_payments())
    fields = booking_repo.get_fields_info()
    active_field_count = _active_field_count(fields, rows)
    bookings = [_booking_export_row(row) for row in rows]
    summary = _summary(bookings, request_data["period_days"], active_field_count)

    return {
        "bot_type": "arena",
        "bot": "Arena",
        "period": {
            "start_date": request_data["start_date"].isoformat(),
            "end_date": request_data["end_date"].isoformat(),
            "days": request_data["period_days"],
        },
        "included_statuses": sorted(REPORT_INCLUDED_STATUSES),
        "active_field_count": active_field_count,
        "generated_at": datetime.now(ZoneInfo(config.BOOKING_TIMEZONE)).isoformat(),
        "summary": summary,
        "bookings": bookings,
    }


def _validate_extract_payload(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        raise DocumentValidationError("JSON body is required.")

    bot_type = payload.get("bot_type")
    if bot_type != "arena":
        raise DocumentValidationError("Only arena document extraction is supported now.")

    start_date = _parse_date(payload.get("start_date"), "start_date")
    end_date = _parse_date(payload.get("end_date"), "end_date")
    if start_date > end_date:
        raise DocumentValidationError("start_date must be before or equal to end_date.")

    period_days = (end_date - start_date).days + 1
    if period_days > MAX_REPORT_RANGE_DAYS:
        raise DocumentValidationError(
            f"Date range must not exceed {MAX_REPORT_RANGE_DAYS} days."
        )

    return {
        "bot_type": bot_type,
        "start_date": start_date,
        "end_date": end_date,
        "period_days": period_days,
    }


def _parse_date(value, field_name: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise DocumentValidationError(f"{field_name} is required.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DocumentValidationError(f"{field_name} must be a valid YYYY-MM-DD date.") from exc


def _attach_payment_totals(bookings: list[dict], payments: list[dict]) -> list[dict]:
    totals: dict[int, Decimal] = {}
    latest_receipt: dict[int, datetime] = {}
    for payment in payments:
        booking_id = payment.get("booking_id")
        if booking_id is None:
            continue
        totals[booking_id] = totals.get(booking_id, Decimal(0)) + _money(payment.get("amount"))
        receipt_date = _datetime_value(payment.get("receipt_date"))
        if receipt_date and (
            latest_receipt.get(booking_id) is None
            or receipt_date > latest_receipt[booking_id]
        ):
            latest_receipt[booking_id] = receipt_date

    for booking in bookings:
        booking_id = booking.get("id")
        booking["paid_api"] = totals.get(booking_id, Decimal(0))
        booking["last_receipt_date"] = latest_receipt.get(booking_id)
        for payment_field in ("paid_cash", "paid_kaspi_qr", "paid_avans"):
            booking[payment_field] = _money(booking.get(payment_field))
    return bookings


def _booking_export_row(row: dict) -> dict:
    duration_hours = _duration_hours(row.get("time_start"), row.get("time_end"))
    paid_cash = _money(row.get("paid_cash"))
    paid_kaspi_qr = _money(row.get("paid_kaspi_qr"))
    paid_api = _money(row.get("paid_api"))
    paid_avans = _money(row.get("paid_avans"))

    return {
        "id": row.get("id"),
        "customer_name": row.get("customer_name") or "",
        "field": row.get("field"),
        "date": _date_string(row.get("date")),
        "time_start": _time_string(row.get("time_start")),
        "time_end": _time_string(row.get("time_end")),
        "duration_hours": duration_hours,
        "price_total": _number(_money(row.get("price_total"))),
        "paid_cash": _number(paid_cash),
        "paid_kaspi_qr": _number(paid_kaspi_qr),
        "paid_api": _number(paid_api),
        "paid_avans": _number(paid_avans),
        "total_paid_amount": _number(paid_cash + paid_kaspi_qr + paid_api + paid_avans),
        "state": row.get("state") or "",
        "source": row.get("source") or "",
    }


def _summary(bookings: list[dict], period_days: int, active_field_count: int) -> dict:
    booked_hours = sum(
        (Decimal(str(row["duration_hours"])) for row in bookings),
        Decimal(0),
    )
    total_paid = sum(
        (_money(row.get("total_paid_amount")) for row in bookings),
        Decimal(0),
    )
    per_field_hours: dict[int, Decimal] = {}
    for row in bookings:
        field = row.get("field")
        if field is None:
            continue
        per_field_hours[field] = per_field_hours.get(field, Decimal(0)) + Decimal(
            str(row["duration_hours"])
        )

    capacity_hours = Decimal(24 * period_days * active_field_count)
    field_capacity_hours = Decimal(24 * period_days)
    return {
        "total_paid_amount": _number(total_paid),
        "booking_count": len(bookings),
        "total_booked_hours": _number(booked_hours),
        "load_level": _ratio_number(booked_hours, capacity_hours),
        "load_level_percent": _number(_ratio(booked_hours, capacity_hours) * Decimal(100)),
        "per_field_load": [
            {
                "field": field,
                "booked_hours": _number(hours),
                "load_level": _ratio_number(hours, field_capacity_hours),
                "load_level_percent": _number(_ratio(hours, field_capacity_hours) * Decimal(100)),
            }
            for field, hours in sorted(per_field_hours.items())
        ],
    }


def _duration_hours(start, end) -> float:
    start_minutes = _minutes(start, is_end=False)
    end_minutes = _minutes(end, is_end=True)
    if end_minutes < start_minutes:
        end_minutes += 24 * 60
    return _number(Decimal(end_minutes - start_minutes) / Decimal(60))


def _minutes(value, *, is_end: bool) -> int:
    if isinstance(value, time):
        hour = value.hour
        minute = value.minute
    else:
        try:
            parts = str(value or "").split(":")
            hour = int(parts[0])
            minute = int(parts[1])
        except (IndexError, TypeError, ValueError):
            return 0
    if is_end and hour == 23 and minute == 59:
        return 24 * 60
    return hour * 60 + minute


def _active_field_count(fields: list[dict], bookings: list[dict]) -> int:
    field_ids = {field.get("id") for field in fields if field.get("id") is not None}
    if field_ids:
        return len(field_ids)
    booking_field_ids = {row.get("field") for row in bookings if row.get("field") is not None}
    if booking_field_ids:
        return len(booking_field_ids)
    return max(1, len(config.BOOKING_FIELDS))


def _money(value) -> Decimal:
    if value in (None, ""):
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _datetime_value(value) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(config.BOOKING_TIMEZONE))
    return parsed.astimezone(timezone.utc)


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= 0:
        return Decimal(0)
    return numerator / denominator


def _ratio_number(numerator: Decimal, denominator: Decimal) -> float | int:
    value = _ratio(numerator, denominator).quantize(Decimal("0.0001"))
    if value == value.to_integral():
        return int(value)
    return float(value)


def _number(value: Decimal | int | float) -> float | int:
    value = Decimal(str(value))
    normalized = value.quantize(Decimal("0.01"))
    if normalized == normalized.to_integral():
        return int(normalized)
    return float(normalized)


def _date_string(value) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")


def _time_string(value) -> str:
    if isinstance(value, time):
        return value.strftime("%H:%M")
    return str(value or "")[:5]
