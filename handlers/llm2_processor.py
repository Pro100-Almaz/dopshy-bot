"""
LLM2 — Booking Processor and Database-Aware Assistant.

Second stage of the two-LLM architecture. Receives the structured output
from LLM1 (intent + extracted data) and:

  • create_booking  → resolves field format, runs availability checks via
                      LlmBookingFlowHandler, creates/updates draft + session.
  • my_bookings     → queries the user's upcoming bookings from PostgreSQL
                      and generates a natural reply via get_booking_reply().
  • general_availability → fetches free windows and formats them for the user.
  • simple_qa       → returns LLM1's direct answer (no extra LLM call).
  • cancel / modify / unknown → returns None so the caller can fall through
                      to the existing RAG/LLM pipeline (which has the
                      edit_booking / cancel tools).
"""

import logging

import config
from chat.llm import get_booking_reply
from handlers.llm_booking_flow import LlmBookingFlowHandler
from integrations import booking as booking_logic
from integrations.repo import booking_repo

logger = logging.getLogger(__name__)

_booking_handler = LlmBookingFlowHandler()

# Kazakh-only Cyrillic characters — quick language detection
_KZ_CHARS = set("әғіңөұүһқ")


# ─── Public API ──────────────────────────────────────────────────────────

def process(
    llm1_result: dict,
    chat_id: str,
    sender_phone: str,
    phone_number_id: str,
    user_text: str,
) -> str | None:
    """
    Route LLM1 output to the correct handler.

    Returns a response string to send to the user, or None if this layer
    cannot handle the intent (caller should fall through to the existing
    RAG/LLM pipeline).
    """
    intent = llm1_result.get("type", "unknown")
    extracted = llm1_result.get("extracted_data", {})
    answer = llm1_result.get("answer")
    lang = "kk" if any(c in _KZ_CHARS for c in user_text.lower()) else "ru"

    logger.info("[LLM2] Processing intent=%s lang=%s", intent, lang)

    # ── simple_qa: LLM1 already produced the answer ──────────────────
    if intent == "simple_qa" and answer:
        return answer

    # ── create_booking: smart booking flow with partial data ─────────
    if intent == "create_booking":
        return _handle_create_booking(extracted, chat_id, sender_phone, lang)

    # ── my_bookings: query DB, generate natural reply ────────────────
    if intent == "my_bookings":
        return _handle_my_bookings(sender_phone, user_text)

    # ── general_availability: show free slots via LLM ────────────────
    if intent == "general_availability":
        return _handle_availability(user_text)

    # ── cancel / modify / unknown → not handled here ─────────────────
    # Returning None lets the caller fall through to the existing
    # RAG/LLM pipeline which has the edit_booking and cancel tools.
    return None


# ─── Internal handlers ───────────────────────────────────────────────────

def _resolve_field_format(extracted: dict) -> dict:
    """
    Translate a field format string ("6x6") into an integer field ID.

    • Unique match (e.g. "6x6" → field 2)  → set field to that ID.
    • Multiple matches (e.g. "5x5" → fields 1 and 3) → set field to None
      so the availability check shows all matching options.
    • Already an int or None → pass through unchanged.
    """
    resolved = dict(extracted)
    field_val = resolved.get("field")

    if field_val is None or isinstance(field_val, int):
        return resolved

    matching = [
        f for f in config.BOOKING_FIELDS
        if f["format"] == str(field_val)
    ]
    resolved["field"] = matching[0]["id"] if len(matching) == 1 else None
    return resolved


def _handle_create_booking(
    extracted: dict,
    chat_id: str,
    sender_phone: str,
    lang: str,
) -> str:
    """
    Handle create_booking intent.

    • No data extracted at all → start the familiar deterministic step flow
      (numbered date list, etc.) so the UX stays consistent for bare
      "хочу забронировать" messages.
    • Some data extracted → use LlmBookingFlowHandler which checks
      availability, creates a draft with the known fields, and saves
      the session at the correct step so the deterministic flow picks up
      only the MISSING pieces.
    """
    has_any_data = any(v is not None for v in extracted.values())

    if not has_any_data:
        # Fall back to the original step-by-step flow (starts at step_date)
        from handlers.sessions.booking_session import start_booking_flow
        return start_booking_flow(chat_id, sender_phone, lang)

    resolved = _resolve_field_format(extracted)
    return _booking_handler.handle(resolved, chat_id, sender_phone, lang)


def _handle_my_bookings(sender_phone: str, user_text: str) -> str:
    """Query the user's upcoming bookings and generate a conversational reply."""
    bookings = booking_repo.get_user_upcoming_bookings(sender_phone)
    ctx = booking_logic.format_user_booking_context(bookings)
    # get_booking_reply makes an LLM call to wrap the raw data in a natural response
    return get_booking_reply(user_text, ctx)


def _handle_availability(user_text: str) -> str:
    """Fetch free windows and generate a natural reply."""
    free = booking_logic.get_free_windows()
    ctx = booking_logic.format_availability_context(free)
    return get_booking_reply(user_text, ctx)
