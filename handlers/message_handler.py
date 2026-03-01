"""
Core message processing pipeline:
  incoming text → RAG retrieval → GPT-4o-mini → WhatsApp reply
"""

import logging

from chat.conversation import append_message, get_history, clear_history
from chat.llm import get_ai_response
from rag.retriever import retrieve_context
from handlers.whatsapp_client import send_text_message, mark_as_read

logger = logging.getLogger(__name__)

RESET_COMMANDS = {"/reset", "/сброс", "/тазалау", "сброс", "reset"}


def handle_incoming_message(payload: dict) -> None:
    """
    Parse a WhatsApp Cloud API webhook payload and respond.
    Supports both individual and group messages.
    """
    try:
        entry = payload.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})

        messages = value.get("messages", [])
        if not messages:
            return  # Delivery status update — ignore

        message = messages[0]
        msg_type = message.get("type")
        message_id = message.get("id", "")
        sender_id = message.get("from", "")  # sender phone number

        # Mark as read immediately
        mark_as_read(message_id)

        # Only handle text messages
        if msg_type != "text":
            send_text_message(
                sender_id,
                "Пожалуйста, отправьте текстовое сообщение. / Мәтіндік хабарлама жіберіңіз.",
            )
            return

        user_text = message["text"]["body"].strip()

        # Determine chat context key.
        # For group messages Meta Cloud API includes a "context" object; we use
        # sender_id so each person in a group gets a shared thread identified by
        # their own number (simplest approach — change to group JID if needed).
        chat_id = sender_id

        # Handle reset command
        if user_text.lower() in RESET_COMMANDS:
            clear_history(chat_id)
            send_text_message(
                sender_id,
                "История разговора сброшена. Начнём заново! 🔄\n"
                "Сөйлесу тарихы тазаланды. Қайтадан бастайық! 🔄",
            )
            return

        logger.info("Message from %s: %s", sender_id, user_text[:80])

        # 1. Retrieve relevant context from knowledge base
        context = retrieve_context(user_text)

        # 2. Get conversation history
        history = get_history(chat_id)

        # 3. Generate response
        reply = get_ai_response(
            chat_id=chat_id,
            user_message=user_text,
            history=history,
            context=context,
        )

        # 4. Save to history
        append_message(chat_id, "user", user_text)
        append_message(chat_id, "assistant", reply)

        # 5. Send reply
        send_text_message(sender_id, reply)
        logger.info("Replied to %s", sender_id)

    except Exception as exc:
        logger.exception("Error handling message: %s", exc)
        # Best-effort fallback reply
        try:
            sender_id = (
                payload.get("entry", [{}])[0]
                .get("changes", [{}])[0]
                .get("value", {})
                .get("messages", [{}])[0]
                .get("from", "")
            )
            if sender_id:
                send_text_message(
                    sender_id,
                    "Произошла ошибка. Попробуйте позже или свяжитесь с администратором.\n"
                    "Қате орын алды. Кейінірек немесе әкімшімен хабарласыңыз.",
                )
        except Exception:
            pass
