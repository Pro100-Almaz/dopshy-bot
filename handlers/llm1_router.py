"""
LLM1 — Lightweight Intent Router and Data Extractor.

First stage of the two-LLM architecture. Uses gpt-4o-mini with strict
function calling to:
  1. Classify the user's intent from message + chat history.
  2. Extract any booking-related parameters the user already provided.
  3. For simple QA, produce a direct answer in the user's language.

Returns a structured dict consumed by llm2_processor.process().
"""

import json
import logging
from datetime import timedelta

from openai import OpenAI

import config
from utils import today_almaty

logger = logging.getLogger(__name__)

_client = OpenAI(api_key=config.OPENAI_API_KEY)

# ─── Intent types ────────────────────────────────────────────────────────
INTENT_TYPES = [
    "simple_qa",              # answerable from system prompt / RAG / business rules
    "my_bookings",            # user wants to view their own existing bookings
    "general_availability",   # asking about schedule / free slots (no booking intent)
    "create_booking",         # wants to create a new booking (0 or more params extracted)
    "cancel_booking",         # wants to cancel an existing booking
    "modify_booking",         # wants to change / edit an existing booking
    "unknown",                # cannot determine intent
]

# ─── Defaults ────────────────────────────────────────────────────────────
_EMPTY_EXTRACTED = {
    "date": None,
    "time_start": None,
    "time_end": None,
    "field": None,
    "players": None,
    "name": None,
}

_EMPTY_RESULT = {
    "type": "unknown",
    "answer": None,
    "extracted_data": dict(_EMPTY_EXTRACTED),
}

# ─── OpenAI function-calling schema (strict mode) ───────────────────────
# With strict=True every key in "properties" must also appear in "required",
# and additionalProperties must be False at every object level.

_ROUTE_TOOL = {
    "type": "function",
    "function": {
        "name": "route_intent",
        "description": "Classify user intent and extract booking parameters.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "type": {
                    "type": "string",
                    "enum": INTENT_TYPES,
                    "description": "The detected user intent.",
                },
                "answer": {
                    "type": ["string", "null"],
                    "description": (
                        "For simple_qa: a brief, friendly answer in the user's "
                        "language. null for every other intent type."
                    ),
                },
                "extracted_data": {
                    "type": "object",
                    "additionalProperties": False,
                    "description": (
                        "Booking parameters extracted from the user's message. "
                        "Only populated for create_booking intent — all other "
                        "intents must have every field set to null."
                    ),
                    "properties": {
                        "date": {
                            "type": ["string", "null"],
                            "description": (
                                "Booking date in YYYY-MM-DD. Resolve relative "
                                "references (tomorrow, Thursday, etc.) to an "
                                "absolute date. null if not stated."
                            ),
                        },
                        "time_start": {
                            "type": ["string", "null"],
                            "description": "Start time in HH:MM 24h format. null if not stated.",
                        },
                        "time_end": {
                            "type": ["string", "null"],
                            "description": "End time in HH:MM 24h format. null if not stated.",
                        },
                        "field": {
                            "type": ["string", "null"],
                            "description": (
                                "Field format requested by the user: '5x5' or '6x6'. "
                                "null if not stated or ambiguous."
                            ),
                        },
                        "players": {
                            "type": ["integer", "null"],
                            "description": "Number of players. null if not stated.",
                        },
                        "name": {
                            "type": ["string", "null"],
                            "description": "Customer name. null if not stated.",
                        },
                    },
                    "required": [
                        "date", "time_start", "time_end",
                        "field", "players", "name",
                    ],
                },
            },
            "required": ["type", "answer", "extracted_data"],
        },
    },
}


# ─── System prompt (rebuilt every call to inject today's date) ───────────

def _build_system_prompt(rag_context: str = "") -> str:
    today = today_almaty()
    tomorrow = today + timedelta(days=1)

    weekdays = [
        "Monday", "Tuesday", "Wednesday",
        "Thursday", "Friday", "Saturday", "Sunday",
    ]
    day_name = weekdays[today.weekday()]

    fields_info = ", ".join(
        f"Поле {f['id']} ({f['format']})" for f in config.BOOKING_FIELDS
    )

    prompt = f"""You are the intent router for "Допши" (Dopshy) — a football field rental chatbot on WhatsApp.
Users write in Russian or Kazakh. Your job:
1. Classify the user's intent.
2. For create_booking — also extract any booking parameters they mentioned.
3. For simple_qa — also provide a brief answer.

TODAY: {day_name}, {today.strftime('%Y-%m-%d')}
OPERATING HOURS: {config.BOOKING_OPEN_TIME} – {config.BOOKING_CLOSE_TIME}
AVAILABLE FIELDS: {fields_info}

══════ INTENT TYPES ══════

"simple_qa"
  General question answerable from the knowledge base below (prices, location, rules, payment, etc.).
  → Set "answer" to a short, friendly reply in the user's language (like a real person on WhatsApp, 1-3 sentences).
  → All extracted_data fields must be null.

"my_bookings"
  User wants to CHECK / VIEW their own existing booking(s).
  Examples: "моя бронь", "мои брони", "менің брондауым", "я забронировал", "покажи мою бронь", "когда у меня бронь"
  → answer = null; all extracted_data = null.

"general_availability"
  User asks about what's free / the schedule WITHOUT wanting to book right now.
  Examples: "что свободно?", "есть ли места?", "бос уақыт бар ма?", "расписание"
  → answer = null; all extracted_data = null.

"create_booking"
  User wants to CREATE a new booking. ANY hint of wanting to book / rent / reserve / play / come for a game counts.
  Examples: "забронировать", "хочу поле", "арендовать", "хочу играть", "брондау", "можно на четверг?"
  → answer = null; extract whatever booking details the user mentioned.

"cancel_booking"
  User wants to CANCEL an existing booking.
  → answer = null; all extracted_data = null.

"modify_booking"
  User wants to CHANGE or EDIT an existing booking (move date, switch field, etc.).
  → answer = null; all extracted_data = null.

"unknown"
  Cannot determine intent.
  → answer = null; all extracted_data = null.

══════ EXTRACTION RULES (only for create_booking) ══════

For ALL intents EXCEPT create_booking, every extracted_data field MUST be null.

Within create_booking:
• Any parameter NOT clearly stated → null. NEVER guess or infer.
• date: resolve to YYYY-MM-DD using today = {today.strftime('%Y-%m-%d')}.
  "завтра" / "tomorrow" → {tomorrow.strftime('%Y-%m-%d')}.
  Weekday names → the NEXT occurrence starting from today.
• time_start / time_end: 24-hour HH:MM.
  "вечером" / "evening" is too vague → null.
  "в 6 вечера" → "18:00". "с 10 до 12" → time_start="10:00", time_end="12:00".
• field: "5x5" or "6x6" ONLY.
  "большое поле" = "6x6". "маленькое" = "5x5". Generic "поле" without size → null.
• players: integer. "нас 10 человек" → 10. "с друзьями" → null (count unknown).
• name: customer's name. "меня зовут Алмаз" → "Алмаз". Not stated → null."""

    if rag_context:
        prompt += f"\n\n══════ KNOWLEDGE BASE ══════\n{rag_context}\n══════"

    return prompt


# ─── Public API ──────────────────────────────────────────────────────────

def route(
    user_text: str,
    chat_history: list[dict],
    rag_context: str = "",
) -> dict:
    """
    Classify user intent and extract booking parameters.

    Args:
        user_text:    The latest user message.
        chat_history: Prior conversation turns [{"role": ..., "content": ...}, ...].
        rag_context:  Retrieved knowledge-base context (for answering simple QA).

    Returns:
        {
            "type":           str,          # one of INTENT_TYPES
            "answer":         str | None,   # direct answer (simple_qa) or None
            "extracted_data": {             # every value is nullable
                "date", "time_start", "time_end",
                "field", "players", "name"
            }
        }
    """
    try:
        system_prompt = _build_system_prompt(rag_context)

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(chat_history)
        messages.append({"role": "user", "content": user_text})

        response = _client.chat.completions.create(
            model=config.MODEL_NAME,
            temperature=0,
            messages=messages,
            tools=[_ROUTE_TOOL],
            tool_choice={
                "type": "function",
                "function": {"name": "route_intent"},
            },
        )

        tool_calls = response.choices[0].message.tool_calls
        if not tool_calls:
            logger.warning("[LLM1] No tool call in response")
            return dict(_EMPTY_RESULT)

        raw = tool_calls[0].function.arguments
        if not raw:
            return dict(_EMPTY_RESULT)

        parsed = json.loads(raw)

        # Merge over empty template so every key is guaranteed to exist
        result = {**_EMPTY_RESULT, **parsed}
        result["extracted_data"] = {
            **_EMPTY_EXTRACTED,
            **parsed.get("extracted_data", {}),
        }

        logger.info(
            "[LLM1] type=%s | extracted=%s | answer=%.80s",
            result["type"],
            {k: v for k, v in result["extracted_data"].items() if v is not None},
            result.get("answer") or "",
        )
        return result

    except Exception as err:
        logger.error("[LLM1] route() failed: %s", err, exc_info=True)
        return dict(_EMPTY_RESULT)
