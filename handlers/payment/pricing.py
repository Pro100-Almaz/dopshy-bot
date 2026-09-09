from datetime import date, datetime, time, timedelta

import config
from handlers.payment.pricing_repo import get_total_field_prices, get_prices_for_format

PRICING_TYPE_LABELS = {
    "ru": {
        "morning_day": "Утро / день",
        "evening": "Вечер",
        "late_night": "Поздний вечер",
        "after_midnight": "После полуночи",
        "weekend_holiday": "Выходные / праздники",
    },
    "kk": {
        "morning_day": "Таңғы / күндізгі",
        "evening": "Кешкі",
        "late_night": "Кеш түнгі",
        "after_midnight": "Түн ортасынан кейін",
        "weekend_holiday": "Демалыс / мереке",
    },
}

PRICING_TYPE_RU = PRICING_TYPE_LABELS["ru"]

PRICING_TIME_RANGE = {
    "morning_day": "07:00 – 18:30",
    "evening": "18:30 – 22:00",
    "late_night": "22:00 – 00:00",
    "after_midnight": "00:00 – 07:00",
    "weekend_holiday": {
        "ru": "весь день",
        "kk": "күні бойы",
    },
}

# (pricing_type, start_minute, end_minute) — covers the full 24 h
PRICING_PERIODS = [
    ("after_midnight", 0,    420),   # 00:00 – 07:00
    ("morning_day",    420,  1110),  # 07:00 – 18:30
    ("evening",        1110, 1320),  # 18:30 – 22:00
    ("late_night",     1320, 1440),  # 22:00 – 24:00
]


def _to_minutes(t) -> int:
    """Convert a time/str to minutes-from-midnight.  23:59:59 → 1440 (midnight)."""
    if isinstance(t, str):
        parts = t.split(":")
        h, m = int(parts[0]), int(parts[1])
        s = int(parts[2]) if len(parts) > 2 else 0
    elif isinstance(t, time):
        h, m, s = t.hour, t.minute, t.second
    else:
        raise ValueError(f"Cannot convert {t!r} to minutes")
    if h == 23 and m == 59 and s >= 59:
        return 1440
    return h * 60 + m


def _to_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d), "%Y-%m-%d").date()


def _is_weekend_or_holiday(d) -> bool:
    d = _to_date(d)
    if d.weekday() in (5, 6):
        return True
    return d in config.HOLIDAYS


def calculate_booking_price(format_name: str, booking_date,
                            time_start, time_end) -> float:
    """Return the total price (tenge) for one booking segment.

    For transitive (midnight-crossing) bookings each half is a separate
    segment — call this function once per half.
    """
    prices = get_prices_for_format(format_name)

    start_min = _to_minutes(time_start)
    end_min = _to_minutes(time_end)
    if end_min <= start_min:
        end_min = 1440

    if _is_weekend_or_holiday(booking_date):
        duration_hours = (end_min - start_min) / 60.0
        return round(duration_hours * prices.get("weekend_holiday", 0), 2)

    total = 0.0
    for period_type, period_start, period_end in PRICING_PERIODS:
        overlap = max(0, min(end_min, period_end) - max(start_min, period_start))
        if overlap > 0:
            total += (overlap / 60.0) * prices.get(period_type, 0)

    return round(total, 2)


def calculate_full_booking_price(format_name: str, booking_date,
                                  time_start, time_end) -> float:
    """Total price for a logical booking, including both halves of a
    midnight-crossing (transitive) booking."""
    start_min = _to_minutes(time_start)
    end_min = _to_minutes(time_end)
    if start_min >= end_min and end_min != 0:
        d = _to_date(booking_date)
        p1 = calculate_booking_price(format_name, d, time_start, "23:59:59")
        p2 = calculate_booking_price(format_name, d + timedelta(days=1),
                                     "00:00", time_end)
        return round(p1 + p2, 2)
    return calculate_booking_price(format_name, booking_date, time_start, time_end)


def fmt_price(amount) -> str:
    return f"{int(amount):,} тг".replace(",", " ")


_PRICES_I18N = {
    "ru": {
        "empty": "Пока цены не указаны.",
        "header": "💰 Наши тарифы:",
        "per_hour": "тг/час",
    },
    "kk": {
        "empty": "Әзірге бағалар көрсетілмеген.",
        "header": "💰 Біздің тарифтер:",
        "per_hour": "тг/сағ",
    },
}


def process_field_prices(lang: str = "ru") -> str:
    all_prices = get_total_field_prices()
    if not all_prices:
        return _PRICES_I18N[lang]["empty"]

    labels = PRICING_TYPE_LABELS.get(lang, PRICING_TYPE_LABELS["ru"])
    strings = _PRICES_I18N.get(lang, _PRICES_I18N["ru"])
    message = f'{strings["header"]}\n\n'
    price_elems = {}

    for price_elem in all_prices:
        format_name = price_elem['format_name']
        pricing_type = price_elem['pricing_type']
        price_per_hour = price_elem['price_per_hour']

        if format_name not in price_elems:
            price_elems[format_name] = {}
        price_elems[format_name][pricing_type] = price_per_hour

    for format_name, prices in price_elems.items():
        message += f"⚽ *{format_name}*\n"

        for pricing_type, price in prices.items():
            label = labels[pricing_type]
            time_range = PRICING_TIME_RANGE[pricing_type]
            if isinstance(time_range, dict):
                time_range = time_range.get(lang, time_range["ru"])
            formatted_price = f"{price:,.0f}".replace(",", " ")
            message += f"▸ {label} ({time_range}) — *{formatted_price} {strings['per_hour']}*\n"

        message += "\n"

    return message.strip()
