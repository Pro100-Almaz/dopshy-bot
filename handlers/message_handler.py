"""
Core message processing pipeline:
  incoming text → RAG retrieval → GPT-4o-mini → WhatsApp reply
"""

import logging
import threading

from chat.conversation import append_message, get_history, clear_history
from chat.llm import get_ai_response
from handlers.trial_session import handle_trial_turn, start_trial_flow
from integrations.sheets.booking_sheets import upsert_booking_row
from rag.retriever import retrieve_context
from handlers.whatsapp_client import send_text_message, mark_as_read, download_media
from handlers.booking_session import _detect_lang, handle_booking_turn, start_booking_flow
from handlers.edit_booking import handle_edit_request as handle_edit_booking_request
from handlers.edit_trial import handle_edit_request as handle_edit_trial_request
from integrations import booking_service, payment_validation
from integrations.repo import booking_repo
import config

logger = logging.getLogger(__name__)

RESET_COMMANDS = {"/reset", "/сброс", "/тазалау", "сброс", "reset"}

# Distinct user-facing messages per payment_validation rejection code.
# Format placeholders {paid} and {required} are filled in for the "amount" code.
_PAYMENT_REJECT_MESSAGES: dict[str, tuple[str, str]] = {
    "unreadable": (
        "❌ Не удалось распознать чек.\n"
        "Отправьте, пожалуйста, официальный PDF-чек из приложения Kaspi или Halyk "
        "(не скриншот и не фото).",
        "❌ Чекті тану мүмкін болмады.\n"
        "Kaspi немесе Halyk қосымшасынан ресми PDF-чекті жіберіңіз "
        "(скриншот немесе фото емес).",
    ),
    "recipient": (
        "❌ Платёж отправлен не на счёт «Допши».\n"
        "Проверьте, что получатель — это БИН/телефон из инструкции к оплате, "
        "и отправьте новый чек.",
        "❌ Төлем «Допши» шотына түспеген.\n"
        "Алушы — төлем нұсқаулығындағы БСН/телефон екеніне көз жеткізіп, "
        "жаңа чек жіберіңіз.",
    ),
    "amount": (
        "❌ Сумма в чеке меньше необходимой предоплаты ({paid}₸ < {required}₸).\n"
        "Доплатите разницу и отправьте новый чек, либо чек на полную сумму.",
        "❌ Чектегі сома қажетті алдын ала төлемнен кем ({paid}₸ < {required}₸).\n"
        "Айырмашылықты төлеп жаңа чек жіберіңіз немесе толық сомаға чек жіберіңіз.",
    ),
    "date": (
        "❌ Чек устарел или дата некорректна.\n"
        "Отправьте свежий чек (не старше 24 часов).",
        "❌ Чек ескірген немесе күні дұрыс емес.\n"
        "Жаңа чек жіберіңіз (24 сағаттан аспаған).",
    ),
}

_PAYMENT_REJECT_FOOTER_RU = (
    "Вы можете отправить корректный чек ещё раз, пока бронь не истекла. "
    "Если нужна помощь — свяжитесь с администратором."
)
_PAYMENT_REJECT_FOOTER_KK = (
    "Бронь мерзімі біткенше дұрыс чекті қайта жіберуге болады. "
    "Қажет болса — әкімшімен хабарласыңыз."
)


def _format_payment_reject_message(
    code: str, parsed: dict, booking: dict
) -> str:
    """Build the bilingual user message for a rejected payment receipt."""
    ru, kk = _PAYMENT_REJECT_MESSAGES.get(code, _PAYMENT_REJECT_MESSAGES["unreadable"])
    if code == "amount":
        paid = int(parsed.get("amount") or 0)
        required = int(float(booking.get("price_total") or 0) * config.PAYMENT_MIN_FRACTION)
        ru = ru.format(paid=paid, required=required)
        kk = kk.format(paid=paid, required=required)
    return (
        f"{ru}\n\n{_PAYMENT_REJECT_FOOTER_RU}\n\n"
        f"— — —\n\n"
        f"{kk}\n\n{_PAYMENT_REJECT_FOOTER_KK}"
    )


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

        # Document = payment receipt — confirm the booking
        if msg_type == "document":
            media_id = message.get("document", {}).get("id")
            _handle_payment_receipt(phone_number_id, sender_id, media_id)
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

        # 1. Booking session handler (Bot 1 — Dopshy field rental only)
        if bot_config["name"] == "dopsy_bot":
            logger.info("[BOOKING] Checking booking branch for chat_id=%s", chat_id)
            booking_reply = handle_booking_turn(
                chat_id, phone_number_id, sender_id, user_text
            )
            if booking_reply is not None:
                logger.info(
                    "[BOOKING] Booking branch handled message — skipping RAG/LLM. "
                    "Reply preview: %.120s", booking_reply
                )
                append_message(chat_id, "user", user_text)
                append_message(chat_id, "assistant", booking_reply)
                send_text_message(phone_number_id, sender_id, booking_reply)
                return
            logger.info("[BOOKING] Booking branch returned None — falling through to RAG/LLM")

        elif bot_config["name"] == "dopsy_boxing":
            logger.info("[TRIAL] Checking trial branch for chat_id=%s", chat_id)
            trial_reply = handle_trial_turn(
                chat_id, phone_number_id, sender_id, user_text, bot_config["name"]
            )
            if trial_reply is not None:
                logger.info(
                    "[TRIAL] Trial branch handled message — skipping RAG/LLM. "
                    "Reply preview: %.120s", trial_reply
                )
                append_message(chat_id, "user", user_text)
                append_message(chat_id, "assistant", trial_reply)
                send_text_message(phone_number_id, sender_id, trial_reply)
                return
            logger.info("[TRIAL] Trial branch returned None — falling through to RAG/LLM")


        # 2. Retrieve relevant context from knowledge base
        context = retrieve_context(user_text)
        logger.info("[RAG] Retrieved %d chars of context for: %.80s", len(context), user_text)

        # 3a. For Bot 1: inject live availability so the LLM can answer
        #     "what's free?" questions and decide when to emit [BOOK].
        if bot_config["name"] == "dopsy_bot":
            from integrations.booking import get_free_windows, format_availability_context
            free = get_free_windows()
            availability_ctx = format_availability_context(free)
            logger.info("[BOOKING] Injecting availability context (%d free windows) into LLM call", len(free))
            context = f"{availability_ctx}\n\n{context}" if context else availability_ctx

        elif bot_config["name"] == "dopsy_boxing":
            from integrations.trial import get_trial_daytime, format_availability_context
            free = get_trial_daytime(bot_config["name"], None)
            availability_ctx = format_availability_context(free)
            logger.info("[TRIAL] Injecting availability context (%d free trial times) into LLM call", len(free))
            context = f"{availability_ctx}\n\n{context}" if context else availability_ctx

        # 3b. Get conversation history
        history = get_history(chat_id)
        logger.info("[LLM] History length: %d messages", len(history))

        # 4. Generate response
        reply, tool_call = get_ai_response(
            phone_number_id=phone_number_id,
            chat_id=chat_id,
            user_message=user_text,
            history=history,
            context=context,
        )
        logger.info("[LLM] Raw reply (%.120s) | tool_call=%s", reply, tool_call)

        # 5. LLM may have asked us to launch a deterministic sub-flow.
        if tool_call:
            handle_reply = reply
            if tool_call["name"] in "start_booking":
                lang = _detect_lang(user_text)
                logger.info("[BOOKING] LLM called start_booking tool — starting booking flow (lang=%s)", lang)
                handle_reply = start_booking_flow(chat_id, sender_id, lang)

            elif tool_call["name"] == "edit_booking":
                logger.info("[EDIT] LLM called edit_booking tool — diff=%s", tool_call["args"])
                handle_reply = handle_edit_booking_request(chat_id, sender_id, tool_call["args"])

            elif tool_call["name"] == "start_trial":
                lang = _detect_lang(user_text)
                logger.info("[TRIAL] LLM called start_trial tool — starting trial flow (lang=%s)", lang)
                handle_reply = start_trial_flow(chat_id, sender_id, bot_config["name"], lang)

            elif tool_call["name"] == "edit_trial":
                logger.info("[EDIT] LLM called edit_trial tool — diff=%s", tool_call["args"])
                handle_reply = handle_edit_trial_request(chat_id, sender_id, tool_call["args"], bot_config["name"])

            reply = (reply + "\n\n" + handle_reply) if reply else handle_reply

        # 6. Save to history
        append_message(chat_id, "user", user_text)
        append_message(chat_id, "assistant", reply)

        # 7. Send reply
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


def _refresh_booking_sheet(booking: dict, state: str) -> None:
    """Push a booking's current state to the flat Bookings sheet (background)."""
    row = {
        "id":            booking["id"],
        "field":         booking["field"],
        "date":          booking["date"],
        "time_start":    booking["time_start"],
        "time_end":      booking["time_end"],
        "customer_name": booking.get("customer_name", ""),
        "players":       booking.get("players"),
        "state":         state,
        "notes":         "",
    }

    def _run():
        try:
            upsert_booking_row(row)
        except Exception as exc:
            logger.error("[PAYMENT] Sheet update failed for booking id=%s: %s", booking["id"], exc)

    threading.Thread(target=_run, daemon=True).start()


def _handle_payment_receipt(phone_number_id: str, sender_phone: str,
                            proof_media_id: str | None = None) -> None:
    """
    Called when a user sends a document (assumed to be a payment receipt).
    Finds their most recent awaiting_payment booking, confirms it via the
    service layer, and refreshes Google Sheets in the background.
    """
    booking = booking_repo.get_awaiting_payment_booking(sender_phone)

    if not booking:
        logger.info("[PAYMENT] Document from %s — no awaiting_payment booking found", sender_phone)
        send_text_message(
            phone_number_id,
            sender_phone,
            "Оплата принята! Спасибо. ✅\nТөлем қабылданды! Рахмет. ✅",
        )
        return

    # Download the receipt PDF and validate it before confirming.
    pdf = download_media(phone_number_id, proof_media_id) if proof_media_id else None
    if not pdf:
        logger.warning("[PAYMENT] Could not download media for booking id=%d", booking["id"])
        send_text_message(
            phone_number_id, sender_phone,
            "Не удалось загрузить чек. Пожалуйста, отправьте PDF-чек ещё раз.\n"
            "Чекті жүктеу мүмкін болмады. PDF-чекті қайта жіберіңіз.",
        )
        return

    result = payment_validation.validate_receipt(booking, pdf)

    if not result["ok"]:
        booking_service.reject_payment(booking["id"], result["reason"], result["parsed"])
        logger.info("[PAYMENT] Booking id=%d receipt rejected: %s", booking["id"], result["code"])
        send_text_message(
            phone_number_id, sender_phone,
            _format_payment_reject_message(result["code"], result["parsed"], booking),
        )
        return

    res = booking_service.submit_payment_proof(
        booking["id"], parsed=result["parsed"], proof_media_id=proof_media_id
    )
    if not res["ok"]:
        msg = ("Этот чек уже был использован. Свяжитесь с администратором.\n"
               "Бұл чек бұрын қолданылған. Әкімшімен хабарласыңыз."
               if res["code"] == "PAYMENT_DUPLICATE"
               else "Не удалось подтвердить оплату. Свяжитесь с администратором.\n"
                    "Төлемді растау мүмкін болмады. Әкімшімен хабарласыңыз.")
        logger.error("[PAYMENT] submit_payment_proof failed for id=%d: %s", booking["id"], res)
        send_text_message(phone_number_id, sender_phone, msg)
        return

    logger.info("[PAYMENT] Booking id=%d confirmed for phone=%s", booking["id"], sender_phone)

    _refresh_booking_sheet(booking, "confirmed")

    booking_date = booking["date"]
    ts = str(booking["time_start"])[:5]
    te = str(booking["time_end"])[:5]
    send_text_message(
        phone_number_id,
        sender_phone,
        f"✅ Оплата получена! Бронь подтверждена.\n\n"
        f"📅 {booking_date}\n"
        f"⏰ {ts}–{te}\n"
        f"⚽ Поле {booking['field']} ({booking['format']})\n\n"
        f"Ждём вас! 🙌\n\n"
        f"— — —\n"
        f"✅ Төлем қабылданды! Брондау расталды.\n\n"
        f"📅 {booking_date}\n"
        f"⏰ {ts}–{te}\n"
        f"⚽ Поле {booking['field']} ({booking['format']})\n\n"
        f"Сізді күтеміз! 🙌",
    )