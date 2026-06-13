import json
import logging
from typing import List, Dict, Any
from openai import OpenAI
import config
from chat.system_prompts.sp_1 import get_data_extract_prompt
from chat.tools.arena_tools import EXTRACT_DATA_LLM

client = OpenAI(api_key = config.OPENAI_API_KEY)


def extract_booking_details(history: List[Dict[str, str]], user_text: str) -> Dict[str, Any]:
    """Extract the 6 booking parameters from the conversation.

    Args:
        chat_history: Prior turns as either an array of dicts (preferred) or a single string.

    Returns:
        A dictionary containing date, start_time, end_time, field_size, players, and name.
    """
    messages = [{"role": "system", "content": get_data_extract_prompt()}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    empty_result = {
        "date": None,
        "time_start": None,
        "time_end": None,
        "field": None,
        "players": None,
        "name": None
    }

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=messages,
            tools=[EXTRACT_DATA_LLM],
            tool_choice={"type": "function", "function": {"name": "extract_booking_data"}}
        )

        tool_calls = response.choices[0].message.tool_calls
        if not tool_calls:
            return empty_result

        raw_args = tool_calls[0].function.arguments
        if not raw_args:
            return empty_result

        # Merge over the empty template so any absent key is guaranteed to be None.
        parsed_args = json.loads(raw_args)
        return {**empty_result, **parsed_args}

    except Exception as err:
        logging.error(f"extract_booking_details failed: {err}")
        return empty_result

