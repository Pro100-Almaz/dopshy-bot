import json
import logging
from typing import Any

import config
from chat.system_prompts.sp_2 import _DATA_EXTRACT_PROMPT_TEMPLATE
from chat.tools.academy_tools import EXTRACT_TRIAL_DATA_LLM
from utils import closest_named_weekday_date, now_almaty, parse_weekdays, today_almaty

from chat.openai_client import client


EMPTY_TRIAL_DATA = {
    "child_name": None,
    "child_birth_year": None,
    "audience": None,
    "experience": None,
    "school_shift": None,
    "preferred_date": None,
    "preferred_weekday": None,
    "preferred_time_start": None,
    "preferred_time_end": None,
    "preferred_weekdays": None,
    "preferred_trainers": None,
}


# The question the bot is waiting on, so a short answer is read as its answer.
_STEP_HINTS = {
    "audience": "Бот сейчас спросил, в какую группу записаться: ответ клиента — это audience.",
    "child_name": (
        "Бот сейчас спросил имя ученика: имя в ответе клиента — это child_name, в том числе "
        "с вводными словами («Есімім Әсет» → «Әсет», «меня зовут Алихан» → «Алихан», "
        "«Мен Айдоспын» → «Айдос»)."
    ),
    "child_birth_year": (
        "Бот сейчас спросил возраст или год рождения ученика: ответ клиента — это child_birth_year, "
        "даже если возраст не подходит ни одной группе."
    ),
    "experience": "Бот сейчас спросил уровень подготовки: ответ клиента — это experience.",
    "school_shift": "Бот сейчас спросил школьную смену: ответ клиента — это school_shift.",
}


def _with_deterministic_weekday(data: dict[str, Any], user_text: str) -> dict[str, Any]:
    """Fill weekday preferences the LLM omitted, without replacing explicit dates."""
    result = {**EMPTY_TRIAL_DATA, **data}
    weekdays = parse_weekdays(user_text)
    weekday_date = closest_named_weekday_date(user_text)
    if weekday_date:
        if result.get("preferred_date") is None:
            result["preferred_date"] = weekday_date.isoformat()
        if result.get("preferred_weekday") is None:
            result["preferred_weekday"] = weekdays[0]
        if not result.get("preferred_weekdays"):
            result["preferred_weekdays"] = weekdays
    return result


def _failed_extraction(user_text: str) -> dict[str, Any]:
    """No reading of the message — "failed" tells it from a message with no data."""
    return {**_with_deterministic_weekday({}, user_text), "failed": True}


def extract_trial_details(
    history: list[dict[str, str]], user_text: str, answering: str | None = None,
    wish: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract academy trial intake fields and schedule preferences.

    `answering` is the signup step the bot is waiting on, if any; `wish` what
    the client already wishes for, so a later message only changes that.

    The returned preferred_* values are user wishes only. The gated trial flow
    must validate them against eligible groups before assigning trial_day,
    start_time, end_time, or group_id.
    """
    now = now_almaty()
    prompt = _DATA_EXTRACT_PROMPT_TEMPLATE.format(
        today=today_almaty().isoformat(),
        now=now.strftime("%Y-%m-%d %H:%M"),
    )
    hint = _STEP_HINTS.get(answering)
    if hint:
        prompt = f"{prompt}\n\n{hint}"
    stored = {key: value for key, value in (wish or {}).items() if value is not None}
    if stored:
        prompt = (
            f"{prompt}\n\nУже сохранённые пожелания клиента: {json.dumps(stored, ensure_ascii=False)}. "
            "Верни их в тех же полях без изменений; меняй только то, что клиент изменил или снял "
            "в последнем сообщении."
        )
    messages = [
        {
            "role": "system",
            "content": prompt,
        }
    ]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    try:
        response = client.chat.completions.create(
            model=config.EXTRACTOR_MODEL,
            temperature=0,
            messages=messages,
            tools=[EXTRACT_TRIAL_DATA_LLM],
            tool_choice={"type": "function", "function": {"name": "extract_trial_data"}},
        )

        tool_calls = response.choices[0].message.tool_calls
        if not tool_calls:
            return _failed_extraction(user_text)

        raw_args = tool_calls[0].function.arguments
        if not raw_args:
            return _failed_extraction(user_text)

        parsed = json.loads(raw_args)
        return _with_deterministic_weekday(parsed, user_text)

    except Exception as err:
        logging.error("extract_trial_details failed: %s", err)
        return _failed_extraction(user_text)
