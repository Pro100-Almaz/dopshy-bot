import os
import json
import logging
from typing import Union, List, Dict, Any
from openai import OpenAI
import config

# Initialize the OpenAI client. It automatically picks up the OPENAI_API_KEY environment variable.
client = OpenAI(api_key = config.OPENAI_API_KEY)

# The extractor reads the whole session log and pulls structured booking data.
# The critical instruction is the null contract: anything not clearly stated must
# be null (None), never guessed — downstream code relies on null to know what's missing.
SYSTEM_PROMPT = """You extract football field booking details from a WhatsApp conversation.
Return exactly 6 parameters. For ANY parameter that is completely missing, not yet
mentioned, or ambiguous in the text, you MUST set its value to literal JSON null.
Never invent, infer, or guess a value to fill a gap — when in doubt, return null.
Normalize dates to YYYY-MM-DD and times to 24-hour HH:MM."""


def extract_booking_details(chat_history: Union[str, List[Dict[str, str]]]) -> Dict[str, Any]:
    """Extract the 6 booking parameters from the conversation.

    Args:
        chat_history: Prior turns as either an array of dicts (preferred) or a single string.

    Returns:
        A dictionary containing date, start_time, end_time, field_size, players, and name.
    """
    if isinstance(chat_history, list):
        history_messages = chat_history
    else:
        history_messages = [{"role": "user", "content": str(chat_history or "")}]

    # Returned when extraction can't run — keeps the null contract intact for callers.
    empty_result = {
        "date": None,
        "start_time": None,
        "end_time": None,
        "field_size": None,
        "players": None,
        "name": None
    }

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history_messages,
            tools=[{
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
                            "date": {"type": ["string", "null"], "description": "Format YYYY-MM-DD"},
                            "start_time": {"type": ["string", "null"], "description": "Format HH:MM"},
                            "end_time": {"type": ["string", "null"], "description": "Format HH:MM"},
                            "field_size": {"type": ["string", "null"], "enum": ["5x5", "6x6", None]},
                            "players": {"type": ["number", "null"],
                                        "description": "The total number of players expected"},
                            "name": {"type": ["string", "null"], "description": "The name of the booker"}
                        },
                        "required": ["date", "start_time", "end_time", "field_size", "players", "name"]
                    }
                }
            }],
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