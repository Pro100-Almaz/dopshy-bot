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
    "experience": None,
    "school_shift": None,
    "preferred_date": None,
    "preferred_weekday": None,
    "preferred_time_start": None,
    "preferred_time_end": None,
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
    return result


def extract_trial_details(history: list[dict[str, str]], user_text: str) -> dict[str, Any]:
    """Extract academy trial intake fields and schedule preferences.

    The returned preferred_* values are user wishes only. The gated trial flow
    must validate them against eligible groups before assigning trial_day,
    start_time, end_time, or group_id.
    """
    now = now_almaty()
    prompt = _DATA_EXTRACT_PROMPT_TEMPLATE.format(
        today=today_almaty().isoformat(),
        now=now.strftime("%Y-%m-%d %H:%M"),
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
            return _with_deterministic_weekday({}, user_text)

        raw_args = tool_calls[0].function.arguments
        if not raw_args:
            return _with_deterministic_weekday({}, user_text)

        parsed = json.loads(raw_args)
        return _with_deterministic_weekday(parsed, user_text)

    except Exception as err:
        logging.error("extract_trial_details failed: %s", err)
        return _with_deterministic_weekday({}, user_text)
