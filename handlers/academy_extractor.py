import json
import logging
from typing import Any

from openai import OpenAI

import config
from chat.tools.academy_tools import EXTRACT_TRIAL_DATA_LLM

client = OpenAI(api_key=config.OPENAI_API_KEY)


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


def extract_trial_details(history: list[dict[str, str]], user_text: str) -> dict[str, Any]:
    """Extract academy trial intake fields and schedule preferences.

    The returned preferred_* values are user wishes only. The gated trial flow
    must validate them against eligible groups before assigning trial_day,
    start_time, end_time, or group_id.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "Extract trial signup data from the conversation. "
                "Do not infer missing personal data. "
                "A requested day/time is only a preference, never a confirmed slot. "
                "Normalize dates to YYYY-MM-DD and times to HH:MM. "
                "Use weekday numbers 0=Monday through 6=Sunday."
            ),
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
            return dict(EMPTY_TRIAL_DATA)

        raw_args = tool_calls[0].function.arguments
        if not raw_args:
            return dict(EMPTY_TRIAL_DATA)

        parsed = json.loads(raw_args)
        return {**EMPTY_TRIAL_DATA, **parsed}

    except Exception as err:
        logging.error("extract_trial_details failed: %s", err)
        return dict(EMPTY_TRIAL_DATA)
