"""
Non-deterministic LLM-driven trial-signup flow (academy bots).

The arena counterpart is llm_booking_flow.py. Same architecture:
- Accepts signup data in any order (no fixed step sequence)
- Uses the LLM to extract intent + params from natural language
- Creates or continues a draft trial based on whatever data is available
- Identifies a continuation by phone + state='draft' + group_type, so no
  trial_sessions row is needed and out-of-order data cannot break step ordering

What is NOT ported from the arena flow:
- the earlier-start suggestion (BaseChecker._offer_earlier_start and friends) —
  it only makes sense for a free-form slot that can slide leftward
- pricing, payment receipts and the reservation TTL — a trial is free
- field/format resolution — a class is one dimension, not two

Where it necessarily differs: an academy has no free-form slot grid. The parent
picks one of a handful of existing weekly classes, so the arena's seven checking
rules collapse into four, and the class supplies time_end and group_id rather
than the parent supplying them (see _evaluate_and_respond).

Registration data is identical to the deterministic flow (trial_session.py):
date, start/end time, group, child name, child age.
"""

import logging
import uuid

from chat.conversation import clear_history
from handlers.base_classes.base_asker import BaseAsker
from handlers.base_classes.base_button import BaseButton
from handlers.base_classes.base_checker import BaseChecker
from handlers.base_classes.base_format import BaseFormat
from handlers.base_classes.base_helper import BaseHelper
from integrations import trial as trial_logic
from integrations.repo import academy_repo, postgres
from integrations.sheets.trial_sheets import refresh_all_trials
from utils import is_past_booking_time

logger = logging.getLogger(__name__)


# Sanity bounds only — each academy states its own ages in its system prompt
# (sp_2: 5–15, sp_3: 7–16 plus adults), and the admin, not the bot, decides.
MIN_AGE = 2
MAX_AGE = 99


T = {
    "ask_date":          {"ru": "📅 На какую дату хотите записаться?",
                          "kk": "📅 Қай күнге жазылғыңыз келеді?"},
    "ask_time":          {"ru": "⏰ Укажите время занятия (напр. *18:00*)",
                          "kk": "⏰ Сабақ уақытын жазыңыз (мыс. *18:00*)"},
    "ask_name":          {"ru": "👤 Укажите имя вашего ребёнка:",
                          "kk": "👤 Балаңыздың есімін жазыңыз:"},
    "ask_age":           {"ru": "🎂 Сколько лет вашему ребёнку?",
                          "kk": "🎂 Балаңыз нешеде?"},
    "general_header":    {"ru": "Давайте запишемся! Ближайшие занятия:",
                          "kk": "Жазылайық! Жақын сабақтар:"},
    "general_prompt":    {"ru": "Укажите дату или время занятия.",
                          "kk": "Сабақтың күнін немесе уақытын жазыңыз."},
    "no_classes_7d":     {"ru": "К сожалению, свободных занятий на ближайшие 7 дней нет.\n"
                                "Пожалуйста, свяжитесь с администратором.",
                          "kk": "Өкінішке орай, жақын 7 күнде бос сабақ жоқ.\n"
                                "Әкімшімен хабарласыңыз."},
    "no_classes_date":   {"ru": "На {date} занятий нет.",
                          "kk": "{date} күні сабақ жоқ."},
    "no_classes_time":   {"ru": "Занятий в {ts} нет.",
                          "kk": "{ts} уақытында сабақ жоқ."},
    "day_classes":       {"ru": "📅 {date} — занятия:",
                          "kk": "📅 {date} — сабақтар:"},
    "time_classes":      {"ru": "⏰ {ts} — доступные дни:",
                          "kk": "⏰ {ts} — қолжетімді күндер:"},
    "available_dates":   {"ru": "Доступные даты:",
                          "kk": "Қолжетімді күндер:"},
    "no_slots_empty":    {"ru": "  (нет занятий)",
                          "kk": "  (сабақ жоқ)"},
    "choose_group":      {"ru": "📅 {date}, {ts}–{te}\n\n👥 Выберите группу:",
                          "kk": "📅 {date}, {ts}–{te}\n\n👥 Топты таңдаңыз:"},
    "class_free":        {"ru": "✅ {group} — {date} {ts}–{te}",
                          "kk": "✅ {group} — {date} {ts}–{te}"},
    "class_full":        {"ru": "❌ Группа «{group}» уже заполнена.\nВыберите другое занятие:",
                          "kk": "❌ «{group}» тобы толып қалды.\nБасқа сабақты таңдаңыз:"},
    "time_in_past":      {"ru": "⏰ Это время уже прошло. Укажите будущее занятие.",
                          "kk": "⏰ Бұл уақыт өтіп кетті. Болашақ сабақты таңдаңыз."},
    "age_invalid":       {"ru": "Укажите, пожалуйста, возраст ребёнка числом.",
                          "kk": "Балаңыздың жасын санмен жазыңыз."},
    "limit_reached":     {"ru": "Вы достигли максимума пробных занятий.\n"
                                "Свяжитесь с администратором.",
                          "kk": "Сіз сынақ сабағының шегіне жеттіңіз.\n"
                                "Әкімшімен хабарласыңыз."},
    "already_registered": {"ru": "Вы уже записаны на пробное занятие.",
                           "kk": "Сіз сынақ сабағына тіркелдіңіз."},
    "confirm_header":    {"ru": "📋 Детали записи:",
                          "kk": "📋 Жазылу деректері:"},
    "confirm_question":  {"ru": "Подтвердить?",
                          "kk": "Растайсыз ба?"},
    "confirm_btn":       {"ru": "Подтверждаю✅",
                          "kk": "Растаймын✅"},
    "cancel_btn":        {"ru": "Отмена❌",
                          "kk": "Бас тартамын❌"},
    "confirmed":         {"ru": ("✅ Вы записаны на пробное занятие, будем вас ждать!\n\n"
                                 "📅 {date}\n"
                                 "⏰ {ts}–{te}\n"
                                 "👥 {group}\n"
                                 "👤 Имя ребёнка: {child_name}\n"
                                 "🎂 Возраст: {child_age}\n"),
                          "kk": ("✅ Жазылым сәтті аяқталды, сізді қуана күтеміз!\n\n"
                                 "📅 {date}\n"
                                 "⏰ {ts}–{te}\n"
                                 "👥 {group}\n"
                                 "👤 Балаңыздың есімі: {child_name}\n"
                                 "🎂 Жасы: {child_age}\n")},
    "cancelled":         {"ru": "Запись отменена. Если захотите снова — просто напишите! 🙂",
                          "kk": "Жазылым тоқтатылды. Қайта қаласаңыз — жазыңыз! 🙂"},
    "error":             {"ru": "Ошибка. Попробуйте ещё раз.",
                          "kk": "Қате. Қайталап көріңіз."},
}


class LlmTrialFlowHandler:
    """
    LLM-driven trial-signup handler.

    Main entry point: handle().

    Unlike LlmBookingFlowHandler, the collaborators are built per instance:
    the arena binds them at class level because its bot is a constant, whereas
    this handler serves chatbot_2 and dopsy_boxing and every repo call is
    scoped by bot_name.
    """

    def __init__(self, bot_name: str):
        self.bot_name = bot_name
        self.asker = BaseAsker(T)
        self.formatter = BaseFormat(self.asker)
        self.buttons = BaseButton()
        self.helper = BaseHelper()
        # Only check_confirm_response is reusable from BaseChecker — every other
        # method there reads config.BOOKING_FIELDS or integrations.booking.
        self.confirm_reader = BaseChecker(
            self.asker, self.formatter, self.buttons, None,
        )

    # ══════════════════════════════════════════════════════════════════════
    #  Main Entry Point
    # ══════════════════════════════════════════════════════════════════════

    def handle(self, data: dict, chat_id: str, user_message: str,
               phone: str, lang: str = "ru") -> str:
        """
        Process one user message through the LLM trial flow.

        Returns the message to send back to the user.
        """
        data = dict(data)
        data["lang"] = lang

        # The extractor speaks "name"/"age"; normalise to the canonical keys.
        if data.get("name") and not data.get("child_name"):
            data["child_name"] = data.pop("name")

        draft = academy_repo.get_existing_trial_draft(self.bot_name, phone)

        if draft:
            logger.info("[TRIAL_FLOW] Existing draft id=%d for phone=%s",
                        draft["id"], phone)

            current = self._draft_to_data(draft)
            current["lang"] = lang

            if self._is_ready_for_confirm(draft):
                confirm = self.confirm_reader.check_confirm_response(user_message)
                if confirm == "yes":
                    logger.info("[TRIAL_FLOW] YES → finalize id=%d", draft["id"])
                    return self._finalize_trial(current, chat_id, lang)
                if confirm == "no":
                    logger.info("[TRIAL_FLOW] NO → cancel id=%d", draft["id"])
                    return self._cancel_draft(draft, chat_id, lang)

            logger.info("[TRIAL_FLOW] Extracted for continuation: %s", data)
            merged = self._merge_data(current, data)

            # A group-selection button reply names the group verbatim.
            self._apply_group_button(merged, user_message)

            self._update_draft(merged)
            return self._evaluate_and_respond(merged)

        # ── No draft yet — the gates that decide whether one may exist ────
        # start_trial_flow logs these two and proceeds regardless; here they
        # actually stop the signup, which is the point of having them.
        if not academy_repo.check_trial_limits(self.bot_name, phone):
            logger.info("[TRIAL_FLOW] Trial limit reached for phone=%s", phone)
            return self.asker.localize(lang, "limit_reached")

        if academy_repo.has_active_trial(self.bot_name, phone):
            logger.info("[TRIAL_FLOW] Already has a confirmed trial, phone=%s", phone)
            return self.asker.localize(lang, "already_registered")

        client_token = str(uuid.uuid4())
        result = postgres.create_draft(
            bot_name=self.bot_name, chat_id=chat_id, phone=phone,
            client_token=client_token, language=lang,
            group_type=academy_repo.group_type_of(self.bot_name),
        )
        trial_id = result["data"]["trial_id"]
        logger.info("[TRIAL_FLOW] Created draft id=%d", trial_id)

        data["trial_id"] = trial_id
        data["client_token"] = client_token
        data.setdefault("time_end", None)
        data.setdefault("group_id", None)

        self._update_draft(data)
        return self._evaluate_and_respond(data)

    # ══════════════════════════════════════════════════════════════════════
    #  Core Evaluation
    # ══════════════════════════════════════════════════════════════════════

    def _evaluate_and_respond(self, data: dict) -> str:
        """
        Central dispatcher — examines which fields are filled and delegates.

        Four rules, against the arena's seven. There is no field dimension and
        no user-supplied end time, so the combinations collapse:
          1. date only        → that day's classes
          2. time only        → the days having a class at that time
          3. date + time      → resolve the class (0, 1 or many candidates)
          4. everything known → confirm
        """
        lang = data.get("lang", "ru")

        has_date = data.get("date") is not None
        has_time = data.get("time_start") is not None
        has_group = data.get("group_id") is not None

        # ── Reject a class that has already started ──
        if has_date and is_past_booking_time(
            data["date"], data["time_start"] if has_time else None,
        ):
            return self.asker.localize(lang, "time_in_past")

        # ── Validate age ──
        age = data.get("child_age")
        if age is not None and not self._valid_age(age):
            data["child_age"] = None
            return (self.asker.localize(lang, "age_invalid") + "\n\n"
                    + self.asker.localize(lang, "ask_age"))

        # ── Rule 3/4: date + time → the class is (or can be) resolved ──
        if has_date and has_time:
            if not has_group:
                return self._resolve_class(data)
            return self._ask_next_or_confirm(data)

        # ── Rule 1: date only ──
        if has_date:
            return self._show_day(data)

        # ── Rule 2: time only ──
        if has_time:
            return self._show_time(data)

        # ── Nothing specific → 7-day overview, ask for a date ──
        return self._show_general_availability(lang)

    def _resolve_class(self, data: dict) -> str:
        """date + time are known: find the class, or narrow down to one."""
        lang = data.get("lang", "ru")
        date_str, ts = data["date"], data["time_start"]

        candidates = trial_logic.find_classes(self.bot_name, date_str, ts)

        if not candidates:
            return (self.asker.localize(lang, "no_classes_time", ts=ts)
                    + "\n\n" + self._show_day(data))

        # Two age groups can share a start time — the arena never hits this,
        # since a field id is unique. Ask rather than guess.
        if len(candidates) > 1:
            btn_text = self.asker.localize(
                lang, "choose_group",
                date=self.formatter.fmt_date(date_str, lang),
                ts=trial_logic.fmt_hhmm(candidates[0]["time_start"]),
                te=trial_logic.fmt_hhmm(candidates[0]["time_end"]),
            )
            return self.buttons.get_buttons(
                btn_text, [self._btn_title(c) for c in candidates],
            )

        return self._select_class(data, candidates[0])

    def _select_class(self, data: dict, cls: dict) -> str:
        """Bind a resolved class onto the draft, then ask for what is missing."""
        lang = data.get("lang", "ru")

        if not trial_logic.has_capacity(cls):
            logger.info("[TRIAL_FLOW] Group id=%s is full", cls["group_id"])
            return (self.asker.localize(lang, "class_full",
                                        group=cls.get("group_name") or "?")
                    + "\n\n" + self._show_day(data))

        data["group_id"] = cls["group_id"]
        data["time_start"] = trial_logic.fmt_hhmm(cls["time_start"])
        data["time_end"] = trial_logic.fmt_hhmm(cls["time_end"])
        data["date"] = str(cls["date"])
        self._update_draft(data)

        logger.info("[TRIAL_FLOW] Class resolved: group_id=%s %s %s-%s",
                    cls["group_id"], data["date"],
                    data["time_start"], data["time_end"])

        head = self.asker.localize(
            lang, "class_free",
            group=cls.get("group_name") or "?",
            date=self.formatter.fmt_date(data["date"], lang),
            ts=data["time_start"], te=data["time_end"],
        )
        nxt = self._ask_next_or_confirm(data)
        return f"{head}\n\n{nxt}" if nxt else head

    def _ask_next_or_confirm(self, data: dict) -> str:
        """Ask for the highest-priority missing field, or show the summary."""
        lang = data.get("lang", "ru")
        if data.get("child_name") is None:
            return self.asker.localize(lang, "ask_name")
        if data.get("child_age") is None:
            return self.asker.localize(lang, "ask_age")
        return self._confirm(data)

    def _confirm(self, data: dict) -> str:
        """Everything is known — re-check capacity, then show the summary."""
        lang = data.get("lang", "ru")

        cls = self._class_by_id(data)
        if cls and not trial_logic.has_capacity(cls):
            return (self.asker.localize(lang, "class_full",
                                        group=cls.get("group_name") or "?")
                    + "\n\n" + self._show_day(data))

        summary = (
            f"{self.asker.localize(lang, 'confirm_header')}\n"
            f"📅 {self.formatter.fmt_date(data['date'], lang)}\n"
            f"⏰ {data['time_start']}–{data['time_end']}\n"
            f"👥 {(cls or {}).get('group_name') or '?'}\n"
            f"👤 {data['child_name']}\n"
            f"🎂 {data['child_age']}\n\n"
            f"{self.asker.localize(lang, 'confirm_question')}"
        )
        return self.buttons.get_buttons(summary, [
            self.asker.localize(lang, "confirm_btn"),
            self.asker.localize(lang, "cancel_btn"),
        ])

    # ══════════════════════════════════════════════════════════════════════
    #  Availability views
    # ══════════════════════════════════════════════════════════════════════

    def _show_day(self, data: dict) -> str:
        """Rule 1: the classes on a given date."""
        lang = data.get("lang", "ru")
        date_str = data["date"]
        classes = trial_logic.find_classes(self.bot_name, date_str)

        if not classes:
            return (self.asker.localize(
                        lang, "no_classes_date",
                        date=self.formatter.fmt_date(date_str, lang))
                    + "\n\n" + self._available_dates(lang))

        return (self.asker.localize(lang, "day_classes",
                                    date=self.formatter.fmt_date(date_str, lang))
                + "\n" + self._list_classes(classes, lang)
                + "\n\n" + self.asker.localize(lang, "ask_time"))

    def _show_time(self, data: dict) -> str:
        """Rule 2: the days that have a class at a given time."""
        lang = data.get("lang", "ru")
        ts = data["time_start"]
        classes = trial_logic.find_classes(self.bot_name, None, ts)

        if not classes:
            return (self.asker.localize(lang, "no_classes_time", ts=ts)
                    + "\n\n" + self._show_general_availability(lang))

        lines = [
            f"  📅 {self.formatter.fmt_date(str(c['date']), lang)}"
            f" — {c.get('group_name') or '?'}"
            for c in classes
        ]
        return (self.asker.localize(lang, "time_classes", ts=ts)
                + "\n" + "\n".join(lines)
                + "\n\n" + self.asker.localize(lang, "ask_date"))

    def _show_general_availability(self, lang: str = "ru") -> str:
        """Full 7-day overview."""
        classes = trial_logic.find_classes(self.bot_name)
        if not classes:
            return self.asker.localize(lang, "no_classes_7d")

        by_date: dict[str, list] = {}
        for c in classes:
            by_date.setdefault(str(c["date"]), []).append(c)

        lines = []
        for d in sorted(by_date):
            # Two groups can share a slot; the overview shows times, not groups.
            ranges = dict.fromkeys(
                f"{trial_logic.fmt_hhmm(c['time_start'])}"
                f"–{trial_logic.fmt_hhmm(c['time_end'])}"
                for c in by_date[d]
            )
            times = ", ".join(ranges)
            lines.append(f"  📅 {self.formatter.fmt_date(d, lang)}: {times}")

        return (self.asker.localize(lang, "general_header") + "\n"
                + "\n".join(lines) + "\n\n"
                + self.asker.localize(lang, "general_prompt"))

    def _available_dates(self, lang: str) -> str:
        classes = trial_logic.find_classes(self.bot_name)
        if not classes:
            return self.asker.localize(lang, "no_classes_7d")
        dates = sorted({str(c["date"]) for c in classes})
        return (self.asker.localize(lang, "available_dates") + "\n"
                + "\n".join(f"  📅 {self.formatter.fmt_date(d, lang)}"
                            for d in dates))

    def _list_classes(self, classes: list[dict], lang: str) -> str:
        if not classes:
            return self.asker.localize(lang, "no_slots_empty")
        return "\n".join(
            f"  ⏰ {trial_logic.fmt_hhmm(c['time_start'])}"
            f"–{trial_logic.fmt_hhmm(c['time_end'])}"
            f" — {c.get('group_name') or '?'}"
            for c in classes
        )

    # ══════════════════════════════════════════════════════════════════════
    #  Terminal transitions
    # ══════════════════════════════════════════════════════════════════════

    def _finalize_trial(self, data: dict, chat_id: str, lang: str = "ru") -> str:
        """
        Confirm the draft. No payment, no reservation TTL: a trial is free, so
        this is where the arena's request_payment / Kaspi step would have been.
        """
        trial_id = data["trial_id"]

        cls = self._class_by_id(data)
        if cls and not trial_logic.has_capacity(cls):
            return (self.asker.localize(lang, "class_full",
                                        group=cls.get("group_name") or "?")
                    + "\n\n" + self._show_day(data))

        self._update_draft(data)
        if not academy_repo.confirm_trial(trial_id):
            logger.error("[TRIAL_FLOW] confirm_trial failed for id=%d", trial_id)
            return self.asker.localize(lang, "error")

        logger.info("[TRIAL_FLOW] Trial id=%d → confirmed", trial_id)
        refresh_all_trials()
        clear_history(chat_id)

        return self.asker.localize(
            lang, "confirmed",
            date=self.formatter.fmt_date(data["date"], lang),
            ts=data["time_start"], te=data["time_end"],
            group=(cls or {}).get("group_name") or "?",
            child_name=data.get("child_name", ""),
            child_age=data.get("child_age", ""),
        )

    def _cancel_draft(self, draft: dict, chat_id: str, lang: str = "ru") -> str:
        """Cancel the draft and return user-facing confirmation."""
        clear_history(chat_id)
        postgres.cancel_booking_trial(
            self.bot_name, draft["id"],
            actor_type="whatsapp", reason="user_cancel_llm_flow",
        )
        logger.info("[TRIAL_FLOW] Draft id=%d cancelled", draft["id"])
        return self.asker.localize(lang, "cancelled")

    # ══════════════════════════════════════════════════════════════════════
    #  Data mapping
    #
    #  academy_trials names its columns differently from the canonical dict
    #  used above (and from bookings). Translate only here, at the DB boundary.
    # ══════════════════════════════════════════════════════════════════════

    _DB_FIELDS = {
        "date": "trial_day",
        "time_start": "start_time",
        "time_end": "end_time",
        "group_id": "group_id",
        "child_name": "child_name",
        "child_age": "child_age",
        "lang": "language",
    }

    @staticmethod
    def _is_ready_for_confirm(draft: dict) -> bool:
        """True when every registration field is filled on the draft row.

        Same set the deterministic flow ends up with (trial_session.confirm):
        day, start, end, group, child name, child age.
        """
        return all([
            draft.get("trial_day"),
            draft.get("start_time"),
            draft.get("end_time"),
            draft.get("group_id"),
            draft.get("child_name"),
            draft.get("child_age") is not None,
        ])

    @staticmethod
    def _draft_to_data(draft: dict) -> dict:
        """Convert a DB draft row into the canonical data dict."""
        return {
            "date": str(draft["trial_day"]) if draft.get("trial_day") else None,
            "time_start": (trial_logic.fmt_hhmm(draft["start_time"])
                           if draft.get("start_time") else None),
            "time_end": (trial_logic.fmt_hhmm(draft["end_time"])
                         if draft.get("end_time") else None),
            "group_id": int(draft["group_id"]) if draft.get("group_id") else None,
            "child_name": draft.get("child_name"),
            "child_age": LlmTrialFlowHandler._int_or_none(draft.get("child_age")),
            "trial_id": draft["id"],
            "client_token": str(draft.get("client_token", "")),
        }

    @staticmethod
    def _merge_data(current: dict, extracted: dict) -> dict:
        """Only non-null extracted values overwrite what the draft already holds."""
        merged = dict(current)
        for key in ("date", "time_start", "time_end", "group_id",
                    "child_name", "child_age", "lang"):
            val = extracted.get(key)
            if val is not None:
                merged[key] = val

        # A new date or time invalidates the class resolved from the old ones.
        if (extracted.get("date") or extracted.get("time_start")) \
                and not extracted.get("group_id"):
            merged["group_id"] = None
            merged["time_end"] = None

        return merged

    def _update_draft(self, data: dict) -> None:
        """Persist the canonical dict back to the draft row."""
        trial_id = data.get("trial_id")
        if trial_id is None:
            return

        # child_age lands in an INTEGER column, and the draft is written before
        # _evaluate_and_respond gets to reject a junk value ("много"), so coerce
        # here rather than let the insert fail. The caller still sees the raw
        # value in `data` and still answers with age_invalid.
        patch = {}
        for key, column in self._DB_FIELDS.items():
            val = data.get(key)
            if val is None:
                continue
            if key == "child_age":
                if not self._valid_age(val):
                    continue
                val = int(val)
            patch[column] = val

        if patch:
            postgres.update_draft(self.bot_name, trial_id, **patch)

    # ══════════════════════════════════════════════════════════════════════
    #  Helpers
    # ══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _int_or_none(value):
        """Legacy rows may hold a non-numeric child_age (see trial_session)."""
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _valid_age(value) -> bool:
        try:
            return MIN_AGE <= int(value) <= MAX_AGE
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _btn_title(cls: dict) -> str:
        """WhatsApp caps button titles at 20 characters."""
        return (cls.get("group_name") or f"#{cls['group_id']}")[:20]

    def _class_by_id(self, data: dict) -> dict | None:
        """Re-read the resolved class so capacity is checked against live rows."""
        group_id = data.get("group_id")
        if group_id is None:
            return None
        for c in trial_logic.find_classes(
            self.bot_name, data.get("date"), data.get("time_start"),
        ):
            if c["group_id"] == int(group_id):
                return c
        return None

    def _apply_group_button(self, data: dict, user_message: str) -> None:
        """Bind a class when the user taps one of the group buttons."""
        if data.get("group_id") is not None:
            return
        if not (data.get("date") and data.get("time_start")):
            return

        title = user_message.strip()
        for c in trial_logic.find_classes(
            self.bot_name, data["date"], data["time_start"],
        ):
            if self._btn_title(c) == title[:20]:
                logger.info("[TRIAL_FLOW] Group button: %s → id=%s",
                            title, c["group_id"])
                data["group_id"] = c["group_id"]
                data["time_end"] = trial_logic.fmt_hhmm(c["time_end"])
                return
