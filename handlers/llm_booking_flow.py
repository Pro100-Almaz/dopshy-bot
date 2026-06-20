"""
Non-deterministic LLM-driven booking flow.

Unlike the step-by-step BookingStepHandler (booking_session.py), this flow:
- Accepts booking data in any order (no fixed step sequence)
- Uses LLM to extract intent + booking params from natural language
- Creates or continues a draft booking based on whatever data is available
- Checks availability depending on which combination of fields is present
- Returns single-language responses based on LLM-detected lang (ru/kk)

Continuing a booking is identified by phone + state='draft' in the bookings table —
no booking_sessions row is needed, avoiding the step-ordering bugs that a sequential
state machine would cause when data arrives out of order.

Seven checking rules (see _evaluate_and_respond):
  1. date only            → free fields and time ranges for that date
  2. time_start+time_end  → available dates and fields for that interval
  3. field only           → dates and time ranges for that field
  4. date+time+field      → is this specific slot free?
  5. date+time            → which fields are available?
  6. one of start/end     → ask user to provide both
  7. name / players       → basic validation
"""

import logging
import re
import uuid

from datetime import datetime

import config
from chat.conversation import clear_history
from handlers.payment.pricing import calculate_full_booking_price, fmt_price
from handlers.base_classes.base_asker import BaseAsker
from handlers.base_classes.base_button import BaseButton
from handlers.base_classes.base_checker import BaseChecker
from handlers.base_classes.base_draft_handler import BaseDraftHandler
from handlers.base_classes.base_format import BaseFormat
from handlers.base_classes.base_helper import BaseHelper
from integrations import booking as booking_logic
from integrations import booking_service
from integrations.booking import floor_time_to_30_minutes
from integrations.repo import booking_repo, postgres
from integrations.sheets.booking_sheets import refresh_all_bookings, refresh_week_sheet
from utils import is_past_booking_time

logger = logging.getLogger(__name__)


T = {
    "ask_date":             {"ru": "📅 На какую дату хотите забронировать?",
                             "kk": "📅 Қай күнге брондағыңыз келеді?"},
    "ask_time":             {"ru": "⏰ Укажите время (напр. *18:00 - 20:00*)",
                             "kk": "⏰ Уақытты жазыңыз (мыс. *18:00 - 20:00*)"},
    "ask_field":            {"ru": "⚽ Выберите размер поля:",
                             "kk": "⚽ Алаң өлшемін таңдаңыз:"},
    "ask_players":          {"ru": "👥 Сколько игроков будет?",
                             "kk": "👥 Қанша ойыншы болады?"},
    "ask_name":             {"ru": "👤 Укажите ваше имя:",
                             "kk": "👤 Атыңызды жазыңыз:"},
    "no_slots_7d":          {"ru": "Нет свободных слотов на ближайшие 7 дней.",
                             "kk": "Жақын 7 күнде бос слот жоқ."},
    "no_slots_date":        {"ru": "На {date} нет свободных слотов.\nВыберите другую дату.",
                             "kk": "{date} күнінде бос слот жоқ.\nБасқа күнді таңдаңыз."},
    "ask_time_polite":      {"ru": "📅Дата выбрана: {date}.\nНа какое время хотите бронировать?",
                             "kk": "📅{date} күні таңдалды.\nҚай уақытқа броньдағыңыз келеді?"},
    "available_dates":      {"ru": "Доступные даты:",
                             "kk": "Қолжетімді күндер:"},
    "slots_header":         {"ru": "📅 {date} — свободные слоты:",
                             "kk": "📅 {date} — бос слоттар:"},
    "write_time":           {"ru": "⏰ Укажите время (напр. *18:00 - 20:00*)",
                             "kk": "⏰ Уақытты жазыңыз (мыс. *18:00 - 20:00*)"},
    "provide_both_times":   {"ru": "Укажите время начала и окончания.\nНапр.: *18:00 - 20:00*",
                             "kk": "Басталу-аяқталу уақытын жазыңыз.\nМыс.: *18:00 - 20:00*"},
    "time_equal":           {"ru": "Время окончания должно быть позже начала.\nНапр.: *18:00 - 20:00*",
                             "kk": "Аяқталу уақыты басталудан кейін болуы керек.\nМыс.: *18:00 - 20:00*"},
    "format_not_found":     {"ru": "Формат \"{fmt}\" не найден. Доступные: {available}",
                             "kk": "\"{fmt}\" форматы табылмады. Қолжетімді: {available}"},
    "field_not_found":      {"ru": "Формат не найден. Доступные размеры:",
                             "kk": "Формат табылмады. Қолжетімді өлшемдер:"},
    "players_invalid":      {"ru": "Игроков должно быть > 0.",
                             "kk": "Ойыншылар саны 0-ден көп болуы керек."},
    "players_overflow":     {"ru": f"Макс. количество игроков: {config.MAX_PLAYERS}",
                             "kk": f"Макс. ойыншы саны: {config.MAX_PLAYERS}"},
    "field_free":           {"ru": "✅ {fmt} свободно\n{date} {ts}–{te}!",
                             "kk": "✅ {fmt} бос\n{date} {ts}–{te}!"},
    "field_taken":          {"ru": "❌ {fmt} занято {date} {ts}–{te}.",
                             "kk": "❌ {fmt} бос емес {date} {ts}–{te}."},
    "alternatives":         {"ru": "Доступные варианты:",
                             "kk": "Бос нұсқалар:"},
    "no_free_fields_slot":  {"ru": "Нет свободных полей {date} {ts}–{te}.",
                             "kk": "{date} {ts}–{te} бос алаң жоқ."},
    "available_time":       {"ru": "Доступное время:",
                             "kk": "Бос уақыт:"},
    "field_auto":           {"ru": "✅ {fmt} свободно {date} {ts}–{te}!",
                             "kk": "✅ {fmt} бос {date} {ts}–{te}!"},
    "choose_field":         {"ru": "📅 {date}, {ts}–{te}\n\n⚽ Выберите размер поля:",
                             "kk": "📅 {date}, {ts}–{te}\n\n⚽ Алаң өлшемін таңдаңыз:"},
    "time_available":       {"ru": "⏰ {ts}–{te} — доступно",
                             "kk": "⏰ {ts}–{te} — қолжетімді"},
    "no_fields_time":       {"ru": "На {ts}–{te} нет свободных полей.\nПопробуйте другое время.",
                             "kk": "{ts}–{te} аралығында бос алаң жоқ.\nБасқа уақыт көріңіз."},
    "which_date":           {"ru": "📅 На какую дату хотите бронировать?",
                             "kk": "📅 Қай күнге брондағыңыз келеді?"},
    "field_booked_date":    {"ru": "{fmt} занято {date}.",
                             "kk": "{fmt} {date} бос емес."},
    "other_options":        {"ru": "Другие варианты:",
                             "kk": "Басқа нұсқалар:"},
    "field_schedule":       {"ru": "⚽ {fmt}, {date}\nСвободное время: {times}",
                             "kk": "⚽ {fmt}, {date}\nБос уақыт: {times}"},
    "field_full_week":      {"ru": "{fmt} занято в ближайшие 7 дней.",
                             "kk": "{fmt} жақын 7 күнде бос емес."},
    "field_slots":          {"ru": "⚽ Давайте забронируем поле {fmt}!",
                             "kk": "⚽ {fmt} алаңын борндайық!"},
    "confirm_header":       {"ru": "📋 Детали брони:",
                             "kk": "📋 Брондау деректері:"},
    "confirm_question":     {"ru": "Подтвердить?",
                             "kk": "Растайсыз ба?"},
    "confirm_btn":          {"ru": "Подтверждаю✅",
                             "kk": "Растаймын✅"},
    "cancel_btn":           {"ru": "Отмена❌",
                             "kk": "Бас тартамын❌"},
    "slot_taken":           {"ru": "Этот слот заняли. Выберите другой.",
                             "kk": "Бұл слот алынды. Басқасын таңдаңыз."},
    "error":                {"ru": "Ошибка. Попробуйте ещё раз.",
                             "kk": "Қате. Қайталап көріңіз."},
    "slot_taken_confirm":   {"ru": "❌ {fmt} {date} {ts}–{te} занято.",
                             "kk": "❌ {fmt} {date} {ts}–{te} бос емес."},
    "booking_done":         {"ru": ("📋 Бронь оформлена!\n\n"
                                    "📅 {date}\n"
                                    "⏰ {ts}–{te}\n"
                                    "⚽ {fmt}\n"
                                    "👥 Игроков: {players}\n"
                                    "👤 Имя: {name}\n"
                                    "💰 {price}\n\n"
                                    "Оплатите аванс на сумму не менее 10тысяч тг:\n{pay_url}\n"
                                    "💳 По желанию вы можете оплатить полную сумму сразу.\n"
                                    "⚠️ Возврат при неявке не производится\n\n"
                                    "Отправьте PDF-чек сюда 🙏\n⚠️ 20 мин без оплаты — бронь отменится."),
                             "kk": ("📋 Брондау тіркелді!\n\n"
                                    "📅 {date}\n"
                                    "⏰ {ts}–{te}\n"
                                    "⚽ {fmt}\n"
                                    "👥 Ойыншылар: {players}\n"
                                    "👤 Аты: {name}\n"
                                    "💰 {price}\n\n"
                                    "Аванс ретінде кемінде 10мың тг төлем жасаңыз:\n{pay_url}\n"
                                    "💳 Қаласаңыз толық соманы бірден төлей аласыз.\n"
                                    "⚠️ Келмесеңіз төлем қайтарылмайды\n\n"
                                    "PDF-чек жіберіңіз 🙏\n⚠️ 20 мин төлемсіз — брондау жойылады.")},
    "cancelled":            {"ru": "Бронь отменена. Напишите, если что! 🙂",
                             "kk": "Брондау тоқтатылды. Қаласаңыз жазыңыз! 🙂"},
    "general_header":       {"ru": "Давайте забронируем! Свободные слоты:",
                             "kk": "Брондайық! Бос слоттар:"},
    "general_prompt":       {"ru": "Укажите дату, время или поле.",
                             "kk": "Күнді, уақытты немесе алаңды жазыңыз."},
    "no_slots_empty":       {"ru": "  (нет свободных слотов)",
                             "kk": "  (бос слот жоқ)"},
    "time_in_past":         {"ru": "⏰ Это время уже прошло. Укажите будущее время.",
                             "kk": "⏰ Бұл уақыт өтіп кетті. Болашақ уақытты жазыңыз."},
    "field_label":          {"ru": "Поле",
                             "kk": "Алаң"},

}


_FIELD_BTN_RE = re.compile(r'(?:Поле|Алаң)\s*(\d+)\s*\((\S+)\)')
_FORMAT_BTN_RE = re.compile(r'\b(\d+x\d+)\b')


class LlmBookingFlowHandler:
    """
    LLM-driven booking handler.

    Main entry point: handle().
    Called from message_handler when a booking-related message is detected or
    when an existing draft (phone + state='draft') needs continuation.
    """

    BOT_NAME = "dopsy_bot"

    asker = BaseAsker(T)
    draft_handler = BaseDraftHandler(BOT_NAME)
    formatter = BaseFormat(asker)
    helper = BaseHelper()
    buttons = BaseButton()
    checker = BaseChecker(asker, formatter, buttons, draft_handler, )

    # ══════════════════════════════════════════════════════════════════════
    #  Main Entry Point
    # ══════════════════════════════════════════════════════════════════════

    def handle(
        self,
        data: dict,
        chat_id: str,
        user_message: str,
        phone: str,
        lang: str = "ru",
    ) -> str | None:
        """
        Process one user message through the LLM booking flow.

        Returns:
            str  — message to send back to the user
            None — not a booking intent; caller should fall through to RAG/LLM
        """
        data["lang"] = lang

        for key in ("time_start", "time_end"):
            if data.get(key):
                data[key] = floor_time_to_30_minutes(
                    datetime.strptime(data[key], "%H:%M").time()
                )

        # data["field"] from the extractor is a format string ("5x5", "6x6"),
        # not a field ID.
        format_str = data.get("field")
        data["format"] = format_str
        data["field"] = None

        if format_str:
            data["field"] = self._resolve_field_id(format_str, data)

        # Handle format button reply (e.g., "5x5" or "6x6")
        fmt_btn = _FORMAT_BTN_RE.search(user_message)
        if fmt_btn:
            fmt = fmt_btn.group(1)
            if any(f["format"] == fmt for f in config.BOOKING_FIELDS):
                data["format"] = fmt
                data["field"] = self._resolve_field_id(fmt, data)

        # Handle legacy field button reply (e.g., "Поле 1 (5x5)")
        if not data.get("field") and not fmt_btn:
            field_btn = _FIELD_BTN_RE.search(user_message)
            if field_btn:
                fid = int(field_btn.group(1))
                fmt = field_btn.group(2)
                if any(f["id"] == fid and f["format"] == fmt for f in config.BOOKING_FIELDS):
                    data["field"] = fid
                    data["format"] = fmt

        # ── 1. Check for an existing draft ────────────────────────────────
        draft = booking_repo.get_existing_draft(phone)

        if draft:
            logger.info(
                "[LLM_FLOW] Existing draft id=%d for phone=%s",
                draft["id"], phone,
            )

            if self.helper.is_ready_for_confirm(draft):
                confirm = self.checker.check_confirm_response(user_message)
                if confirm == "yes":
                    logger.info("[LLM_FLOW] YES → finalize id=%d", draft["id"])
                    return self._finalize_booking(draft, chat_id, phone, lang)
                if confirm == "no":
                    logger.info("[LLM_FLOW] NO → cancel id=%d", draft["id"])
                    return self._cancel_draft(draft, chat_id, lang)

            logger.info("[LLM_FLOW] Extracted for continuation: %s", data)

            current_data = self.helper.draft_to_data(draft)
            current_data["lang"] = lang
            merged = self.helper.merge_data(current_data, data)

            if merged.get("format") and not merged.get("field"):
                merged["field"] = self._resolve_field_id(
                    merged["format"], merged,
                )

            self.draft_handler.update_draft_in_db(draft["id"], merged)
            return self._evaluate_and_respond(merged)

        # Create a new draft with whatever data was extracted
        client_token = str(uuid.uuid4())

        create_kwargs: dict = {"phone": phone, "client_token": client_token}
        for key in ("date", "time_start", "time_end", "field",
                     "players", "customer_name", "format"):
            if data.get(key) is not None:
                create_kwargs[key] = data[key]

        result = postgres.create_draft(bot_name=self.BOT_NAME, chat_id=chat_id, **create_kwargs)
        booking_id = result["data"]["booking_id"]
        logger.info("[LLM_FLOW] Created draft id=%d fields=%s", booking_id, create_kwargs)

        data = {
            "date": data.get("date"),
            "time_start": data.get("time_start"),
            "time_end": data.get("time_end"),
            "field": data.get("field"),
            "format": data.get("format"),
            "players": data.get("players"),
            "customer_name": data.get("customer_name"),
            "booking_id": booking_id,
            "client_token": client_token,
            "lang": lang,
        }

        return self._evaluate_and_respond(data)

    @staticmethod
    def _resolve_field_id(format_str: str, data: dict) -> int | None:
        """
        Resolve a format string (e.g. '5x5') to a concrete field ID.
        When multiple fields share the format, pick the first one that
        is free for the requested date/time slot.
        """
        matching = [
            f for f in config.BOOKING_FIELDS if f["format"] == format_str
        ]
        if not matching:
            return None
        if len(matching) == 1:
            return matching[0]["id"]

        date_str = data.get("date")
        ts = data.get("time_start")
        te = data.get("time_end")

        if date_str and ts and te:
            week_start, week_end = booking_logic.get_week_range()
            from datetime import timedelta
            extend = timedelta(days=1) if ts > te else timedelta(0)
            booked = booking_logic.get_all_booked(week_start, week_end + extend)
            for f in matching:
                if booking_logic.check_range_free(
                    booked, date_str, ts, te, f["id"],
                ):
                    return f["id"]

        return None


    def _finalize_booking(
        self, draft: dict, chat_id: str, phone: str, lang: str = "ru",
    ) -> str:
        """
        Transition draft → awaiting_payment via booking_service.request_payment.
        """
        booking_id = draft["id"]
        client_token = str(draft.get("client_token", ""))

        result = booking_service.request_payment(booking_id, client_token)

        if not result["ok"]:
            if result["code"] == "SLOT_TAKEN":
                logger.warning(
                    "[LLM_FLOW] Slot taken at finalization, id=%d", booking_id,
                )
                return (
                    self.asker.localize(lang, "slot_taken") + "\n\n"
                    + self._show_general_availability(lang)
                )
            logger.error("[LLM_FLOW] request_payment failed: %s", result)
            return result.get("message", "Ошибка. Попробуйте ещё раз.")

        d = str(draft["date"])
        ts = str(draft["time_start"])[:5]
        te = str(draft["time_end"])[:5]
        field_id = int(draft["field"])
        fmt = draft.get("format") or "?"
        players = draft.get("players", "?")
        name = draft.get("customer_name", "")

        logger.info("[LLM_FLOW] Booking id=%d → awaiting_payment", booking_id)
        clear_history(chat_id)
        refresh_all_bookings()
        refresh_week_sheet()

        total = calculate_full_booking_price(fmt, d, ts, te)

        return self.asker.localize(lang, "booking_done",
                  date=self.formatter.fmt_date(d, lang), ts=ts, te=te,
                  fid=field_id, fmt=fmt, players=players,
                  name=name, price=fmt_price(total),
                  pay_url=config.KASPI_PAYMENT_URL)

    def _cancel_draft(self, draft: dict, chat_id: str, lang: str = "ru") -> str:
        """Cancel the draft and return user-facing confirmation."""
        clear_history(chat_id)
        postgres.cancel_booking_trial(
            self.BOT_NAME, draft["id"],
            actor_type="whatsapp", reason="user_cancel_llm_flow",
        )
        logger.info("[LLM_FLOW] Draft id=%d cancelled", draft["id"])
        return self.asker.localize(lang, "cancelled")

    # ══════════════════════════════════════════════════════════════════════
    #  Core Evaluation
    # ══════════════════════════════════════════════════════════════════════

    def _evaluate_and_respond(self, data: dict) -> str:
        """
        Central dispatcher — examines which booking fields are filled
        and delegates to the right check method.
        """
        lang = data.get("lang", "ru")

        has_date = data.get("date") is not None
        has_ts = data.get("time_start") is not None
        has_te = data.get("time_end") is not None
        has_field = data.get("field") is not None
        has_players = data.get("players") is not None
        has_name = data.get("customer_name") is not None

        # ── Rule 6: only one of start/end provided → ask for both ──
        if has_ts != has_te:
            return self.asker.localize(lang, "provide_both_times")

        has_time = has_ts and has_te

        if has_time and data["time_start"] == data["time_end"]:
            return self.asker.localize(lang, "time_equal")

        # ── Reject past date/time ──
        if has_date and is_past_booking_time(
            data["date"], data["time_start"] if has_time else None,
        ):
            return self.asker.localize(lang, "time_in_past")

        # ── Validate format string ──
        if data.get("format"):
            format_exists = any(
                f["format"] == data["format"] for f in config.BOOKING_FIELDS
            )
            if not format_exists:
                formats = ", ".join(
                    sorted({f["format"] for f in config.BOOKING_FIELDS})
                )
                return self.asker.localize(lang, "format_not_found", fmt=data["format"], available=formats)

        # ── Validate field ID ──
        if has_field:
            field_exists = any(
                f["id"] == int(data["field"]) for f in config.BOOKING_FIELDS
            )
            if not field_exists:
                formats = sorted({f["format"] for f in config.BOOKING_FIELDS})
                fl = "\n".join(f"  • {fmt}" for fmt in formats)
                return self.asker.localize(lang, "field_not_found") + "\n" + fl

        # ── Rule 7: validate players ──
        if has_players and int(data["players"]) <= 0:
            return self.asker.localize(lang, "players_invalid")
        if has_players and int(data["players"]) > config.MAX_PLAYERS:
            data["players"] = None
            has_players = False
            return (self.asker.localize(lang, "players_overflow")
                    + "\n" + self.asker.localize(lang, "ask_players"))

        # ── All 6 fields → confirm ──
        if has_date and has_time and has_field and has_players and has_name:
            return self.checker.check_and_confirm(data)

        # ── Rule 4: date + time + field → check slot, ask remaining ──
        if has_date and has_time and has_field:
            return self.checker.check_full_slot(data)

        # ── Rule 5: date + time → show free fields ──
        if has_date and has_time:
            return self.checker.check_date_and_time(data)

        # ── date + field (no time) → show time ranges ──
        if has_date and has_field:
            return self.checker.check_date_and_field(data)

        # ── Rule 1: date only → free fields & times for that date ──
        if has_date and not has_time and not has_field:
            return self.checker.check_date_only(data)

        # ── Rule 2: time range only → available dates & fields ──
        if has_time and not has_date:
            return self.checker.check_time_range_only(data)

        # ── Rule 3: field only → dates & times for that field ──
        if has_field and not has_date and not has_time:
            return self.checker.check_field_only(data)

        # ── Nothing specific → ask for date (highest priority) ──
        return self.asker.ask_date_priority(lang)

    def _show_general_availability(self, lang: str = "ru") -> str:
        """Full 7-day availability overview."""
        free = booking_logic.get_free_windows()
        if not free:
            return self.asker.localize(lang, "no_slots_7d")

        ctx = booking_logic.format_availability_context(free)
        return (
            self.asker.localize(lang, "general_header")
            + "\n\n" + ctx + "\n\n"
            + self.asker.localize(lang, "general_prompt")
        )
