"""OpenAI GPT-4o-mini integration."""
import json
from datetime import date

from openai import OpenAI
import config

_client = OpenAI(api_key=config.OPENAI_API_KEY)

def get_ai_response(
    phone_number_id: str,
    chat_id: str,
    user_message: str,
    history: list[dict],
    context: str,
) -> str:
    """
    Build the full prompt with RAG context and conversation history,
    then call GPT-4o-mini and return the assistant reply.
    """
    system_content = config.BOT_CONFIGS[phone_number_id]["system_prompt"]
    if context:
        system_content += f"\n\n--- База знаний / Білім базасы ---\n{context}\n---"

    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    response = _client.chat.completions.create(
        model=config.MODEL_NAME,
        messages=messages,
        temperature=0.6,
        max_tokens=512,
    )
    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# Booking-specific LLM helpers
# ---------------------------------------------------------------------------

def extract_booking_params(
    user_text: str,
    current_params: dict,
    free_slots_summary: str,
) -> dict:
    """
    Extract booking parameters from a user message.
    Returns a dict — fields not mentioned are set to null/None.
    Uses JSON mode for reliable structured output.
    """
    today = date.today().isoformat()
    prompt = (
        f"Today is {today} (YYYY-MM-DD). "
        f"Extract booking parameters from the user's message.\n"
        f"Already collected: {json.dumps(current_params, ensure_ascii=False, default=str)}\n"
        f"Available slots:\n{free_slots_summary}\n\n"
        f"User message: \"{user_text}\"\n\n"
        f"Return a JSON object with these fields (null for anything not mentioned or unclear):\n"
        f'{{"date":"YYYY-MM-DD or null",'
        f'"time_start":"HH:MM or null",'
        f'"format":"5x5 or 6x6 or null",'
        f'"players":integer_or_null,'
        f'"customer_name":"string or null",'
        f'"field":integer_or_null}}'
    )
    try:
        response = _client.chat.completions.create(
            model=config.MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=150,
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content)
    except Exception:
        return {}


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
