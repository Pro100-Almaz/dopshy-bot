"""
Core message processing pipeline:
  incoming text → RAG retrieval → GPT-4o-mini → WhatsApp reply
"""

import logging

from chat.conversation import append_message, get_history, clear_history
from chat.llm import get_ai_response
from rag.retriever import retrieve_context
from handlers.whatsapp_client import send_text_message, mark_as_read
import config

logger = logging.getLogger(__name__)

RESET_COMMANDS = {"/reset", "/сброс", "/тазалау", "сброс", "reset"}


def handle_incoming_message(payload: dict) -> None:
    """
    Parse a WhatsApp Cloud API webhook payload and respond.
    Supports both individual and group messages.
    """
    sender_id = ""

    try:
        entry = payload.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})

        phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
        bot_config = config.get_bot_config(phone_number_id)

        if not bot_config:
            logger.warning("Unknown phone_number_id from webhook: %s", phone_number_id)
            return

        messages = value.get("messages", [])
        if not messages:
            return  # Delivery status update — ignore

        message = messages[0]
        msg_type = message.get("type")
        message_id = message.get("id", "")
        sender_id = message.get("from", "")  # sender phone number

        # Mark as read immediately
        if message_id:
            mark_as_read(phone_number_id, message_id)

        # Document = payment receipt confirmation
        if msg_type == "document":
            send_text_message(
                phone_number_id,
                sender_id,
                "Оплата принята! Спасибо. ✅\nТөлем қабылданды! Рахмет. ✅",
            )
            return

        # Only handle text messages
        if msg_type != "text":
            send_text_message(
                phone_number_id,
                sender_id,
                "Пожалуйста, отправьте текстовое сообщение. / Мәтіндік хабарлама жіберіңіз.",
            )
            return

        user_text = message["text"]["body"].strip()

        # Determine chat context key.
        # For group messages Meta Cloud API includes a "context" object; we use
        # sender_id so each person in a group gets a shared thread identified by
        # their own number (simplest approach — change to group JID if needed).
        chat_id = f"{phone_number_id}:{sender_id}"

        # Handle reset command
        if user_text.lower() in RESET_COMMANDS:
            clear_history(chat_id)
            send_text_message(
                phone_number_id,
                sender_id,
                "История разговора сброшена. Начнём заново! 🔄\n"
                "Сөйлесу тарихы тазаланды. Қайтадан бастайық! 🔄",
            )
            return

        logger.info(
            "Message from %s via bot %s (%s): %s",
            sender_id,
            bot_config["name"],
            phone_number_id,
            user_text[:80],
        )

        # 1. Retrieve relevant context from knowledge base
        context = retrieve_context(user_text)

        # 2. Get conversation history
        history = get_history(chat_id)

        # 3. Generate response
        reply = get_ai_response(
            phone_number_id=phone_number_id,
            chat_id=chat_id,
            user_message=user_text,
            history=history,
            context=context,
        )

        # 4. Save to history
        append_message(chat_id, "user", user_text)
        append_message(chat_id, "assistant", reply)

        # 5. Send reply
        send_text_message(phone_number_id,  sender_id, reply)
        logger.info("Replied to %s via bot %s", sender_id, bot_config["name"])

    except Exception as exc:
        logger.exception("Error handling message: %s", exc)
        # Best-effort fallback reply
        try:
            entry = payload.get("entry", [{}])[0]
            changes = entry.get("changes", [{}])[0]
            value = changes.get("value", {})

            phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
            bot_config = config.get_bot_config(phone_number_id)

            if sender_id and bot_config:
                send_text_message(
                    phone_number_id,
                    sender_id,
                    "Произошла ошибка. Попробуйте позже или свяжитесь с администратором.\n"
                    "Қате орын алды. Кейінірек немесе әкімшімен хабарласыңыз.",
                )
        except Exception:
            pass