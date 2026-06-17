"""OpenAI GPT-4o-mini integration."""
import config
import json
import logging

from openai import OpenAI

from chat.conversation import Message
from chat.system_prompts.sp_1 import INTENT_PROMPT
from chat.tools.arena_tools import EDIT_BOOKING_TOOL, START_BOOKING_TOOL, SELECT_INTENT_LLM
from chat.tools.academy_tools import START_TRIAL_TOOL, EDIT_TRIAL_TOOL, CANCEL_TRIAL_TOOL

logger = logging.getLogger(__name__)

_client = OpenAI(api_key=config.OPENAI_API_KEY)


def get_ai_response(
        phone_number_id: str,
        chat_id: str,
        user_message: str,
        history: list[Message],
        context: str,
) -> tuple[str, dict | None]:
    """
    Build the full prompt with RAG context and conversation history,
    then call GPT-4o-mini.

    Returns:
        (reply_text, tool_call)
        tool_call is None if the LLM produced a plain reply, otherwise a dict
        of shape {"name": "start_booking" | "edit_booking", "args": {...}}.
        The handler dispatches on `name` to launch the corresponding flow.
    """
    is_dopsy = config.BOT_CONFIGS[phone_number_id]["name"] == "dopsy_bot"
    system_content = config.BOT_CONFIGS[phone_number_id]["system_prompt"]

    # Inject factual field list for Bot 1 so the LLM never hallucinates field formats
    if is_dopsy and config.BOOKING_FIELDS:
        fields_lines = "\n".join(
            f"  - Поле {f['id']}: формат {f['format']}"
            for f in config.BOOKING_FIELDS
        )
        system_content += f"\n\n--- Наши поля ---\n{fields_lines}\n---"

    if context:
        system_content += f"\n\n--- База знаний / Білім базасы ---\n{context}\n---"

    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    kwargs = dict(
        model=config.MODEL_NAME,
        messages=messages,
        temperature=0.2 if is_dopsy else 0.6,
        max_tokens=512,
    )
    if is_dopsy:
        kwargs["tools"] = [START_BOOKING_TOOL, EDIT_BOOKING_TOOL]
        kwargs["tool_choice"] = "auto"
    else:
        kwargs["tools"] = [START_TRIAL_TOOL, EDIT_TRIAL_TOOL, CANCEL_TRIAL_TOOL]
        kwargs["tool_choice"] = "auto"

    response = _client.chat.completions.create(**kwargs)
    msg = response.choices[0].message
    preamble = (msg.content or "").strip()

    if msg.tool_calls:
        for tc in msg.tool_calls:
            name = tc.function.name
            if name in ("start_booking", "edit_booking", "start_trial", "edit_trial", "cancel_trial"):
                args: dict = {}
                if name in ("edit_booking", "edit_trial"):
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        logger.warning(
                            "[LLM] edit_booking returned unparseable arguments: %r",
                            tc.function.arguments,
                        )
                        args = {}
                return preamble, {"name": name, "args": args}

    return preamble, None


def get_booking_reply(
        user_text: str,
        booking_context: str,
        system_hint: str = "",
) -> str:
    """
    Generate a natural-language reply for booking-related queries.
    Responds in the same language the user wrote in (Russian or Kazakh).
    """
    system_content = (
        "Ты — ассистент по бронированию футбольных полей «Допши». "
        "Всегда отвечай на том языке, на котором написал пользователь (русский или казахский). "
        "Будь кратким и дружелюбным. Не придумывай информацию. "
        "ВСЕГДА обращайся к клиенту только на «вы», никогда не переходи на «ты», "
        "даже если клиент пишет неформально. "
        "Клиентке ӘРҚАШАН тек «сіз» деп қарата сөйле, ешқашан «сен» деп ауыспа, "
        "клиент бейресми жазса да."
    )
    if system_hint:
        system_content += f"\n\nИнструкция: {system_hint}"
    if booking_context:
        system_content += f"\n\n--- Данные о бронировании ---\n{booking_context}\n---"

    response = _client.chat.completions.create(
        model=config.MODEL_NAME,
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_text},
        ],
        temperature=0.5,
        max_tokens=400,
    )
    return response.choices[0].message.content.strip()


def route_incoming_message(history: list, user_message: str) -> str:
    """Classify the intent of the latest user message given full conversation context.

    Args:
        chat_history: Prior turns as either an array of dicts (preferred) or a single string.

    Returns:
        One of the strict enum intents; "other" on any failure.
    """
    # Accept either a structured message array or a raw string; normalize to messages
    # so role structure (and therefore flow context) is preserved for the model.
    messages = [{"role": "system", "content": INTENT_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    try:
        response = _client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,  # deterministic classification
            messages=messages,
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
        logging.error(f"route_incoming_message failed: {err}")
        return "other"
