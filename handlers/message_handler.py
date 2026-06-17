"""
Core message processing pipeline:
  incoming text → RAG retrieval → GPT-4o-mini → WhatsApp reply
"""
import logging
import threading

from chat.conversation import append_message, get_history, clear_history
from chat.llm import get_ai_response, route_incoming_message
from handlers.extractor import extract_booking_details
from handlers.payment.pricing import process_field_prices
from handlers.sessions.trial_session import handle_trial_turn, start_trial_flow
from integrations.repo.booking_repo import has_awaiting_payments
from integrations.sheets.booking_sheets import upsert_booking_row, refresh_all_bookings
from rag.retriever import retrieve_context
from handlers.whatsapp_client import send_text_message, mark_as_read, download_media
from handlers.sessions.booking_session import handle_booking_turn, start_booking_flow, start_await_date_flow
from handlers.sessions.base_session import BasePromptBuilder
from handlers.edit_booking import handle_edit_request as handle_edit_booking_request
from handlers.edit_trial import handle_edit_request as handle_edit_trial_request, handle_cancel_trial_request
from integrations import booking_service, payment_validation, booking, trial
from integrations.repo import booking_repo
from integrations.repo import postgres as _pg
from handlers.llm_booking_flow import LlmBookingFlowHandler
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

_CANCEL_STATUS = (
    """По вашему номеру не найдено записей.
    \n\n–––\n\n"Сіздің нөміріңізге белсенді жазба табылмады.""",
    """Бронирование отменено. Если захотите снова — просто напишите, что хотите забронировать поле. 🙂
    \n\n–––\n\nБрондау тоқтатылды. Қайта қаласаңыз — алаңды брондағыңыз келетінін жазыңыз. 🙂"""
)

_LOCATION_MESSAGE = (
    "📍 Сыганак 6Ф, напротив ТЦ Mechta и ТЦ Tumar.\n"
    "Вход и заезд со стороны улицы Тәттімбета.\n"
    "Ссылка на 2GIS: https://2gis.kz/astana/geo/70000001074875383\n\n"
    "–––\n\n"
    "📍 Сығанақ 6Ф, Mechta және Tumar СО қарсы.\n"
    "Кіру және кіреберіс Тәттімбет көшесі жағынан.\n"
    "2GIS сілтемесі: https://2gis.kz/astana/geo/70000001074875383\n"
)

builder = BasePromptBuilder({}, "", (), ())


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
        if msg_type not in ["text", "interactive"]:
            send_text_message(
                phone_number_id,
                sender_id,
                "Извините, я ассистент-бот и могу распознавать только текст, не могли бы вы отправлять только текстовые сообщения, пожалуйста. "
                "/ Кешіріңіз, мен бот-ассистентпін, тек қана мәтіндік хабарламаны оқи аламын. Өтініш, мәтіндік хабарлама жіберіңіз.",
            )
            return

        user_text = ""
        if msg_type == "interactive" and message["interactive"]["type"] == 'button_reply':
            button_reply = message["interactive"]["button_reply"]
            user_text = button_reply.get("title")

        if msg_type == "text":
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

        context = retrieve_context(user_text)
        logger.info("[RAG] Retrieved %d chars of context for: %.80s", len(context), user_text)

        history = get_history(chat_id)
        logger.info("[LLM] History length: %d messages", len(history))

        # 1. Booking handler (Bot 1 — Dopshy field rental)
        #
        # Two-LLM architecture:
        #   a) Active session → existing deterministic step handler
        #      (handles step_date, step_time, … step_confirm)
        #   b) No active session → LLM1 (intent + extraction) → LLM2 (process)
        #   c) If LLM2 returns None → fall through to legacy RAG/LLM pipeline
        if bot_config["name"] == "dopsy_bot":
            logger.info("[BOOKING] Checking booking branch for chat_id=%s", chat_id)

            free = booking.get_free_windows()
            availability_ctx = booking.format_availability_context(free)
            logger.info("[BOOKING] Injecting availability context (%d free windows) into LLM call", len(free))
            context = f"{availability_ctx}\n\n{context}" if context else availability_ctx

            _session = _pg.get_active_session("dopsy_bot", chat_id)

            if _session:
                # (a) Active booking session → deterministic step handler
                booking_reply = handle_booking_turn(
                    chat_id, phone_number_id, sender_id, user_text
                )
                if booking_reply is not None:
                    logger.info(
                        "[BOOKING] Session handler replied: %.120s", booking_reply,
                    )
                    append_message(chat_id, "user", user_text)
                    append_message(chat_id, "assistant", booking_reply)
                    send_text_message(phone_number_id, sender_id, booking_reply)
                    return

            intent = route_incoming_message(history, user_text)
            logger.info("[BOOKING] Intent detection replied, Intent is %s", intent)
            if intent == 'question_price':
                send_text_message(phone_number_id, sender_id, process_field_prices())
                return

            elif intent == 'question_location':
                send_text_message(phone_number_id, sender_id, _LOCATION_MESSAGE)
                return

            elif intent == 'question_slots':
                # If the user named a date, show slots for that day only; otherwise
                # fall back to the full 7-day overview. _check_date_only also handles
                # the "no free slots on that date" case (lists alternative dates).
                slots_date = extract_booking_details(history, user_text).get("date")
                logger.info("[BOOKING] question_slots — extracted date=%s", slots_date)
                if slots_date:
                    response = LlmBookingFlowHandler()._check_date_only({"date": slots_date})
                else:
                    response = f"""Давайте забронируем! Вот доступные слоты / 
                                Брондайық! Бос слоттар:\n\n{availability_ctx}\n\n
                                Укажите дату, время, или номер поля./
                                Күнді, уақытты немесе алаң нөмірін жазыңыз.
                                """
                send_text_message(phone_number_id, sender_id, response)
                return

            elif intent == 'booking_init':
                # Booking intent but no date/time/field yet → ask for the date first,
                # extract it on the next turn, then launch the flow on that date.
                if has_awaiting_payments(sender_id):
                    send_text_message(phone_number_id, sender_id,
                                      'Вы не можете создать новую бронь пока не оплатите предыдущую! \n'
                                      '\n----\n'
                                      'Осығын дейінгі брондарыңызды төлемей жаңа брондар өоя алмайсыз! \n')
                    clear_history(chat_id)
                    return
                lang = builder.detect_lang(user_text)
                reply = start_await_date_flow(chat_id, sender_id, lang)
                append_message(chat_id, "user", user_text)
                append_message(chat_id, "assistant", reply)
                logger.info("[BOOKING] booking_init — asked for date, awaiting reply")
                send_text_message(phone_number_id, sender_id, reply)
                return

            elif intent in ['booking_new', 'booking_continue']:
                if has_awaiting_payments(sender_id):
                    send_text_message(phone_number_id, sender_id,
                                      'Вы не можете создать новую бронь пока не оплатите предыдущую! \n'
                                      '\n----\n'
                                      'Осығын дейінгі брондарыңызды төлемей жаңа брондар өоя алмайсыз! \n')
                    clear_history(chat_id)
                    return
                extracted_data = extract_booking_details(history, user_text)
                logger.info("[BOOKING] Data Extracted: %s", extracted_data)
                handler = LlmBookingFlowHandler()
                reply = handler.handle(extracted_data, chat_id, user_text, sender_id)
                append_message(chat_id, "user", user_text)
                append_message(chat_id, "assistant", reply)
                logger.info("[LLM2] reply: %s", reply)
                send_text_message(phone_number_id, sender_id, reply)
                return

            elif intent == 'booking_status':
                logger.info("[BOOKING] Fetching user's own bookings")
                bookings = booking_repo.get_user_upcoming_bookings(sender_id)
                send_text_message(phone_number_id, sender_id,
                                  booking.format_user_booking_context(bookings))
                return

            elif intent == 'booking_cancel':
                logger.info("[BOOKING] Cancelling all drafts of the user")
                cancelled = booking_repo.cancel_draft_awaiting_payment(sender_id)
                clear_history(chat_id)
                refresh_all_bookings()
                send_text_message(phone_number_id, sender_id, _CANCEL_STATUS[cancelled])
                return

            # (c) Neither handled the message → fall through to RAG/LLM
            logger.info("[BOOKING] Two-LLM flow did not handle — falling through to RAG/LLM")

        else:
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

            free = trial.get_trial_daytime(bot_config["name"], None)
            availability_ctx = trial.format_availability_context(free)
            logger.info("[TRIAL] Injecting availability context (%d free trial times) into LLM call", len(free))
            context = f"{availability_ctx}\n\n{context}" if context else availability_ctx

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
                lang = builder.detect_lang(user_text)
                logger.info("[BOOKING] LLM called start_booking tool — starting booking flow (lang=%s)", lang)
                handle_reply = start_booking_flow(chat_id, sender_id, lang)

            elif tool_call["name"] == "edit_booking":
                logger.info("[EDIT] LLM called edit_booking tool — diff=%s", tool_call["args"])
                handle_reply = handle_edit_booking_request(chat_id, sender_id, tool_call["args"])

            elif tool_call["name"] == "start_trial":
                lang = builder.detect_lang(user_text)
                logger.info("[TRIAL] LLM called start_trial tool — starting trial flow (lang=%s)", lang)
                handle_reply = start_trial_flow(chat_id, sender_id, bot_config["name"], lang)

            elif tool_call["name"] == "edit_trial":
                logger.info("[EDIT] LLM called edit_trial tool — diff=%s", tool_call["args"])
                handle_reply = handle_edit_trial_request(chat_id, sender_id, tool_call["args"], bot_config["name"])

            elif tool_call["name"] == "cancel_trial":
                logger.info("[CANCEL] LLM called cancel_trial tool")
                handle_reply = handle_cancel_trial_request(chat_id, sender_id, bot_config["name"])

            reply = (reply + "\n\n" + handle_reply) if reply else handle_reply

        # 6. Save to history
        append_message(chat_id, "user", user_text)
        append_message(chat_id, "assistant", reply)

        # 7. Send reply
        send_text_message(phone_number_id, sender_id, reply)
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
        "id": booking["id"],
        "field": booking["field"],
        "date": booking["date"],
        "time_start": booking["time_start"],
        "time_end": booking["time_end"],
        "customer_name": booking.get("customer_name", ""),
        "players": booking.get("players"),
        "state": state,
        "notes": "",
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

    # TRANSITIVE BOOKING: use combined price of both bookings for payment validation
    combined_price = booking_repo.get_transitive_total_price(booking["id"])
    if combined_price is not None:
        booking["price_total"] = combined_price

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
