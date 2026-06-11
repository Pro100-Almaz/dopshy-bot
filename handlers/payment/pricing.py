from datetime import datetime


def process_field_prices(all_prices: list):
    if not all_prices:
        return 'Пока цены не указаны.'
    message = '💰 Наши тарифы:\n\n'
    price_elems = {}

    # building the hashmap with the format dicts that contain the pricing information
    #{ '5x5' : {
    #       'morning_day' : 20000,
    #           }
    #       }
    for price_elem in all_prices:
        format_name = price_elem['format_name']
        pricing_type = price_elem['pricing_type']
        price_per_hour = price_elem['price_per_hour']

        if format_name not in price_elems:
            price_elems[format_name] = {}
        price_elems[format_name][pricing_type] = price_per_hour

    for format_name, prices in price_elems.items():
        message += f"⚽ Поле {format_name}:\n"

        for pricing_type, price in prices.items():
            message += f"  • {pricing_type}: {price:,} тг/час\n".replace(",", " ")

        message += "\n"

    return message.strip()

