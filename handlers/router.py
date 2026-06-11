import os
import json
import logging
from typing import Union, List, Dict
from openai import OpenAI

from chat.tools.arena_tools import SELECT_INTENT_LLM

# Initialize the OpenAI client. It automatically picks up the OPENAI_API_KEY environment variable.
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# System prompt: the classifier must reason over the WHOLE history, not just the
# last line, so it can resolve pronouns ("book it", "that one") and detect whether
# the user is starting a booking vs. answering questions in an ongoing one.
SYSTEM_PROMPT = """You are an intent classifier for a WhatsApp football field rental bot.
Read the ENTIRE chat history as context — earlier turns disambiguate pronouns
("it", "that slot", "the same one") and reveal whether a booking flow is already
in progress. Classify ONLY the user's most recent message into exactly one intent:

- question_price: asking about cost, rates, or fees.
- question_slots: asking what times/dates are free or available.
- question_field_size: asking about field specs (5-a-side, 11-a-side, dimensions, surface).
- booking_new: a fresh, first-time intent to reserve a field (no booking underway yet).
- booking_continue: supplying details (date, time, name, duration) to an ALREADY ongoing booking.
- other: greetings, thanks, small talk, or anything unrelated.

When the prior turns show the bot asking for booking details, treat the user's reply
as booking_continue, not booking_new."""


def route_incoming_message(chat_history: Union[str, List[Dict[str, str]]]) -> str:
    """Classify the intent of the latest user message given full conversation context.

    Args:
        chat_history: Prior turns as either an array of dicts (preferred) or a single string.

    Returns:
        One of the strict enum intents; "other" on any failure.
    """
    # Accept either a structured message array or a raw string; normalize to messages
    # so role structure (and therefore flow context) is preserved for the model.
    if isinstance(chat_history, list):
        history_messages = chat_history
    else:
        history_messages = [{"role": "user", "content": str(chat_history or "")}]

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,  # deterministic classification
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history_messages,
            tools=[SELECT_INTENT_LLM],
            tool_choice={"type": "function", "function": {"name": "route_message"}}
        )

        # Safely reach into the tool call; any missing link defaults to "other".
        tool_calls = response.choices[0].message.tool_calls
        if not tool_calls:
            return "other"

        raw_args = tool_calls[0].function.arguments
        if not raw_args:
            return "other"

        data = json.loads(raw_args)
        return data.get("type", "other")

    except Exception as err:
        # Network errors, schema rejections, or malformed JSON all fall back gracefully.
        logging.error(f"route_incoming_message failed: {err}")
        return "other"