"""
Core message processing pipeline:
  incoming text → RAG retrieval → GPT-4o-mini → WhatsApp reply
"""
import logging
import re
import threading
from typing import Union

import requests

from chat.conversation import append_message, get_history, clear_history
from chat.llm import get_ai_response, route_incoming_message, route_trial_message
from handlers.extractor import extract_booking_details
from handlers.payment.pricing import process_field_prices, fmt_price
from handlers.questions import check_slots
from handlers.sessions.base_session import BasePromptBuilder
from handlers.sessions.trial_session import handle_trial_turn, start_trial_flow
from handlers.llm_trial_flow import LlmTrialFlowHandler
from handlers.llm_trial_flow import LLM_FLOW_STATES as LLM_TRIAL_FLOW_STATES
from handlers.llm_trial_flow import is_greeting as is_trial_greeting
from handlers.llm_trial_flow import is_acknowledgement as is_trial_acknowledgement
from handlers.llm_trial_flow import is_bot_identity_question
from handlers.llm_trial_flow import is_factual_question as is_trial_factual_question
from integrations.providers.payload import IncomingWhatsAppMessage, OutboundChannel, WhatsAppMedia
from integrations.repo.booking_repo import has_awaiting_payments, get_existing_draft
from integrations.repo.academy_repo import get_existing_trial_draft
from integrations.repo.bot_pause_repo import is_bot_paused
from integrations.repo.existing_client_repo import is_existing_academy_client
from integrations.repo.postgres import cancel_booking_trial
from integrations.sheets.booking_sheets import upsert_booking_row, refresh_all_bookings, refresh_week_sheet
from integrations.sheets.trial_sheets import refresh_all_trials
from rag.retriever import intent_context, retrieve_context
from handlers.whatsapp_client import send_text_message as _send_text_message, mark_as_read, download_media
from handlers.sessions.booking_session import handle_booking_turn, start_booking_flow
from handlers.edit_booking import handle_edit_request as handle_edit_booking_request
from handlers.edit_trial import (
    handle_edit_request as handle_edit_trial_request,
    handle_cancel_trial_request,
    handle_trial_status_request,
)
from integrations import booking_service, payment_validation, booking, trial
from utils import display_end_time
from integrations.repo import academy_repo, booking_repo
from integrations.repo import postgres as _pg
from handlers.llm_booking_flow import LlmBookingFlowHandler
from handlers.base_classes.base_checker import BaseChecker
import config

logger = logging.getLogger(__name__)

RESET_COMMANDS = {"/reset", "/сброс", "/тазалау", "сброс", "reset"}


def send_text_message(channel: OutboundChannel, to: str, text: str) -> dict | None:
    """Best-effort outbound send for inbound handling.

    A provider-side delivery failure, such as YCloud denying access to the
    configured `from` number, must not make the inbound message processing look
    failed after the bot has already updated sessions, bookings, or history.
    """
    try:
        return _send_text_message(channel, to, text)
    except requests.RequestException as exc:
        logger.error(
            "Outbound WhatsApp delivery failed via %s/%s to %s: %s",
            channel.provider,
            channel.phone_number_id,
            to,
            exc,
        )
        return None


# ── Pre-LLM confirmation short-circuit helpers ────────────────────────────────
# The short-circuit decides, WITHOUT an LLM call, whether a message on a
# ready-to-confirm draft is a plain confirm/cancel. It reuses BaseChecker's
# YES/NO word sets as the single source of truth, but matches on word
# BOUNDARIES rather than substrings so an edit request like "давай 19:00" is
# NOT misread as "да" (confirm) — it falls through to the LLM as before.
def _build_confirm_re(words: set[str]):
    # Alphanumeric words match on \b…\b; emoji/symbol tokens (e.g. "👍") have no
    # word chars, so \b can't anchor them — those are matched by substring below.
    alnum = [re.escape(w) for w in words if any(ch.isalnum() for ch in w)]
    return re.compile(r"\b(?:" + "|".join(alnum) + r")\b") if alnum else None


_YES_RE = _build_confirm_re(BaseChecker._YES_WORDS)
_NO_RE = _build_confirm_re(BaseChecker._NO_WORDS)
_YES_SYMBOLS = tuple(w for w in BaseChecker._YES_WORDS if not any(ch.isalnum() for ch in w))

# Kazakh-specific confirm/cancel tokens — used only to pick a reply language
# without an LLM call. The confirm buttons are localized, so a
# "Растаймын✅"/"Бас тартамын❌" reply implies kk, while "Подтверждаю✅"/"Отмена❌"
# (and anything ambiguous) defaults to ru.
_CONFIRM_KK_WORDS = {
    "иә", "растаймын", "жарайды", "дұрыс",
    "жоқ", "бас тартамын", "болмайды", "өзгерт", "бастапқы",
}


def _confirm_intent(text: str) -> str | None:
    """Return 'yes'/'no'/None for a confirmation, using word-boundary matching."""
    lower = text.lower().strip()
    if (_YES_RE and _YES_RE.search(lower)) or any(s in lower for s in _YES_SYMBOLS):
        return "yes"
    if _NO_RE and _NO_RE.search(lower):
        return "no"
    return None


def _infer_confirm_lang(text: str) -> str:
    lower = text.lower()
    return "kk" if any(w in lower for w in _CONFIRM_KK_WORDS) else "ru"

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
    "Оттуда же въезд на парковку\n"
    "Ссылка на 2GIS: https://2gis.kz/astana/geo/70000001074875383\n\n"
    "–––\n\n"
    "📍 Сығанақ 6Ф, Mechta және Tumar СО қарсы.\n"
    "Кіру және кіреберіс Тәттімбет көшесі жағынан.\n"
    "Көлік тұрағы да сол жерде\n"
    "2GIS сілтемесі: https://2gis.kz/astana/geo/70000001074875383\n"
)

_ACADEMY_ADMIN_PHONE = "+7 700 555 6000"

# WhatsApp sends voice notes as "audio"; round video notes arrive as "video".
_ACADEMY_VOICE_VIDEO_TYPES = {"audio", "voice", "video"}
_ACADEMY_VOICE_VIDEO_REPLY = (
    "Кешіріңіз, дауыстық және бейне хабарламаларды тыңдай алмаймын 🙏 "
    "Сұрағыңызды мәтінмен жазып жіберсеңіз, бірден жауап беремін.\n\n"
    "–––\n\n"
    "Извините, я не могу прослушать голосовые и видеосообщения 🙏 "
    "Напишите, пожалуйста, ваш вопрос текстом — я сразу отвечу."
)

_ACADEMY_UNCLEAR_REPLY = {
    "kk": "Күте тұрыңыз, сізбен мектеп әкімшісі байланысады.",
    "ru": "Подождите, пожалуйста, с вами свяжется администратор школы.",
}

_PHONE_RE = re.compile(r"\+?\d[\d\s()\-]{8,}\d")


def _has_foreign_phone(text: str) -> bool:
    """True if the text names any phone number other than the school admin's.

    Academy replies must never carry the arena's or an invented number.
    """
    admin = re.sub(r"\D", "", _ACADEMY_ADMIN_PHONE)
    for match in _PHONE_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group())
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        # Full KZ numbers (+7 7xx… mobile, +7 71xx… city) are 11 digits starting
        # with 77 — this keeps price ranges like "10 000 - 15 000" from matching.
        if len(digits) == 11 and digits.startswith("77") and digits != admin:
            return True
    return False


_ACADEMY_INFO_INTENTS = {
    "question_personal_training",
    "question_adult_training",
    "question_child_training",
    "question_payment",
    "question_invoice",
    "question_discounts",
    "question_contacts",
}

builder = BasePromptBuilder({}, "", (), ())


def _academy_info_reply(intent: str, bot_name: str, lang: str) -> str:
    is_boxing = bot_name == "dopsy_boxing"

    if intent in {"question_personal_training", "question_adult_training"}:
        if lang == "kk":
            return (
                "Жеке немесе ересектерге арналған жаттығулар бойынша баға мен бос уақытты "
                f"әкімші нақтылайды: {_ACADEMY_ADMIN_PHONE}."
            )
        subject = "персональным и взрослым тренировкам" if is_boxing else "индивидуальным условиям"
        return (
            f"По {subject} стоимость и свободное время уточняет администратор: "
            f"{_ACADEMY_ADMIN_PHONE}."
        )

    if intent == "question_child_training":
        if lang == "kk":
            return (
                "Балалар топтары бар. Нақты топ жасына, дайындық деңгейіне және мектеп "
                "ауысымына қарай таңдалады. Сынақ сабағына жазғыңыз келсе, жазыңыз: "
                "«сынақ сабаққа жазу»."
            )
        age_text = "7–16 лет" if is_boxing else "5–15 лет"
        return (
            f"Для детей есть группы {age_text}. Точная группа зависит от возраста, уровня "
            "подготовки и школьной смены. Если хотите записать на пробное, напишите: "
            "«запишите на пробное»."
        )

    if intent == "question_payment":
        if lang == "kk":
            return f"Төлем шарттарын әкімші нақтылайды: {_ACADEMY_ADMIN_PHONE}."
        return f"Условия оплаты уточнит администратор: {_ACADEMY_ADMIN_PHONE}."

    if intent == "question_invoice":
        if lang == "kk":
            return f"Төлемге шотты мектеп әкімшісі жібереді. Әкімші телефоны: {_ACADEMY_ADMIN_PHONE}."
        return f"Счёт на оплату отправит администратор школы. Телефон администратора: {_ACADEMY_ADMIN_PHONE}."

    if intent == "question_discounts":
        if lang == "kk":
            return f"Жеңілдіктер мен арнайы шарттарды әкімші нақтылайды: {_ACADEMY_ADMIN_PHONE}."
        return f"Скидки и специальные условия уточнит администратор: {_ACADEMY_ADMIN_PHONE}."

    if lang == "kk":
        return (
            f"Мекенжай: Астана, Сығанақ 6Ф. Әкімші телефоны: {_ACADEMY_ADMIN_PHONE}."
        )
    return f"Адрес: Астана, Сыганак 6Ф. Телефон администратора: {_ACADEMY_ADMIN_PHONE}."


def _with_pending(answer: str, pending: str | None) -> str:
    return f"{answer}\n\n{pending}" if pending else answer


def _dispatch_in_trial_session(
    intent: str | None,
    session: dict,
    pending: str | None,
    chat_id: str,
    sender_id: str,
    bot_name: str,
    user_text: str,
    history: list,
    lang: str,
) -> str | None:
    """Route one message that arrived during an LLM-flow signup.

    Returns the reply, or None to fall through to RAG/LLM (the caller then
    appends the pending step). A None intent means the router failed — the
    message stays in the flow, as it did before routing ran mid-signup.
    """
    if intent in (None, "trial_new", "trial_continue", "trial_edit"):
        return LlmTrialFlowHandler().handle_session_turn(
            chat_id, sender_id, bot_name, user_text, history, session
        )
    if intent == "trial_cancel":
        return handle_cancel_trial_request(chat_id, sender_id, bot_name)
    if intent == "trial_status":
        return _with_pending(handle_trial_status_request(sender_id, bot_name, lang), pending)
    if intent == "human_help":
        return _with_pending(
            "Передам администратору. Он сможет уточнить детали по записи.\n\n"
            "Әкімшіге жіберемін. Ол жазылым бойынша нақтылап береді.",
            pending,
        )
    # Info questions (schedule, trainers, prices, ...) are answered by RAG/LLM.
    return None


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


def handle_incoming_message(payload: IncomingWhatsAppMessage) -> None:
    """
    Parse a WhatsApp Cloud API webhook payload and respond.
    Supports both individual and group messages.
    """
    sender_id = ""
    # channel = None
    try:
        phone_number_id = config.resolve_inbound_phone_number_id(
            payload.provider,
            payload.business.phone_number_id,
            payload.business.phone,
        )

        bot_config = config.get_bot_config(phone_number_id)

        channel = OutboundChannel(
            provider=payload.provider,
            phone_number_id=phone_number_id
        )

        if not bot_config:
            logger.warning("Unknown phone_number_id from webhook: %s", phone_number_id)
            return


        msg_type = payload.message_type
        message_id = payload.whatsapp_message_id
        sender_id = payload.customer.phone  # sender phone number

        # Bot paused for this contact — a manager is handling them. Mark the
        # message read so their inbox stays clean, but send no auto-reply.
        if is_bot_paused(sender_id):
            if message_id:
                mark_as_read(channel, message_id)
            logger.info("[PAUSED] Bot paused for %s — skipping auto-reply", sender_id)
            return

        # Existing academy clients are handled by managers: the bot has no record
        # of their earlier chats. Left unread so the message stays visible to them.
        is_academy = bot_config["name"] != "dopsy_bot"
        if is_academy and is_existing_academy_client(sender_id):
            logger.info("[EXISTING] %s is an existing client of %s — skipping auto-reply",
                        sender_id, bot_config["name"])
            return

        if is_academy and msg_type not in ("text", "interactive"):
            # Voice and video notes get a polite "please write instead".
            if msg_type in _ACADEMY_VOICE_VIDEO_TYPES:
                if message_id:
                    mark_as_read(channel, message_id)
                send_text_message(channel, sender_id, _ACADEMY_VOICE_VIDEO_REPLY)
                return
            # Receipts, photos, files, reactions: no reply, and left unread so
            # the school admin notices and handles them manually.
            logger.info("[SKIP] %s message from %s via %s — no auto-reply",
                        msg_type, sender_id, bot_config["name"])
            return

        # Mark as read immediately
        if message_id:
            mark_as_read(channel, message_id)

        # Document = payment receipt — confirm the booking.
        # Disabled by default while keeping the receipt code available for a
        # later return to manual PDF validation.
        if msg_type == "document" and config.PAYMENT_RECEIPT_RECOGNITION_ENABLED:
            _handle_payment_receipt(channel, sender_id, payload.media)
            return

        # Only handle text messages
        if msg_type not in ["text", "interactive"]:
            send_text_message(
                channel,
                sender_id,
                "Извините, я ассистент-бот и могу распознавать только текст, не могли бы вы отправлять только текстовые сообщения, пожалуйста. "
                "/ Кешіріңіз, мен бот-ассистентпін, тек қана мәтіндік хабарламаны оқи аламын. Өтініш, мәтіндік хабарлама жіберіңіз.",
            )
            return

        user_text = ""
        if (msg_type == "interactive" and payload.interactive
                and payload.interactive.button_reply
                and payload.interactive.button_reply.title):
            user_text = str(payload.interactive.button_reply.title)

        if msg_type == "text":
            user_text = str(payload.text)

        # Determine chat context key.
        # For group messages Meta Cloud API includes a "context" object; we use
        # sender_id so each person in a group gets a shared thread identified by
        # their own number (simplest approach — change to group JID if needed).
        chat_id = f"{phone_number_id}:{sender_id}"


        # Handle reset command
        if user_text.lower() in RESET_COMMANDS:
            logger.info("[RESET] Reset command by user %s: command -> %s", sender_id, user_text)
            if bot_config["name"] == "dopsy_bot":
                draft = get_existing_draft(sender_id)
                if draft:
                    cancel_booking_trial(bot_config["name"], draft["id"])
                refresh_week_sheet()
                refresh_all_bookings()
            else:
                draft = get_existing_trial_draft(sender_id, bot_config["name"])
                if draft:
                    cancel_booking_trial(bot_config["name"], draft["id"])
                    refresh_all_trials()

            clear_history(chat_id)
            send_text_message(
                channel,
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

        try:
            context = retrieve_context(user_text, bot_name=bot_config["name"])
            logger.info("[RAG] Retrieved %d chars of context for: %.80s", len(context), user_text)
        except Exception:
            logger.exception(
                "[RAG] Context retrieval failed for: %.80s — continuing without RAG context",
                user_text,
            )
            context = ""

        history = get_history(chat_id)
        logger.info("[LLM] History length: %d messages", len(history))

        # Academy only: an LLM-flow signup in progress and its pending step.
        booking_session = None
        pending = None

        # 1. Booking handler (Bot 1 — Dopshy field rental)
        #
        # Two-LLM architecture:
        #   a) Active session → existing deterministic step handler
        #      (handles step_date, step_time, … step_confirm)
        #   b) No active session → LLM1 (intent + extraction) → LLM2 (process)
        #   c) If LLM2 returns None → fall through to legacy RAG/LLM pipeline
        if bot_config["name"] == "dopsy_bot":
            logger.info("[BOOKING] Checking booking branch for chat_id=%s", chat_id)

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
                    send_text_message(channel, sender_id, booking_reply)
                    return

            # (a.5) Pre-LLM confirmation short-circuit.
            # When the user already has a draft with every required field filled,
            # a plain "да"/"растаймын"/"нет" is a confirm or cancel of THAT draft.
            # Handle it deterministically here so an LLM1 intent misclassification
            # (e.g. a bare "да" routed to `other`) can't drop a valid confirmation.
            # A message that isn't a yes/no word falls through to the LLM as before,
            # preserving the edit/continue path.
            _ready_draft = get_existing_draft(sender_id)
            if _ready_draft and LlmBookingFlowHandler.helper.is_ready_for_confirm(_ready_draft):
                _confirm = _confirm_intent(user_text)
                if _confirm in ("yes", "no"):
                    _lang = _infer_confirm_lang(user_text)
                    logger.info(
                        "[BOOKING] Pre-LLM confirm short-circuit (%s) for draft id=%s",
                        _confirm, _ready_draft["id"],
                    )
                    reply = LlmBookingFlowHandler().handle(
                        {}, chat_id, user_text, sender_id, _lang, channel.provider
                    )
                    append_message(chat_id, "user", user_text)
                    append_message(chat_id, "assistant", reply)
                    send_text_message(channel, sender_id, reply)
                    return

            intent, lang = route_incoming_message(history, user_text)
            logger.info("[BOOKING] Intent detection replied, Intent is %s, lang=%s", intent, lang)

            free = booking.get_free_windows()
            availability_ctx = booking.format_availability_context(free, lang)
            logger.info("[BOOKING] Injecting availability context (%d free windows) into LLM call", len(free))
            context = f"{availability_ctx}\n\n{context}" if context else availability_ctx

            if intent == 'question_price':
                send_text_message(channel, sender_id, process_field_prices(lang))
                return

            elif intent == 'question_location':
                send_text_message(channel, sender_id, _LOCATION_MESSAGE)
                return

            elif intent == 'question_slots':
                # If the user named a date, show slots for that day only; otherwise
                # fall back to the full 7-day overview. _check_date_only also handles
                # the "no free slots on that date" case (lists alternative dates).
                slots_date = extract_booking_details(history, user_text).get("date")
                logger.info("[BOOKING] question_slots — extracted date=%s", slots_date)
                if slots_date:
                    response = check_slots({"date": slots_date, "lang": lang},
                                           chat_id, user_text, sender_id, lang)
                else:
                    response = (f"Давайте забронируем! Вот доступные слоты\n–––––\n"
                                f"Брондайық! Бос слоттар:\n\n{availability_ctx}\n\n"
                                f"Укажите дату, время, или размер поля.\n–––––\n"
                                f"Күнді, уақытты немесе алаң өлшемін жазыңыз.")

                send_text_message(channel, sender_id, response)
                return

            elif intent in ['booking_new', 'booking_continue']:
                if has_awaiting_payments(sender_id):
                    send_text_message(channel, sender_id,
                                      'Вы не можете создать новую бронь пока не оплатите предыдущую! \n'
                                      '\n----\n'
                                      'Осығын дейінгі брондарыңызды төлемей жаңа брондар қоя алмайсыз! \n')
                    clear_history(chat_id)
                    return
                extracted_data = extract_booking_details(history, user_text)
                logger.info("[BOOKING] Data Extracted: %s", extracted_data)
                handler = LlmBookingFlowHandler()
                reply = handler.handle(extracted_data, chat_id, user_text, sender_id, lang,
                                       channel.provider)
                append_message(chat_id, "user", user_text)
                append_message(chat_id, "assistant", reply)
                logger.info("[LLM2] reply: %s", reply)
                send_text_message(channel, sender_id, reply)
                return

            elif intent == 'booking_edit':
                logger.info("[EDIT] Intent booking_edit detected for %s", sender_id)
                extracted = extract_booking_details(history, user_text)

                target = get_existing_draft(sender_id)
                if target:
                    handler = LlmBookingFlowHandler()
                    reply = handler.handle(extracted, chat_id, user_text, sender_id, lang,
                                           channel.provider)
                    send_text_message(channel, sender_id, reply)
                    return

                diff = {}
                if extracted.get("date"):
                    diff["date"] = extracted["date"]
                if extracted.get("time_start"):
                    diff["time_start"] = extracted["time_start"]
                if extracted.get("time_end"):
                    diff["time_end"] = extracted["time_end"]
                if extracted.get("field"):
                    format_str = extracted["field"]
                    diff["format"] = format_str
                    diff["field"] = None
                    if format_str:
                        diff["field"] = LlmBookingFlowHandler()._resolve_field_id(format_str, diff)
                if extracted.get("players"):
                    diff["players"] = extracted["players"]
                if extracted.get("name"):
                    diff["customer_name"] = extracted["name"]
                reply = handle_edit_booking_request(chat_id, sender_id, diff)
                append_message(chat_id, "user", user_text)
                append_message(chat_id, "assistant", reply)
                send_text_message(channel, sender_id, reply)
                return

            elif intent == 'booking_status':
                logger.info("[BOOKING] Fetching user's own bookings")
                bookings = booking_repo.get_user_upcoming_bookings(sender_id)
                send_text_message(channel, sender_id,
                                  booking.format_user_booking_context(bookings, lang))
                return

            elif intent == 'booking_cancel':
                logger.info("[BOOKING] Cancelling all drafts of the user")
                cancelled = booking_repo.cancel_draft_awaiting_payment(sender_id)
                clear_history(chat_id)
                refresh_all_bookings()
                refresh_week_sheet()
                send_text_message(channel, sender_id, _CANCEL_STATUS[cancelled])
                return

            # (c) Neither handled the message → fall through to RAG/LLM
            logger.info("[BOOKING] Two-LLM flow did not handle — falling through to RAG/LLM")

        else:
            # Academy bots (chatbot_2, dopsy_boxing) — mirrors the arena branch:
            #   (a) Active deterministic session → the old step handler finishes it
            #   (b) Otherwise → LLM1 (intent + extraction) → LLM2 (trial flow)
            #   (c) Questions and anything else → fall through to RAG/LLM
            bot_name = bot_config["name"]
            logger.info("[TRIAL] Checking trial branch for chat_id=%s", chat_id)

            # (a) An LLM-flow signup in progress: a message that plainly
            # answers the pending step goes straight to the flow; anything else
            # is routed below like any other message, and the pending step is
            # re-shown after the answer.
            session = _pg.get_active_session(bot_name, chat_id)
            booking_session = (
                session if session and session["state"] in LLM_TRIAL_FLOW_STATES else None
            )
            if booking_session:
                fast_reply = LlmTrialFlowHandler().try_fast_path(
                    chat_id, sender_id, bot_name, user_text, history, booking_session
                )
                if fast_reply is not None:
                    append_message(chat_id, "user", user_text)
                    append_message(chat_id, "assistant", fast_reply)
                    send_text_message(channel, sender_id, fast_reply)
                    return

            # (a') A step-flow conversation started before this deploy, or a
            # cancel selection, must be allowed to finish; both flows write the
            # same draft row, so the session has to win while it exists.
            elif session:
                trial_reply = handle_trial_turn(
                    chat_id, phone_number_id, sender_id, user_text, bot_name
                )
                if trial_reply is not None:
                    logger.info(
                        "[TRIAL] Session handler replied: %.120s", trial_reply,
                    )
                    append_message(chat_id, "user", user_text)
                    append_message(chat_id, "assistant", trial_reply)
                    send_text_message(channel, sender_id, trial_reply)
                    return

            if not booking_session and is_trial_greeting(user_text):
                handle_reply = (
                    "Здравствуйте! Чем могу помочь?\n\n"
                    "Сәлеметсіз бе! Қалай көмектесе аламын?"
                )
                append_message(chat_id, "user", user_text)
                append_message(chat_id, "assistant", handle_reply)
                send_text_message(channel, sender_id, handle_reply)
                return

            if not booking_session and is_bot_identity_question(user_text):
                handle_reply = (
                    "Я бот-ассистент академии. Могу ответить на вопросы о тренировках "
                    "и помочь записаться на пробное занятие.\n\n"
                    "Мен академияның бот-ассистентімін. Жаттығулар туралы сұрақтарға "
                    "жауап беріп, сынақ сабағына жазуға көмектесемін."
                )
                append_message(chat_id, "user", user_text)
                append_message(chat_id, "assistant", handle_reply)
                send_text_message(channel, sender_id, handle_reply)
                return

            if not booking_session and is_trial_acknowledgement(user_text):
                handle_reply = (
                    "Хорошо. Если появятся вопросы по тренировкам или пробному занятию, напишите.\n\n"
                    "Жақсы. Жаттығулар немесе сынақ сабағы бойынша сұрақ болса, жазыңыз."
                )
                append_message(chat_id, "user", user_text)
                append_message(chat_id, "assistant", handle_reply)
                send_text_message(channel, sender_id, handle_reply)
                return

            free = trial.get_trial_daytime(bot_config["name"], None)
            availability_ctx = trial.format_availability_context(free)
            logger.info("[TRIAL] Injecting availability context (%d free trial times) into LLM call", len(free))
            context = f"{availability_ctx}\n\n{context}" if context else availability_ctx

            pending = (
                LlmTrialFlowHandler().pending_prompt(bot_name, booking_session)
                if booking_session else None
            )
            trial_intent, trial_lang = route_trial_message(
                history, user_text, pending=pending,
                # Router failure mid-signup keeps the message in the flow.
                fallback=None if booking_session else "other",
            )
            logger.info("[TRIAL] Intent detection replied, Intent is %s, lang=%s",
                        trial_intent, trial_lang)

            if booking_session:
                trial_lang = booking_session["params"].get("lang") or trial_lang
                handle_reply = _dispatch_in_trial_session(
                    trial_intent, booking_session, pending, chat_id, sender_id,
                    bot_name, user_text, history, trial_lang,
                )
                if handle_reply is not None:
                    append_message(chat_id, "user", user_text)
                    append_message(chat_id, "assistant", handle_reply)
                    send_text_message(channel, sender_id, handle_reply)
                    return

            # The router is tuned to over-trigger signup intent. The booking flow
            # cannot answer a price/schedule/location question, so veto the router
            # for those — but only for clients with no draft in progress, so a
            # side-question mid-signup still reaches the flow's own interrupt
            # handling instead of being diverted to RAG. This runs BEFORE the
            # info-intent branch below: those two are complementary, not
            # alternatives. _ACADEMY_INFO_INTENTS catches questions the router
            # labelled correctly; this catches questions it labelled as signup.
            if (
                trial_intent in ("trial_new", "trial_continue")
                and is_trial_factual_question(user_text)
                and not academy_repo.get_existing_trial_draft(sender_id, bot_config["name"])
            ):
                logger.info(
                    "[TRIAL] Router said %s but message is a factual question and no "
                    "draft is in progress — falling through to RAG/LLM", trial_intent
                )
                trial_intent = "other"

            if trial_intent in _ACADEMY_INFO_INTENTS:
                handle_reply = _academy_info_reply(trial_intent, bot_config["name"], trial_lang)
                append_message(chat_id, "user", user_text)
                append_message(chat_id, "assistant", handle_reply)
                send_text_message(channel, sender_id, handle_reply)
                return

            if trial_intent == "question_schedule":
                # A factual "what's on Wednesday and Thursday" lookup, answered
                # with the real schedule regardless of whether a signup is in
                # progress. Only handled here when a weekday was actually named
                # — otherwise this falls through to RAG/LLM exactly as before.
                weekdays = trial.parse_weekdays(user_text)
                if weekdays:
                    classes = trial.list_classes_by_weekday(bot_config["name"], weekdays)
                    handle_reply = trial.format_weekday_schedule(classes, trial_lang)
                    append_message(chat_id, "user", user_text)
                    append_message(chat_id, "assistant", handle_reply)
                    send_text_message(channel, sender_id, handle_reply)
                    return

            if trial_intent in ("trial_new", "trial_continue"):
                handle_reply = LlmTrialFlowHandler().handle(
                    chat_id, sender_id, bot_config["name"], user_text, history, trial_lang
                )
                append_message(chat_id, "user", user_text)
                append_message(chat_id, "assistant", handle_reply)
                send_text_message(channel, sender_id, handle_reply)
                return

            if trial_intent == "trial_cancel":
                handle_reply = handle_cancel_trial_request(chat_id, sender_id, bot_config["name"])
                append_message(chat_id, "user", user_text)
                append_message(chat_id, "assistant", handle_reply)
                send_text_message(channel, sender_id, handle_reply)
                return

            if trial_intent == "trial_status":
                handle_reply = handle_trial_status_request(sender_id, bot_config["name"], trial_lang)
                append_message(chat_id, "user", user_text)
                append_message(chat_id, "assistant", handle_reply)
                send_text_message(channel, sender_id, handle_reply)
                return

            if trial_intent == "trial_edit":
                handle_reply = handle_edit_trial_request(
                    chat_id, sender_id, {}, bot_config["name"], user_text, history, trial_lang
                )
                append_message(chat_id, "user", user_text)
                append_message(chat_id, "assistant", handle_reply)
                send_text_message(channel, sender_id, handle_reply)
                return

            if trial_intent == "human_help":
                handle_reply = (
                    "Передам администратору. Он сможет уточнить детали по записи.\n\n"
                    "Әкімшіге жіберемін. Ол жазылым бойынша нақтылап береді."
                )
                append_message(chat_id, "user", user_text)
                append_message(chat_id, "assistant", handle_reply)
                send_text_message(channel, sender_id, handle_reply)
                return

            # Voucher questions: the LLM answers from the full voucher guide only,
            # instead of the usual top-k chunks and availability.
            if trial_intent == "question_voucher":
                voucher_doc = intent_context(bot_name, trial_intent)
                if voucher_doc:
                    context = voucher_doc

            # Meaning unclear: hand over to the admin rather than let the LLM guess.
            if trial_intent == "unclear":
                handle_reply = _ACADEMY_UNCLEAR_REPLY.get(trial_lang, _ACADEMY_UNCLEAR_REPLY["kk"])
                append_message(chat_id, "user", user_text)
                append_message(chat_id, "assistant", handle_reply)
                send_text_message(channel, sender_id, handle_reply)
                return

        # 4. Generate response
        reply, tool_call = get_ai_response(
            phone_number_id=phone_number_id,
            chat_id=chat_id,
            user_message=user_text,
            history=history,
            context=context,
        )
        logger.info("[LLM] Raw reply (%.120s) | tool_call=%s", reply, tool_call)

        if is_academy and _has_foreign_phone(reply):
            logger.warning("[LLM] Academy reply named a foreign phone number — replaced: %.200s", reply)
            reply = _ACADEMY_UNCLEAR_REPLY.get(builder.detect_lang(user_text), _ACADEMY_UNCLEAR_REPLY["kk"])

        # 5a. Mid-signup the flow already owns the draft: starting a signup is
        # a no-op and an edit goes through the current step, which only resets
        # the chosen day when a slot-affecting field really changed.
        if tool_call and booking_session:
            if tool_call["name"] == "start_trial":
                logger.info("[TRIAL] Ignoring start_trial tool — signup already in progress")
                tool_call = None
            elif tool_call["name"] == "edit_trial":
                edit_reply = LlmTrialFlowHandler().handle_session_turn(
                    chat_id, sender_id, bot_config["name"], user_text, history, booking_session
                )
                reply = f"{reply}\n\n{edit_reply}" if reply else edit_reply
                tool_call = None
                pending = None

        # 5. LLM may have asked us to launch a deterministic sub-flow.
        if tool_call:
            handle_reply = reply
            if tool_call["name"] == "start_trial":
                if is_trial_greeting(user_text) or is_trial_acknowledgement(user_text) or is_bot_identity_question(user_text):
                    logger.info("[TRIAL] Ignoring start_trial tool for greeting/ack/bot-identity text")
                    handle_reply = (
                        "Хорошо. Чем могу помочь по академии?\n\n"
                        "Жақсы. Академия бойынша қалай көмектесе аламын?"
                    )
                    reply = handle_reply
                else:
                    lang = builder.detect_lang(user_text)
                    logger.info("[TRIAL] LLM called start_trial tool — starting gated trial flow (lang=%s)", lang)
                    handle_reply = LlmTrialFlowHandler().handle(
                        chat_id, sender_id, bot_config["name"], user_text, history, lang
                    )

            elif tool_call["name"] == "edit_trial":
                logger.info("[EDIT] LLM called edit_trial tool — diff=%s", tool_call["args"])
                handle_reply = handle_edit_trial_request(
                    chat_id, sender_id, tool_call["args"], bot_config["name"], user_text, history, builder.detect_lang(user_text)
                )

            elif tool_call["name"] == "cancel_trial":
                logger.info("[CANCEL] LLM called cancel_trial tool")
                handle_reply = handle_cancel_trial_request(chat_id, sender_id, bot_config["name"])

            reply = (reply + "\n\n" + handle_reply) if reply else handle_reply

        # 5b. A side question answered mid-signup: remind the pending step.
        elif pending:
            reply = f"{reply}\n\n{pending}" if reply else pending

        # 6. Save to history
        append_message(chat_id, "user", user_text)
        append_message(chat_id, "assistant", reply)

        # 7. Send reply
        if send_text_message(channel, sender_id, reply) is not None:
            logger.info("Replied to %s via bot %s", sender_id, bot_config["name"])

    except Exception as exc:
        logger.exception("Error handling message: %s", exc)
        # Best-effort fallback reply
        try:
            phone_number_id = config.resolve_inbound_phone_number_id(
                payload.provider,
                payload.business.phone_number_id,
                payload.business.phone,
            )

            bot_config = config.get_bot_config(phone_number_id)

            if sender_id and bot_config:
                send_text_message(
                    channel,
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


def _handle_payment_receipt(channel: OutboundChannel, sender_phone: str,
                            media: WhatsAppMedia | None = None) -> None:
    """
    Called when a user sends a document (assumed to be a payment receipt).
    Finds their most recent awaiting_payment booking, confirms it via the
    service layer, and refreshes Google Sheets in the background.
    """
    booking = booking_repo.get_awaiting_payment_booking(sender_phone)

    if not booking:
        logger.info("[PAYMENT] Document from %s — no awaiting_payment booking found", sender_phone)
        send_text_message(
            channel,
            sender_phone,
            "У вас нет активной брони. Если это ошибка, свяжитесь с администратором."
            "\n\nСізде белсенді бронь табылмады. Егер бұл қателік болса администратормен хабарласыңыз.",
        )
        return

    # TRANSITIVE BOOKING: use combined price of both bookings for payment validation
    combined_price = booking_repo.get_transitive_total_price(booking["id"])
    if combined_price is not None:
        booking["price_total"] = combined_price

    # Download the receipt PDF and validate it before confirming.
    pdf = download_media(channel, media) if media else None
    if not pdf:
        logger.warning("[PAYMENT] Could not download media for booking id=%d", booking["id"])
        send_text_message(
            channel, sender_phone,
            "Не удалось загрузить чек. Пожалуйста, отправьте PDF-чек ещё раз.\n"
            "Чекті жүктеу мүмкін болмады. PDF-чекті қайта жіберіңіз.",
        )
        return

    result = payment_validation.validate_receipt(booking, pdf)

    if not result["ok"]:
        booking_service.reject_payment(booking["id"], result["reason"], result["parsed"])
        logger.info("[PAYMENT] Booking id=%d receipt rejected: %s", booking["id"], result["code"])
        send_text_message(
            channel, sender_phone,
            _format_payment_reject_message(result["code"], result["parsed"], booking),
        )
        return

    res = booking_service.submit_payment_proof(
        booking["id"], parsed=result["parsed"], proof_media_id=media.id
    )
    if not res["ok"]:
        msg = ("Этот чек уже был использован. Свяжитесь с администратором.\n"
               "Бұл чек бұрын қолданылған. Әкімшімен хабарласыңыз."
               if res["code"] == "PAYMENT_DUPLICATE"
               else "Не удалось подтвердить оплату. Свяжитесь с администратором.\n"
                    "Төлемді растау мүмкін болмады. Әкімшімен хабарласыңыз.")
        logger.error("[PAYMENT] submit_payment_proof failed for id=%d: %s", booking["id"], res)
        send_text_message(channel, sender_phone, msg)
        return

    logger.info("[PAYMENT] Booking id=%d confirmed for phone=%s", booking["id"], sender_phone)

    _refresh_booking_sheet(booking, "confirmed")

    booking_date = booking["date"]
    ts = str(booking["time_start"])[:5]
    te = display_end_time(booking["time_end"])  # show an end-of-day 23:59 as 00:00
    price_line = fmt_price(booking["price_total"]) if booking.get("price_total") else ""

    paid = float(result["parsed"].get("amount") or 0)
    total = float(booking.get("price_total") or 0)
    remainder = max(0, total - paid)
    remainder_ru = f"\n💳 Остаток к оплате: {fmt_price(remainder)} — при желании можно оплатить заранее." if remainder > 0 else ""
    remainder_kk = f"\n💳 Төлем қалдығы: {fmt_price(remainder)} — қаласаңыз алдын ала төлей аласыз." if remainder > 0 else ""

    send_text_message(
        channel,
        sender_phone,
        f"✅ Оплата получена! Бронь подтверждена.\n\n"
        f"📅 {booking_date}\n"
        f"⏰ {ts}–{te}\n"
        f"⚽ {booking['format']}\n"
        f"💰 {price_line}"
        f"{remainder_ru}\n\n"
        f"Ждём вас! 🙌\n\n"
        f"— — —\n"
        f"✅ Төлем қабылданды! Брондау расталды.\n\n"
        f"📅 {booking_date}\n"
        f"⏰ {ts}–{te}\n"
        f"⚽ {booking['format']}\n"
        f"💰 {price_line}"
        f"{remainder_kk}\n\n"
        f"Сізді күтеміз! 🙌",
    )
