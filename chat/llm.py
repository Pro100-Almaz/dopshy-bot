"""OpenAI GPT-4o-mini integration."""
from openai import OpenAI
import config

_client = OpenAI(api_key=config.OPENAI_API_KEY)

# Tool definition for Bot 1: LLM calls this instead of emitting a text tag
_START_BOOKING_TOOL = {
    "type": "function",
    "function": {
        "name": "start_booking",
        "description": (
            "Запустить пошаговый процесс бронирования футбольного поля. "
            "Вызывай эту функцию как только пользователь выражает желание забронировать поле — "
            "даже если он уже прислал дату, время или другие детали."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def get_ai_response(
    phone_number_id: str,
    chat_id: str,
    user_message: str,
    history: list[dict],
    context: str,
) -> tuple[str, bool]:
    """
    Build the full prompt with RAG context and conversation history,
    then call GPT-4o-mini.

    Returns:
        (reply_text, start_booking)
        start_booking=True means the LLM called the start_booking tool.
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
        temperature=0.6,
        max_tokens=512,
    )
    if is_dopsy:
        kwargs["tools"] = [_START_BOOKING_TOOL]
        kwargs["tool_choice"] = "auto"

    response = _client.chat.completions.create(**kwargs)
    msg = response.choices[0].message

    # LLM decided to start the booking flow
    if msg.tool_calls and any(tc.function.name == "start_booking" for tc in msg.tool_calls):
        # Use the LLM's text content as a preamble if it provided one
        preamble = (msg.content or "").strip()
        return preamble, True

    return (msg.content or "").strip(), False


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
        "Будь кратким и дружелюбным. Не придумывай информацию."
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
