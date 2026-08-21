START_TRIAL_TOOL = {
    "type": "function",
    "function": {
        "name": "start_trial",
        "description": (
            "Запустить пошаговый процесс регистрации на пробное занятие. "
            "ВЫЗЫВАЙ эту функцию при ЛЮБОМ намёке на желание попробовать, участвовать, "
            "заняться, учиться, записаться или просто прийти на занятие — ДАЖЕ если пользователь "
            "ещё не уверен, не назвал дату/время или прислал только часть деталей. "
            "Лучше вызвать функцию лишний раз, чем пропустить намерение: пошаговый процесс "
            "сам спросит всё необходимое и сам сообщит, если записаться на занятие сейчас нельзя. "
            "НЕ пытайся СОБРАТЬ дату/время/личную информацию в свободном тексте ДО вызова функции. "
            "Ты должен сперва только узнать намерение "
            "и вызывать эту функцию если есть намерение записаться на пробный урок/занятие"
            "НЕ вызывай эту функцию, если пользователь хочет ИЗМЕНИТЬ, ОТМЕНИТЬ уже существующую запись — "
            "для этого есть cancel_booking."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


SELECT_TRIAL_INTENT_LLM = {
    "type": "function",
    "function": {
        "name": "route_trial_message",
        "description": (
            "Classify the latest message for an academy trial/QA WhatsApp bot. "
            "Return exactly one intent and the user's language."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [
                        "question_price",
                        "question_schedule",
                        "question_location",
                        "question_age",
                        "question_trial_rules",
                        "trial_new",
                        "trial_continue",
                        "trial_edit",
                        "trial_status",
                        "trial_cancel",
                        "human_help",
                        "other",
                    ],
                    "description": "Single best intent for the user's latest message.",
                },
                "lang": {
                    "type": "string",
                    "enum": ["ru", "kk"],
                    "description": "Language of the user's latest message.",
                },
            },
            "required": ["type", "lang"],
        },
    },
}


EXTRACT_TRIAL_DATA_LLM = {
    "type": "function",
    "function": {
        "name": "extract_trial_data",
        "description": (
            "Extract structured intake data and preferred schedule from a trial "
            "signup conversation. Preferences are not final group/date/time choices."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "child_name": {
                    "type": ["string", "null"],
                    "description": "Child's name. Null unless explicitly provided.",
                },
                "child_birth_year": {
                    "type": ["integer", "null"],
                    "description": "Four-digit child birth year. Null unless explicit.",
                },
                "experience": {
                    "type": ["string", "null"],
                    "enum": ["Beginner", "Intermediate", "Advanced", None],
                    "description": (
                        "Training level. Map beginner/новичок/бастапқы to Beginner, "
                        "intermediate/средний/орта to Intermediate, advanced/продвинутый/жоғары to Advanced."
                    ),
                },
                "school_shift": {
                    "type": ["string", "null"],
                    "enum": ["morning", "afternoon", None],
                    "description": (
                        "Child's school shift, not desired training time. "
                        "morning means studies in the morning; afternoon means studies in the afternoon."
                    ),
                },
                "preferred_date": {
                    "type": ["string", "null"],
                    "description": "Preferred trial date as YYYY-MM-DD, or null.",
                },
                "preferred_weekday": {
                    "type": ["integer", "null"],
                    "description": "Preferred weekday 0=Monday through 6=Sunday, or null.",
                },
                "preferred_time_start": {
                    "type": ["string", "null"],
                    "description": "Preferred start time HH:MM, or null.",
                },
                "preferred_time_end": {
                    "type": ["string", "null"],
                    "description": "Preferred end time HH:MM, or null.",
                },
            },
            "required": [
                "child_name",
                "child_birth_year",
                "experience",
                "school_shift",
                "preferred_date",
                "preferred_weekday",
                "preferred_time_start",
                "preferred_time_end",
            ],
        },
    },
}


EDIT_TRIAL_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_trial",
        "description": (
            "Изменить детали уже существующей записи на пробное занятие. "
            "ВЫЗЫВАЙ эту функцию, когда пользователь хочет ПЕРЕНЕСТИ время/дату, ИЗМЕНИТЬ "
            "информацию пользователя например имя/возраст ребенка, смену в школе, или ИЗМЕНИТЬ время записи на пробное занятие "
            "(например: «перенесите на пятницу в 18:00», «давайте на другой день», «измените имя на Алмат»). "
            "Заполняй ТОЛЬКО те параметры, которые пользователь действительно изменил — "
            "остальные оставь пустыми, бэкенд возьмёт значения из текущей записи. "
            "Если деталей нет вообще (например, «перенесите мое занятие» без новой даты), "
            "всё равно вызывай функцию с пустыми параметрами — бот сам спросит детали. "
            "Не пытайся сам проверить, можно ли редактировать (правило 48 часов и т.п.) — "
            "это сделает бэкенд и вернёт понятную ошибку."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "trial_day": {
                    "type": "integer",
                    "description": "Новый день недели в формате чисел, например Понедельник - 1, Вторник - 2 и так далее. Опускай, если дата не меняется.",
                },
                "trial_time": {
                    "type": "string",
                    "description": "Новое время начала в формате HH:MM (24h). Опускай, если время не меняется.",
                },
                "shift": {
                    "type": "string",
                    "description": "Новое значение смены учебы ребенка клиента. Опускай, если смена учебы не меняется.",
                },
                "child_birth_year": {
                    "type": "integer",
                    "description": "Новый год рождения ребенка клиента. Опускай, если год рождения не меняется.",
                },
                "child_name": {
                    "type": "string",
                    "description": "Новое имя ребенка клиента. Опускай, если имя не меняется.",
                },
            },
            "required": [],
        },
    },
}


CANCEL_TRIAL_TOOL = {
    "type": "function",
    "function": {
        "name": "cancel_trial",
        "description": (
            "ОТМЕНИТЬ уже существующую запись на пробное занятие. "
            "ВЫЗЫВАЙ эту функцию, когда пользователь хочет ОТМЕНИТЬ пробное занятие. "
            "Не собирай дополнительную информацию и не спрашивай конфирмацию, бэкенд сам начнет процесс отмены занятий. "
            "Ты должен просто запустить процесс и оставить все бэкенду."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}
