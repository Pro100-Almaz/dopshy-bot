import json
import logging
from typing import List, Dict, Any
import config
from chat.system_prompts.sp_1 import get_data_extract_prompt
from chat.system_prompts.sp_academy import get_trial_data_extract_prompt
from chat.tools.arena_tools import EXTRACT_DATA_LLM
from chat.tools.academy_tools import EXTRACT_TRIAL_DATA_LLM

from chat.openai_client import client


def _extract(prompt: str, tool: dict, empty: Dict[str, Any],
             history: List[Dict[str, str]], user_text: str,
             label: str) -> Dict[str, Any]:
    """Run one forced-tool extraction call and return its arguments.

    Shared by the arena and academy extractors: same call shape, different
    prompt/tool/field-set. `empty` doubles as the failure value and as the
    template the parsed arguments are merged over, so a key the model omits
    is guaranteed to come back as None rather than be missing.
    """
    messages = [{"role": "system", "content": prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    try:
        response = client.chat.completions.create(
            model=config.EXTRACTOR_MODEL,
            temperature=0,
            messages=messages,
            tools=[tool],
            tool_choice={"type": "function",
                         "function": {"name": tool["function"]["name"]}}
        )

        tool_calls = response.choices[0].message.tool_calls
        if not tool_calls:
            return dict(empty)

        raw_args = tool_calls[0].function.arguments
        if not raw_args:
            return dict(empty)

        parsed_args = json.loads(raw_args)
        return {**empty, **parsed_args}

    except Exception as err:
        logging.error(f"{label} failed: {err}")
        return dict(empty)


def extract_booking_details(history: List[Dict[str, str]], user_text: str) -> Dict[str, Any]:
    """Extract the 6 booking parameters from the conversation (arena, Bot 1)."""
    return _extract(
        get_data_extract_prompt(),
        EXTRACT_DATA_LLM,
        {
            "date": None,
            "time_start": None,
            "time_end": None,
            "field": None,
            "players": None,
            "name": None,
        },
        history, user_text, "extract_booking_details",
    )


def extract_trial_details(history: List[Dict[str, str]], user_text: str) -> Dict[str, Any]:
    """Extract the 4 trial-signup parameters from the conversation (academy bots).

    No time_end and no group: the class the parent picks supplies both, so the
    flow derives them from (date, time_start) rather than extracting them.
    """
    return _extract(
        get_trial_data_extract_prompt(),
        EXTRACT_TRIAL_DATA_LLM,
        {
            "date": None,
            "time_start": None,
            "child_name": None,
            "child_age": None,
        },
        history, user_text, "extract_trial_details",
    )
