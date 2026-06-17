# Tool definition for Bot 1: LLM calls this instead of emitting a text tag
START_BOOKING_TOOL = {
    "type": "function",
    "function": {
        "name": "start_booking",
        "description": (
            "Запустить пошаговый процесс бронирования футбольного поля. "
            "ВЫЗЫВАЙ эту функцию при ЛЮБОМ намёке на желание забронировать, арендовать, "
            "снять поле, занять время или просто прийти поиграть — даже если пользователь "
            "ещё не уверен, не назвал дату/время или прислал только часть деталей. "
            "Лучше вызвать функцию лишний раз, чем пропустить намерение: пошаговый процесс "
            "сам спросит всё необходимое и сам сообщит, если бронировать сейчас нельзя. "
            "Не пытайся собрать дату/время/состав в свободном тексте до вызова функции. "
            "НЕ вызывай эту функцию, если пользователь хочет ИЗМЕНИТЬ уже существующую бронь — "
            "для этого есть edit_booking."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

# Tool definition for Bot 1: LLM extracts the diff for editing an existing
# booking. The backend (booking_service.client_edit_booking) is the only place
# that enforces the 48h window + once-only + slot-clash rules — this tool's
# job is purely extraction, not policy.
EDIT_BOOKING_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_booking",
        "description": (
            "Изменить детали уже существующей подтверждённой или ожидающей оплаты брони. "
            "ВЫЗЫВАЙ эту функцию, когда пользователь хочет ПЕРЕНЕСТИ время/дату, СМЕНИТЬ "
            "поле, изменить число игроков или своё имя в активной брони "
            "(например: «перенесите на пятницу в 18:00», «давайте на другое поле», "
            "«нас будет 10», «измените имя на Алмат»). "
            "Заполняй ТОЛЬКО те параметры, которые пользователь действительно изменил — "
            "остальные оставь пустыми, бэкенд возьмёт значения из текущей брони. "
            "Если деталей нет вообще (например, «перенесите мою бронь» без новой даты), "
            "всё равно вызывай функцию с пустыми параметрами — бот сам спросит детали. "
            "Не пытайся сам проверить, можно ли редактировать (правило 48 часов и т.п.) — "
            "это сделает бэкенд и вернёт понятную ошибку."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Новая дата в формате YYYY-MM-DD. Опускай, если дата не меняется.",
                },
                "time_start": {
                    "type": "string",
                    "description": "Новое время начала в формате HH:MM (24h). Опускай, если время не меняется.",
                },
                "time_end": {
                    "type": "string",
                    "description": "Новое время окончания в формате HH:MM (24h). Опускай, если время не меняется.",
                },
                "field": {
                    "type": "integer",
                    "description": "Новый номер поля (1, 2, …). Опускай, если поле не меняется.",
                },
                "players": {
                    "type": "integer",
                    "description": "Новое количество игроков. Опускай, если число игроков не меняется.",
                },
                "customer_name": {
                    "type": "string",
                    "description": "Новое имя клиента. Опускай, если имя не меняется.",
                },
            },
            "required": [],
        },
    },
}

EXTRACT_DATA_LLM = {
    "type": "function",
    "function": {
        "name": "extract_booking_data",
        "description": "Extract the 6 required variables for a field booking.",
        # strict mode requires `additionalProperties: false` AND every property
        # key present in `required` (even nullable ones).
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "date": {
                    "type": ["string", "null"],
                    "description": "Format YYYY-MM-DD"
                                   "Return null unless the user explicitly states a date."
                },
                "time_start": {
                    "type": ["string", "null"],
                    "description": "Format HH:MM"
                                   "Return null unless the user explicitly states a time."
                },
                "time_end": {
                    "type": ["string", "null"],
                    "description": "Format HH:MM"
                                    "Return null unless the user explicitly states a time."
                },
                "field": {
                    "type": ["string", "null"],
                    "enum": ["5x5", "6x6", None]
                },
                "players": {
                    "type": ["number", "null"],
                    "description": "The total number of players expected. "
                                   "Return null unless the user explicitly states a number."
                },
                "name": {
                    "type": ["string", "null"],
                    "description": "The name of the booker"
                                   "Return null unless the user explicitly states a name."
                }
            },
            "required": ["date", "time_start", "time_end", "field", "players", "name"]
        }
    }
}

SELECT_INTENT_LLM = {
    "type": "function",
    "function": {
        "name": "route_message",
        "description": "Return the single categorized intent of the latest user message.",
        # strict mode requires `additionalProperties: False` and every property
        # listed in `required` — otherwise the API rejects the request.
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [
                        "question_price",
                        # "question_slots",
                        # "question_field_size",
                        "booking_new",
                        "booking_continue",
                        "booking_cancel",
                        "other"
                    ],
                    "description": "The categorized intent of the message."
                }
            },
            "required": ["type"]
        }
    }
}
