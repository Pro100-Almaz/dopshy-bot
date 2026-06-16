"""Validate a payment-receipt PDF against a booking.

Checks (in order):
  1. Readable + known bank (Kaspi/Halyk).
  2. Recipient is an accepted Dopshy account (payment_recipients table).
  3. Amount >= PAYMENT_MIN_FRACTION of the booking's full price.
  4. Receipt date within PAYMENT_RECEIPT_MAX_AGE_HOURS of now.

Returns {ok, code, reason (RU, user-facing), parsed}. Dedup of the receipt
number is enforced at insert time via the payments UNIQUE index.
"""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

import config
from integrations import booking_service
from integrations.receipt_parser import parse_receipt
from utils import now_almaty

logger = logging.getLogger(__name__)


def _reject(code: str, reason: str, parsed: dict) -> dict:
    return {"ok": False, "code": code, "reason": reason, "parsed": parsed}


def _recipient_matches(parsed: dict) -> bool:
    for r in booking_service.get_payment_recipients():
        if r["bank"] != parsed["bank"]:
            continue
        if r.get("bin") and parsed.get("bin") and r["bin"] == parsed["bin"]:
            return True
        if r.get("phone") and parsed.get("phone") and r["phone"] == parsed["phone"]:
            return True
        if r.get("name") and parsed.get("name") and r["name"].lower() in parsed["name"].lower():
            return True
    return False


def validate_receipt(booking: dict, pdf_bytes: bytes) -> dict:
    parsed = parse_receipt(pdf_bytes)

    if parsed["bank"] == "unknown" or parsed["amount"] is None:
        return _reject("unreadable",
                       "Не удалось распознать чек. Отправьте PDF-чек из Kaspi или Halyk.", parsed)

    if not _recipient_matches(parsed):
        return _reject("recipient",
                       "Платёж отправлен не на счёт Допши. Проверьте получателя.", parsed)

    price_total = booking.get("price_total")
    if price_total is not None:
        min_required = float(price_total) * config.PAYMENT_MIN_FRACTION
        if parsed["amount"] + 0.01 < min_required:
            return _reject(
                "amount",
                f"Сумма в чеке ({int(parsed['amount'])}₸) меньше необходимой "
                f"предоплаты ({int(min_required)}₸).",
                parsed,
            )

    if parsed["date"] is not None:
        receipt_dt = parsed["date"].replace(tzinfo=ZoneInfo(config.BOOKING_TIMEZONE))
        now = now_almaty()
        if (now - receipt_dt > timedelta(hours=config.PAYMENT_RECEIPT_MAX_AGE_HOURS)
                or receipt_dt - now > timedelta(minutes=10)):
            return _reject("date",
                           "Чек устарел или дата некорректна. Отправьте свежий чек.", parsed)

    return {"ok": True, "code": "OK", "reason": "", "parsed": parsed}
