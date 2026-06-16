"""
Non-deterministic LLM-driven booking flow.

Unlike the step-by-step BookingStepHandler (booking_session.py), this flow:
- Accepts booking data in any order (no fixed step sequence)
- Uses LLM to extract intent + booking params from natural language
- Creates or continues a draft booking based on whatever data is available
- Checks availability depending on which combination of fields is present
- Returns bilingual (RU/KK) responses with availability info + what's still needed

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
import uuid

import config
from chat.conversation import clear_history
from integrations import booking as booking_logic
from integrations import booking_service
from integrations.repo import booking_repo, postgres
from integrations.repo.utils import _conn
from integrations.sheets.booking_sheets import refresh_all_bookings
from utils import today_almaty

logger = logging.getLogger(__name__)

# ── Weekday labels for formatted dates ──
_WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# ── Confirmation / cancellation vocabulary (mirrors base_session.py) ──
_YES_WORDS = {
    "да", "иә", "ok", "ок", "подтверждаю", "yes",
    "жарайды", "дұрыс", "растаймын", "👍",
}
_NO_WORDS = {
    "нет", "жоқ", "no", "отмена", "изменить",
    "өзгерт", "болмайды", "бастапқы", "бас тартамын",
}
_CANCEL_PHRASES = (
    "отмен", "стоп", "передум", "не хочу", "не нужно", "не надо",
    "тоқтат", "керек емес", "бас тарт",
)


class LlmBookingFlowHandler:
    """
    LLM-driven booking handler.

    Main entry point: handle_message().
    Called from message_handler when a booking-related message is detected or
    when an existing draft (phone + state='draft') needs continuation.
    """

    BOT_NAME = "dopsy_bot"

    # ══════════════════════════════════════════════════════════════════════
    #  Main Entry Point
    # ══════════════════════════════════════════════════════════════════════

    def handle(
        self,
        data: dict,
        chat_id: str,
        user_message: str,
        phone: str,

    ) -> str | None:
        """
        Process one user message through the LLM booking flow.

        Flow:
          1. Look for an existing draft (phone + state='draft').
          2. If draft exists and is fully filled → check for yes/no confirmation.
          3. If draft exists but incomplete → extract new data, merge, re-evaluate.
          4. If no draft → classify intent via LLM; create draft if booking.
          5. Return a bilingual availability / prompt response.

        Returns:
            str  — message to send back to the user
            None — not a booking intent; caller should fall through to RAG/LLM
        """
        # data["field"] from the extractor is a format string ("5x5", "6x6"),
        # not a field ID.  Separate it: "format" keeps the string,
        # "field" will hold the resolved integer field ID.
        format_str = data.get("field")
        data["format"] = format_str
        data["field"] = None

        if format_str:
            data["field"] = self._resolve_field_id(format_str, data)

        # ── 1. Check for an existing draft ────────────────────────────────
        draft = booking_repo.get_existing_draft(phone)

        if draft:
            logger.info(
                "[LLM_FLOW] Existing draft id=%d for phone=%s",
                draft["id"], phone,
            )

            # 2. If all 6 fields present, the user should be confirming
            if self._is_ready_for_confirm(draft):
                confirm = self._check_confirm_response(user_message)
                if confirm == "yes":
                    logger.info("[LLM_FLOW] YES → finalize id=%d", draft["id"])
                    return self._finalize_booking(draft, chat_id, phone)
                if confirm == "no":
                    logger.info("[LLM_FLOW] NO → cancel id=%d", draft["id"])
                    return self._cancel_draft(draft, chat_id)
                # Not yes/no — user is changing data; fall through to extraction

            # 3. Extract new data, merge into draft, evaluate
            logger.info("[LLM_FLOW] Extracted for continuation: %s", data)

            current_data = self._draft_to_data(draft)
            merged = self._merge_data(current_data, data)

            # Re-resolve field ID when format is known but field isn't yet
            if merged.get("format") and not merged.get("field"):
                merged["field"] = self._resolve_field_id(
                    merged["format"], merged,
                )

            self._update_draft_in_db(draft["id"], merged)
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
        }

        return self._evaluate_and_respond(data)

    def _resolve_field_id(
        self, format_str: str, data: dict,
    ) -> int | None:
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
            booked = booking_logic.get_all_booked(week_start, week_end)
            for f in matching:

                if booking_logic.is_range_free(
                    booked, date_str, ts, te, f["id"],
                ):
                    return f["id"]

        return None

    # ══════════════════════════════════════════════════════════════════════
    #  Draft Management
    # ══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _draft_to_data(draft: dict) -> dict:
        """Convert a DB draft row into the standardized data dict used everywhere."""
        return {
            "date": str(draft["date"]) if draft.get("date") else None,
            "time_start": (
                str(draft["time_start"])[:5] if draft.get("time_start") else None
            ),
            "time_end": (
                str(draft["time_end"])[:5] if draft.get("time_end") else None
            ),
            "field": int(draft["field"]) if draft.get("field") else None,
            "format": draft.get("format"),
            "players": int(draft["players"]) if draft.get("players") else None,
            "customer_name": draft.get("customer_name"),
            "booking_id": draft["id"],
            "client_token": str(draft.get("client_token", "")),
        }

    @staticmethod
    def _merge_data(current: dict, extracted: dict) -> dict:
        """
        Merge newly extracted values into the current draft data.
        Only non-null extracted values overwrite existing ones,
        so previously collected data is preserved.
        """
        merged = dict(current)

        field_map = {
            "date": "date",
            "time_start": "time_start",
            "time_end": "time_end",
            "field": "field",
            "format": "format",
            "players": "players",
            "customer_name": "customer_name",
            "name": "customer_name",
        }
        for ext_key, data_key in field_map.items():
            val = extracted.get(ext_key)
            if val is not None:
                merged[data_key] = val

        return merged

    def _update_draft_in_db(self, booking_id: int | None, data: dict) -> None:
        """Persist the merged data back to the draft row in PostgreSQL."""
        if booking_id is None:
            return

        update_fields: dict = {}
        for key in ("date", "time_start", "time_end", "field",
                     "players", "customer_name", "format"):
            if data.get(key) is not None:
                update_fields[key] = data[key]

        if update_fields:
            postgres.update_draft(self.BOT_NAME, booking_id, **update_fields)

    # ══════════════════════════════════════════════════════════════════════
    #  Confirmation & Cancellation
    # ══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _is_ready_for_confirm(draft: dict) -> bool:
        """True when all 6 booking fields are filled in the draft."""
        return all([
            draft.get("date"),
            draft.get("time_start"),
            draft.get("time_end"),
            draft.get("field"),
            draft.get("players"),
            draft.get("customer_name"),
        ])

    @staticmethod
    def _check_confirm_response(text: str) -> str | None:
        """Return 'yes', 'no', or None from the user's text."""
        lower = text.lower().strip()
        if any(w in lower for w in _YES_WORDS):
            return "yes"
        if any(w in lower for w in _NO_WORDS):
            return "no"
        return None

    @staticmethod
    def _is_cancel_intent(text: str) -> bool:
        lower = text.lower()
        return any(p in lower for p in _CANCEL_PHRASES)

    def _finalize_booking(
        self, draft: dict, chat_id: str, phone: str,
    ) -> str:
        """
        Transition draft → awaiting_payment via booking_service.request_payment.
        The DB EXCLUDE constraint catches slot clashes atomically.
        Returns payment instructions or an error message.
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
                    "Этот слот заняли / Бұл слот алынды. "
                    "Басқа уақыт таңдаңыз.\n\n"
                    + self._show_general_availability()
                )
            logger.error("[LLM_FLOW] request_payment failed: %s", result)
            return (
                "Ошибка / Қате. Попробуйте ещё раз / Қайталап көріңіз."
            )

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

        return (
            f"📋 Бронь оформлена / Брондау тіркелді!\n\n"
            f"📅 {self._fmt_date(d)}\n⏰ {ts}–{te}\n"
            f"⚽ Поле/Алаң {field_id} ({fmt})\n"
            f"👥 Игроков/Ойыншылар: {players}\n👤 Имя/Аты: {name}\n\n"
            f"Оплатите / Төлем жасаңыз:\n{config.KASPI_PAYMENT_URL}\n"
            f"⚠️ Возврат при неявке не производится / "
            f"Келмесеңіз төлем қайтарылмайды\n\n"
            f"PDF-чек жіберіңіз / Отправьте PDF-чек сюда 🙏\n"
            f"⚠️ 15 мин без оплаты — бронь отменится / жойылады."
        )

    def _cancel_draft(self, draft: dict, chat_id: str) -> str:
        """Cancel the draft and return user-facing confirmation."""
        clear_history(chat_id)
        postgres.cancel_booking_trial(
            self.BOT_NAME, draft["id"],
            actor_type="whatsapp", reason="user_cancel_llm_flow",
        )
        logger.info("[LLM_FLOW] Draft id=%d cancelled", draft["id"])
        return (
            "Бронь отменена / Брондау тоқтатылды. "
            "Напишите, если что / Қаласаңыз жазыңыз! 🙂"
        )

    # ══════════════════════════════════════════════════════════════════════
    #  Core Evaluation
    #  Looks at which fields are present and dispatches to the matching
    #  availability-check method.  No step ordering assumed.
    # ══════════════════════════════════════════════════════════════════════

    def _evaluate_and_respond(self, data: dict) -> str:
        """
        Central dispatcher — examines which booking fields are filled
        and delegates to the right check method.
        """
        has_date = data.get("date") is not None
        has_ts = data.get("time_start") is not None
        has_te = data.get("time_end") is not None
        has_field = data.get("field") is not None
        has_players = data.get("players") is not None
        has_name = data.get("customer_name") is not None

        # ── Rule 6: only one of start/end provided → ask for both ──
        if has_ts != has_te:
            return (
                "Укажите время начала и окончания / "
                "Басталу-аяқталу уақытын жазыңыз.\n"
                "Напр./Мыс.: *18:00 - 20:00*"
            )

        has_time = has_ts and has_te

        # ── Validate time order ──
        if has_time and data["time_start"] >= data["time_end"]:
            return (
                "Время окончания должно быть позже начала / "
                "Аяқталу уақыты басталудан кейін болуы керек.\n"
                "Напр./Мыс.: *18:00 - 20:00*"
            )

        # ── Validate format string ──
        if data.get("format"):
            format_exists = any(
                f["format"] == data["format"] for f in config.BOOKING_FIELDS
            )
            if not format_exists:
                formats = ", ".join(
                    sorted({f["format"] for f in config.BOOKING_FIELDS})
                )
                return (
                    f"Формат \"{data['format']}\" не найден / табылмады. "
                    f"Доступные / Қолжетімді: {formats}"
                )

        # ── Validate field ID ──
        if has_field:
            field_exists = any(
                f["id"] == int(data["field"]) for f in config.BOOKING_FIELDS
            )
            if not field_exists:
                fl = "\n".join(
                    f"  {f['id']}. Поле/Алаң {f['id']} ({f['format']})"
                    for f in config.BOOKING_FIELDS
                )
                return (
                    f"Поле не найдено / Алаң табылмады. "
                    f"Доступные / Қолжетімді:\n{fl}"
                )

        # ── Rule 7: validate players ──
        if has_players and int(data["players"]) <= 0:
            return (
                "Игроков должно быть > 0 / "
                "Ойыншылар саны 0-ден көп болуы керек."
            )

        # ── All 6 fields → confirm ──
        if has_date and has_time and has_field and has_players and has_name:
            return self._check_and_confirm(data)

        # ── Rule 4: date + time + field → check slot, ask remaining ──
        if has_date and has_time and has_field:
            return self._check_full_slot(data)

        # ── Rule 5: date + time → show free fields ──
        if has_date and has_time:
            return self._check_date_and_time(data)

        # ── date + field (no time) → show time ranges ──
        if has_date and has_field:
            return self._check_date_and_field(data)

        # ── Rule 1: date only → free fields & times for that date ──
        if has_date and not has_time and not has_field:
            return self._check_date_only(data)

        # ── Rule 2: time range only → available dates & fields ──
        if has_time and not has_date:
            return self._check_time_range_only(data)

        # ── Rule 3: field only → dates & times for that field ──
        if has_field and not has_date and not has_time:
            return self._check_field_only(data)

        # ── Nothing specific → general availability overview ──
        return self._show_general_availability()

    # ══════════════════════════════════════════════════════════════════════
    #  Availability Checks  (Rules 1–5 + combinations)
    # ══════════════════════════════════════════════════════════════════════

    def _check_full_slot(self, data: dict) -> str:
        """
        Rule 4: date + time + field are all known.
        Check if this exact slot is free; ask for remaining fields if so.
        """
        date_str = data["date"]
        ts, te = data["time_start"], data["time_end"]
        field_id = int(data["field"])

        week_start, week_end = booking_logic.get_week_range()
        booked = booking_logic.get_all_booked(week_start, week_end)

        field_conf = next(
            (f for f in config.BOOKING_FIELDS if f["id"] == field_id), {},
        )
        fmt = data.get("format") or field_conf.get("format", "?")

        if booking_logic.is_range_free(booked, date_str, ts, te, field_id):
            # Slot is free — ask for what's still missing
            missing = self._format_missing_fields(data)

            if not missing:
                return self._check_and_confirm(data)

            return (
                f"✅ Поле/Алаң {field_id} ({fmt}) свободно/бос\n"
                f"{self._fmt_date(date_str)} {ts}–{te}!\n\n"
                f"Осталось / Қалғаны:\n{missing}"
            )

        # Slot taken — show what IS available on that date
        free = booking_logic.get_free_windows()
        day_windows = [w for w in free if str(w["date"]) == date_str]
        alt_text = self._format_windows_by_field(day_windows)

        return (
            f"❌ Поле/Алаң {field_id} ({fmt}) занято/бос емес "
            f"{self._fmt_date(date_str)} {ts}–{te}.\n\n"
            f"Доступные варианты / Бос нұсқалар:\n{alt_text}"
        )

    def _check_date_only(self, data: dict) -> str:
        """Rule 1: show all free fields and their time ranges for the given date."""
        date_str = data["date"]
        free = booking_logic.get_free_windows()
        day_windows = [w for w in free if str(w["date"]) == date_str]

        if not day_windows:
            return (
                f"На {self._fmt_date(date_str)} бос слот жоқ / "
                f"нет свободных слотов.\n"
                f"Басқа күнді таңдаңыз / Выберите другую дату.\n\n"
                + self._format_available_dates(free)
            )

        windows_text = self._format_windows_by_field(day_windows)
        return (
            f"📅 {self._fmt_date(date_str)} — бос слоттар / свободные слоты:\n\n"
            f"{windows_text}\n\n"
            f"Укажите время и/или поле / "
            f"Уақыт пен алаңды жазыңыз (напр./мыс. *18:00 - 20:00*)."
        )

    def _check_time_range_only(self, data: dict) -> str:
        """Rule 2: show available dates and fields for the given time interval."""
        ts, te = data["time_start"], data["time_end"]

        week_start, week_end = booking_logic.get_week_range()
        booked = booking_logic.get_all_booked(week_start, week_end)
        free = booking_logic.get_free_windows()
        dates = sorted({str(w["date"]) for w in free})

        # For each date, find fields where the requested range is free
        available: list[dict] = []
        for d in dates:
            free_fields = [
                f for f in config.BOOKING_FIELDS
                if booking_logic.is_range_free(booked, d, ts, te, f["id"])
            ]
            if free_fields:
                available.append({"date": d, "fields": free_fields})

        if not available:
            return (
                f"На {ts}–{te} бос алаң жоқ / нет свободных полей.\n"
                f"Басқа уақыт көріңіз / Попробуйте другое время."
            )

        lines = [f"⏰ {ts}–{te} — доступные / қолжетімді:\n"]

        for item in available:
            d_label = self._fmt_date(item["date"])
            fields = ", ".join(
                f"Поле/Алаң {f['id']} ({f['format']})"
                for f in item["fields"]
            )
            lines.append(f"  📅 {d_label}: {fields}")

        lines.append("\nУкажите дату и поле / Күн мен алаңды жазыңыз.")

        return "\n".join(lines)

    def _check_date_and_field(self, data: dict) -> str:
        """Date + field known, time unknown. Show free time ranges."""
        date_str = data["date"]
        field_id = int(data["field"])
        free = booking_logic.get_free_windows()

        windows = [
            w for w in free
            if str(w["date"]) == date_str and w["field"] == field_id
        ]

        field_conf = next(
            (f for f in config.BOOKING_FIELDS if f["id"] == field_id), {},
        )
        fmt = field_conf.get("format", "?")

        if not windows:
            # Field fully booked on this date — show other fields
            day_windows = [w for w in free if str(w["date"]) == date_str]
            alt_text = self._format_windows_by_field(day_windows)
            return (
                f"Поле/Алаң {field_id} ({fmt}) занято/бос емес "
                f"{self._fmt_date(date_str)}.\n\n"
                f"Басқа нұсқалар / Другие варианты:\n{alt_text}"
            )

        times_str = ", ".join(
            f"{self._fmt_time(w['time_start'])}–{self._fmt_time(w['time_end'])}"
            for w in sorted(windows, key=lambda w: w["time_start"])
        )
        return (
            f"⚽ Поле/Алаң {field_id} ({fmt}), "
            f"{self._fmt_date(date_str)}\n"
            f"Свободное время / Бос уақыт: {times_str}\n\n"
            f"Укажите время / Уақытты жазыңыз "
            f"(напр./мыс. *18:00 - 20:00*)."
        )

    def _check_field_only(self, data: dict) -> str:
        """Rule 3: show available dates and time ranges for the given field."""
        field_id = int(data["field"])
        free = booking_logic.get_free_windows()
        field_windows = [w for w in free if w["field"] == field_id]

        field_conf = next(
            (f for f in config.BOOKING_FIELDS if f["id"] == field_id), {},
        )
        fmt = field_conf.get("format", "?")

        if not field_windows:
            return (
                f"Поле/Алаң {field_id} ({fmt}) занято/бос емес "
                f"жақын 7 күнде / в ближайшие 7 дней."
            )

        windows_text = self._format_windows_by_date(field_windows)
        return (
            f"⚽ Поле/Алаң {field_id} ({fmt}) — бос слоттар / свободные слоты:\n\n"
            f"{windows_text}\n\n"
            f"Укажите дату и время / Күн мен уақытты жазыңыз."
        )

    def _check_date_and_time(self, data: dict) -> str:
        """Rule 5: date + time are known but field is not. Show available fields."""
        date_str = data["date"]
        ts, te = data["time_start"], data["time_end"]

        week_start, week_end = booking_logic.get_week_range()
        booked = booking_logic.get_all_booked(week_start, week_end)

        candidates = config.BOOKING_FIELDS
        if data.get("format"):
            candidates = [
                f for f in candidates if f["format"] == data["format"]
            ]

        free_fields = [
            f for f in candidates
            if booking_logic.is_range_free(booked, date_str, ts, te, f["id"])
        ]

        if not free_fields:
            # No fields free for this slot — show what else is available that day
            free = booking_logic.get_free_windows()
            day_windows = [w for w in free if str(w["date"]) == date_str]
            alt_text = self._format_windows_by_field(day_windows)
            return (
                f"Бос алаң жоқ / Нет свободных полей "
                f"{self._fmt_date(date_str)} {ts}–{te}.\n\n"
                f"Бос уақыт / Доступное время:\n{alt_text}"
            )

        # Single field available — auto-select it and continue
        if len(free_fields) == 1:
            f = free_fields[0]
            data["field"] = f["id"]
            data["format"] = f["format"]
            self._update_draft_in_db(data.get("booking_id"), data)

            # If players+name are also present now, jump to confirmation
            if data.get("players") and data.get("customer_name"):
                return self._check_and_confirm(data)

            missing = self._format_missing_fields(data)
            return (
                f"✅ Поле/Алаң {f['id']} ({f['format']}) свободно/бос "
                f"{self._fmt_date(date_str)} {ts}–{te}!\n\n"
                + (f"Осталось / Қалғаны:\n{missing}" if missing else "")
            )

        # Multiple fields — let user pick
        fl = "\n".join(
            f"  {f['id']}. Поле/Алаң {f['id']} ({f['format']})"
            for f in free_fields
        )
        return (
            f"📅 {self._fmt_date(date_str)}, {ts}–{te}\n\n"
            f"Свободные поля / Бос алаңдар:\n{fl}\n\n"
            f"Укажите номер поля / Алаң нөмірін жазыңыз."
        )

    def _check_and_confirm(self, data: dict) -> str:
        """
        All 6 fields present. Verify the slot is still free,
        then show the confirmation prompt.
        """
        date_str = data["date"]
        ts, te = data["time_start"], data["time_end"]
        field_id = int(data["field"])

        week_start, week_end = booking_logic.get_week_range()
        booked = booking_logic.get_all_booked(week_start, week_end)

        field_conf = next(
            (f for f in config.BOOKING_FIELDS if f["id"] == field_id), {},
        )
        fmt = data.get("format") or field_conf.get("format", "?")

        if not booking_logic.is_range_free(booked, date_str, ts, te, field_id):
            # Slot taken between data collection and confirmation
            free = booking_logic.get_free_windows()
            day_windows = [w for w in free if str(w["date"]) == date_str]
            alt_text = self._format_windows_by_field(day_windows)
            return (
                f"❌ Поле/Алаң {field_id} ({fmt}) "
                f"{self._fmt_date(date_str)} {ts}–{te} занято/бос емес.\n\n"
                f"Доступные варианты / Бос нұсқалар:\n{alt_text}"
            )

        return (
            f"📋 Бронь / Брондау:\n"
            f"📅 {self._fmt_date(date_str)}\n"
            f"⏰ {ts}–{te}\n"
            f"⚽ Поле/Алаң {field_id} ({fmt})\n"
            f"👥 Игроков/Ойыншылар: {data['players']}\n"
            f"👤 Имя/Аты: {data['customer_name']}\n\n"
            f"Подтвердить / Растайсыз ба? *да/иә* или/немесе *нет/жоқ*"
        )

    # ══════════════════════════════════════════════════════════════════════
    #  Formatting Helpers
    # ══════════════════════════════════════════════════════════════════════

    def _show_general_availability(self) -> str:
        """Full 7-day availability overview when no specific fields are given."""
        free = booking_logic.get_free_windows()
        if not free:
            return (
                "Бос слот жоқ / Нет свободных слотов на 7 дней."
            )

        ctx = booking_logic.format_availability_context(free)
        return (
            f"Брондайық / Давайте забронируем! Бос слоттар:\n\n{ctx}\n\n"
            f"Укажите дату, время или поле / "
            f"Күнді, уақытты немесе алаңды жазыңыз."
        )

    @staticmethod
    def _format_windows_by_field(windows: list[dict]) -> str:
        """Group free windows by field, format as multiline text."""
        if not windows:
            return "  (нет свободных слотов / бос слот жоқ)"

        by_field: dict[tuple, list] = {}
        for w in windows:
            key = (w["field"], w.get("format", "?"))
            by_field.setdefault(key, []).append(w)

        lines = []
        for (fid, fmt) in sorted(by_field):
            sorted_w = sorted(by_field[(fid, fmt)], key=lambda x: x["time_start"])
            times = ", ".join(
                f"{LlmBookingFlowHandler._fmt_time(w['time_start'])}"
                f"–{LlmBookingFlowHandler._fmt_time(w['time_end'])}"
                for w in sorted_w
            )
            lines.append(f"  ⚽ Поле/Алаң {fid} ({fmt}): {times}")

        return "\n".join(lines)

    @staticmethod
    def _format_windows_by_date(windows: list[dict]) -> str:
        """Group free windows by date, format as multiline text."""
        if not windows:
            return "  (нет свободных слотов / бос слот жоқ)"

        by_date: dict[str, list] = {}
        for w in windows:
            by_date.setdefault(str(w["date"]), []).append(w)

        lines = []
        for d in sorted(by_date):
            sorted_w = sorted(by_date[d], key=lambda x: x["time_start"])
            d_label = LlmBookingFlowHandler._fmt_date(d)
            times = ", ".join(
                f"{LlmBookingFlowHandler._fmt_time(w['time_start'])}"
                f"–{LlmBookingFlowHandler._fmt_time(w['time_end'])}"
                for w in sorted_w
            )
            lines.append(f"  📅 {d_label}: {times}")

        return "\n".join(lines)

    @staticmethod
    def _format_available_dates(free_windows: list[dict]) -> str:
        """List all dates that have at least one free window."""
        dates = sorted({str(w["date"]) for w in free_windows})
        if not dates:
            return ""

        lines = ["Доступные даты / Қолжетімді күндер:"]
        for d_str in dates:
            lines.append(f"  📅 {LlmBookingFlowHandler._fmt_date(d_str)}")
        return "\n".join(lines)

    @staticmethod
    def _format_missing_fields(data: dict) -> str:
        """Return bilingual list of fields that are still None."""
        missing: list[str] = []

        if data.get("date") is None:
            missing.append("  • Дата / Күн")
        if data.get("time_start") is None or data.get("time_end") is None:
            missing.append("  • Время / Уақыт (начало-конец)")
        if data.get("field") is None:
            missing.append("  • Поле / Алаң")
        if data.get("players") is None:
            missing.append("  • Игроков / Ойыншылар саны")
        if data.get("customer_name") is None:
            missing.append("  • Имя / Атыңыз")

        return "\n".join(missing)

    @staticmethod
    def _fmt_date(date_str: str) -> str:
        """'2026-06-15' → 'Пн 15.06.2026'"""
        try:
            from datetime import datetime
            dt = datetime.strptime(date_str, "%Y-%m-%d").date()
            return f"{_WEEKDAY_RU[dt.weekday()]} {dt.strftime('%d.%m.%Y')}"
        except (ValueError, TypeError):
            return date_str or "?"

    @staticmethod
    def _fmt_time(value) -> str:
        """Accept time objects or 'HH:MM:SS' strings → 'HH:MM'"""
        if hasattr(value, "strftime"):
            return value.strftime("%H:%M")
        return str(value)[:5]
