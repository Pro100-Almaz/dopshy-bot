from datetime import datetime
PRICING_TYPE_RU = {
    "morning_day": "Утро / день",
    "evening": "Вечер",
    "late_night": "Поздний вечер",
    "after_midnight": "После полуночи",
    "weekend_holiday": "Выходные / праздники",
}

from handlers.payment.pricing_repo import get_total_field_prices


PRICING_TYPE_RU = {
    "morning_day": "Утро / день",
    "evening": "Вечер",
    "late_night": "Поздний вечер",
    "after_midnight": "После полуночи",
    "weekend_holiday": "Выходные / праздники",
}

def process_field_prices() -> str:
    all_prices = get_total_field_prices()
    if not all_prices:
        return 'Пока цены не указаны.'
    message = '💰 Наши тарифы:\n\n'
    price_elems = {}

    for price_elem in all_prices:
        format_name = price_elem['format_name']
        pricing_type = price_elem['pricing_type']
        price_per_hour = price_elem['price_per_hour']

        if format_name not in price_elems:
            price_elems[format_name] = {}
        price_elems[format_name][pricing_type] = price_per_hour

    for format_name, prices in price_elems.items():
        message += f"⚽ {format_name}:\n"

        for pricing_type, price in prices.items():
            message += f"  • {PRICING_TYPE_RU[pricing_type]}: {price:,} тг/час\n".replace(",", " ")

        message += "\n"

    return message.strip()

