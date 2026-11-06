"""The academy trial signup, message by message, on an in-memory draft.

Child: who → name → age → level → school shift → class list → pick → summary → yes.
Adult: who → name → age → class list → pick → summary → yes.
Every message's answers are saved to the draft, a side question's too, so a
step is never asked twice.
"""

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from handlers import llm_trial_flow
from handlers.llm_trial_flow import BOXING_T, T, LlmTrialFlowHandler
from integrations import trial as trial_logic

pytestmark = pytest.mark.no_db

TODAY = date(2026, 10, 26)  # Monday
TUE, WED, THU = date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29)

CHILD_GROUPS = [
    {"group_id": 1, "group_name": "Kids Timur", "trainer": "Бейсенов Тимур",
     "time_start": time(15, 0), "time_end": time(16, 0), "date": TUE},
    {"group_id": 2, "group_name": "Kids Madi", "trainer": "Махмуд Мади",
     "time_start": time(16, 0), "time_end": time(17, 0), "date": WED},
    {"group_id": 3, "group_name": "Kids Madi", "trainer": "Махмуд Мади",
     "time_start": time(16, 0), "time_end": time(17, 0), "date": THU},
]
ADULT_GROUPS = [
    {"group_id": 301, "group_name": "Box Adult", "trainer": "Бейсенов Тимур",
     "time_start": time(19, 15), "time_end": time(20, 30), "date": TUE},
]



_OPENERS = {"хочу записать ребёнка": {"audience": "child"}, "хочу записать себя": {"audience": "adult"}}
_LEVEL_ITEMS = {"1": "Beginner", "2": "Intermediate", "3": "Advanced"}
_SHIFT_ITEMS = {"1": "morning", "2": "afternoon", "3": "kindergarten"}


def _reads(text: str, answering: str | None) -> dict:
    """What the extractor gives for a plain answer to the step asked. Anything
    else a test spells out with `extracted`."""
    if answering == "audience":
        return {"audience": {"1": "child", "2": "adult"}.get(text)}
    if answering == "child_birth_year":
        return {"child_birth_year": TODAY.year - int(text)} if text.isdigit() else {}
    if answering == "experience":
        return {"experience": _LEVEL_ITEMS.get(text)}
    if answering == "school_shift":
        return {"school_shift": _SHIFT_ITEMS.get(text)}
    match = re.fullmatch(r"[A-ZА-ЯЁӘҒҚҢӨҰҮҺІ][a-zа-яёәғқңөұүһі]+", text or "")
    return {"child_name": text} if match else dict(_OPENERS.get(text, {}))


@pytest.fixture
def chat(monkeypatch):
    """Send messages one by one; .draft is the saved draft, .calls what happened."""
    state = {"draft": None, "extract": {}, "history": [], "calls": [], "child": list(CHILD_GROUPS),
             "adult": list(ADULT_GROUPS)}

    def save(fields):
        state["draft"].update({k: v for k, v in fields.items() if k != "language"})
        return {"ok": True, "data": {"trial": dict(state["draft"])}}

    def create(bot, chat_id, phone, lang):
        state["draft"] = {"id": 11, "phone": phone}
        return {"ok": True, "data": {"trial": dict(state["draft"])}}

    def assign(bot, trial_id, slot):
        return save({"trial_day": slot["date"], "start_time": slot["time_start"],
                     "end_time": slot["time_end"], "group_id": slot["group_id"]})

    def confirm(bot, chat_id, trial_id):
        state["calls"].append("confirmed")
        return {"ok": True, "data": {"trial": dict(state["draft"])}}

    def cancel(bot, chat_id, trial_id, reason):
        state["calls"].append(("cancelled", reason))
        state["draft"] = None

    def groups_info(bot_name=None, audience=None):
        adult = state["adult"] if bot_name == "dopsy_boxing" else []
        return adult if audience == "adult" else state["child"] + adult

    monkeypatch.setattr(llm_trial_flow, "today_almaty", lambda: TODAY)
    monkeypatch.setattr(llm_trial_flow, "academy_location", lambda *a: "")
    monkeypatch.setattr(llm_trial_flow, "extract_trial_details",
                        lambda history, text, answering=None, wish=None: dict(
                            state["extract"].pop(text, None) or _reads(text, answering)))
    repo = llm_trial_flow.academy_repo
    monkeypatch.setattr(repo, "get_existing_trial_draft",
                        lambda phone, bot: dict(state["draft"]) if state["draft"] else None)
    monkeypatch.setattr(repo, "get_trial", lambda trial_id: dict(state["draft"]))
    monkeypatch.setattr(repo, "get_groups_info", groups_info)
    monkeypatch.setattr(repo, "get_group_by_id", lambda group_id: None)
    service = llm_trial_flow.trial_service
    monkeypatch.setattr(service, "create_or_get_draft", create)
    monkeypatch.setattr(service, "update_intake", lambda bot, trial_id, fields: save(fields))
    monkeypatch.setattr(service, "assign_slot", assign)
    monkeypatch.setattr(service, "confirm_trial", confirm)
    monkeypatch.setattr(service, "cancel_trial", cancel)
    logic = llm_trial_flow.trial_logic
    monkeypatch.setattr(logic, "get_eligible_trial_slots", lambda *a, **k: list(state["child"]))
    monkeypatch.setattr(logic, "get_adult_trial_slots", lambda bot: list(state["adult"]))
    monkeypatch.setattr(logic, "get_birth_year_trial_slots", lambda *a: [])
    monkeypatch.setattr(logic, "is_trial_slot_eligible", lambda bot, draft: True)

    class Chat:
        bot = "dopsy_boxing"
        lang = "ru"

        def send(self, text, extracted=None, intent="trial_continue"):
            if extracted:
                state["extract"][text] = extracted
            reply = LlmTrialFlowHandler().handle(
                "chat-1", "7700", self.bot, text, state["history"], self.lang, intent=intent,
            )
            self._remember(text, reply)
            return reply

        def ask(self, text, extracted=None):
            """A side question: answers saved, the pending step returned."""
            if extracted:
                state["extract"][text] = extracted
            handler = LlmTrialFlowHandler()
            handler.absorb("chat-1", "7700", self.bot, text, state["history"], self.lang)
            reply = handler.pending_prompt(self.bot, self.draft, self.lang)
            self._remember(text, reply)
            return reply

        def _remember(self, text, reply):
            state["history"] += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]

        @property
        def draft(self):
            return state["draft"]

        @property
        def calls(self):
            return state["calls"]

        @property
        def extracted(self):
            return state["extract"]

        @property
        def history(self):
            return state["history"]

        def no_child_classes(self):
            state["child"] = []

        def set_child_classes(self, classes):
            state["child"] = classes

        def no_adult_classes(self):
            state["adult"] = []

    return Chat()


def _ask(key, lang="ru"):
    return BOXING_T.get(key, T[key])[lang]


def test_child_signup_end_to_end(chat):
    assert "1. Детская группа (3–16 лет)" in chat.send("Здравствуйте, хочу записаться")
    assert chat.send("1") == _ask("ask_name")
    assert chat.send("Алихан") == _ask("ask_birth_year")
    assert chat.send("8") == _ask("ask_experience")
    assert chat.send("1") == _ask("ask_school_shift")
    listing = chat.send("2")
    assert "📅 Вт 27.10.2026\n1. 15:00–16:00 — Бейсенов Тимур" in listing
    assert "📅 Чт 29.10.2026\n3. 16:00–17:00 — Махмуд Мади" in listing

    summary = chat.send("3")
    assert "Имя: Алихан" in summary and "Чт 29.10.2026" in summary
    assert "Тренер: Махмуд Мади" in summary
    assert chat.draft["group_id"] == 3

    done = chat.send("да", intent="confirm_yes")
    assert chat.calls == ["confirmed"]
    assert "Что взять с собой:" in done and "сменную обувь" in done
    assert chat.draft["child_birth_year"] == TODAY.year - 8
    assert (chat.draft["experience"], chat.draft["school_shift"]) == ("Beginner", "afternoon")


def test_adult_signup_end_to_end(chat):
    reply = chat.send("хочу заниматься сам, мне 25", {"audience": "adult", "signup_for": "self"})
    assert reply.endswith(_ask("ask_name_self"))
    assert chat.send("Ерлан") == BOXING_T["ask_birth_year_self"]["ru"]
    listing = chat.send("25")
    assert listing == T["choose_class"]["ru"].format(lines="📅 Вт 27.10.2026\n1. 19:15 — Бейсенов Тимур")

    summary = chat.send("1")
    assert "Взрослая группа" in summary and "Имя: Ерлан" in summary

    done = chat.send("да", intent="confirm_yes")
    assert chat.calls == ["confirmed"]
    assert "Что взять с собой" in done
    assert chat.draft["audience"] == "adult" and chat.draft.get("experience") is None


def test_football_never_asks_who(chat):
    chat.bot = "dopsy_fs_school"
    assert chat.send("хочу на пробное").endswith(T["ask_name"]["ru"])
    assert chat.draft["audience"] == "child"
    chat.send("Тимур, 8 лет", {"child_name": "Тимур", "child_birth_year": TODAY.year - 8})
    assert (chat.draft["child_name"], chat.draft["child_birth_year"]) == ("Тимур", TODAY.year - 8)
    chat.send("новичок", {"experience": "Beginner"})
    chat.send("утренняя", {"school_shift": "morning"})
    chat.send("1")
    done = chat.send("да", intent="confirm_yes")
    assert "Для первой тренировки вам понадобятся:" in done


def test_side_question_keeps_its_answer(chat):
    chat.send("хочу записать ребёнка")
    chat.send("Алихан")

    pending = chat.ask("8 лет, а сколько стоит?", {"child_birth_year": TODAY.year - 8})

    assert chat.draft["child_birth_year"] == TODAY.year - 8
    assert pending == _ask("ask_experience")


def test_side_question_without_an_answer_repeats_the_step(chat):
    chat.send("хочу записать ребёнка")
    assert chat.ask("где вы находитесь?") == _ask("ask_name")
    assert chat.draft.get("child_name") is None


def test_answers_given_ahead_are_not_asked_again(chat):
    reply = chat.send("Хочу записать сына Алихана, ему 8 лет", {
        "audience": "child", "child_name": "Алихан", "child_birth_year": TODAY.year - 8,
    })
    assert chat.draft["audience"] == "child"
    assert reply.endswith(_ask("ask_experience"))


def test_parents_own_age_is_not_the_childs(chat):
    chat.send("мне 35 лет, хочу записать сына", {"audience": "child"})
    assert chat.draft["audience"] == "child"
    assert chat.draft.get("child_birth_year") is None


@pytest.mark.parametrize(("who", "age"), [("ребёнка", "2"), ("ребёнка", "61"), ("себя", "61")])
def test_age_outside_the_group_ends_the_signup(chat, who, age):
    chat.send(f"хочу записать {who}")
    chat.send("Аня")

    reply = chat.send(age)

    assert reply.startswith(f"К сожалению, для возраста {age} лет группы нет")
    assert "+7 700 555 2202" in reply
    assert chat.calls == [("cancelled", "age_out_of_range")]


@pytest.mark.parametrize("text", ["да", "новичок", "утренняя"])
def test_non_names_are_not_saved_as_the_name(chat, text):
    chat.send("хочу записать ребёнка")
    assert chat.send(text) == _ask("ask_name")
    assert chat.draft.get("child_name") is None


def test_a_long_name_is_refused_with_the_reason(chat):
    chat.send("хочу записать ребёнка")
    long_name = "Абдурахманов Алихан Ерланулы Сарсенбаев"
    reply = chat.send(long_name, {"child_name": long_name})
    assert reply == BOXING_T["name_too_long"]["ru"].format(max_len=llm_trial_flow.CHILD_NAME_MAX_LEN)


def _to_class_list(chat):
    chat.send("хочу записать ребёнка")
    for answer in ("Алихан", "8", "1", "2"):
        reply = chat.send(answer)
    return reply


def test_a_named_trainer_narrows_the_list_and_keeps_its_numbering(chat):
    _to_class_list(chat)
    narrowed = chat.send("Махмуд", {"preferred_trainers": ["Махмуд"]})
    assert "Тимур" not in narrowed and "📅 Чт 29.10.2026\n2. 16:00–17:00" in narrowed

    chat.send("2", {"preferred_trainers": ["Махмуд"]})

    assert chat.draft["group_id"] == 3


def test_both_named_trainers_stay_in_the_list(chat):
    _to_class_list(chat)
    reply = chat.send("Тимур или Мади", {"preferred_trainers": ["Тимур", "Мади"]})
    assert "Тимур" in reply and "Мади" in reply


def test_a_time_range_keeps_the_classes_that_start_in_it(chat):
    _to_class_list(chat)
    reply = chat.send("после обеда", {"preferred_time_start": "15:00", "preferred_time_end": "16:00"})
    assert chat.draft["group_id"] == 1 and "Подтверждаете запись?" in reply


def test_the_wish_stays_after_a_side_question(chat):
    _to_class_list(chat)
    wish = {"preferred_time_start": "16:00", "preferred_time_end": "18:00"}
    assert "Вот что подходит" in chat.send("после обеда", wish)

    pending = chat.ask("тренеры хорошие?", wish)

    assert "Вот что подходит" in pending and "Тимур" not in pending


def test_several_weekdays_all_count(chat):
    _to_class_list(chat)
    reply = chat.send("вторник и четверг", {"preferred_weekdays": [TUE.weekday(), THU.weekday()]})
    assert "Вт 27.10.2026" in reply and "Чт 29.10.2026" in reply and "Ср 28.10.2026" not in reply


def test_a_wish_nothing_fits_says_so_and_shows_the_closest(chat):
    _to_class_list(chat)
    reply = chat.send("вечером у Мади", {
        "preferred_time_start": "18:00", "preferred_time_end": "23:59", "preferred_trainers": ["Мади"],
    })
    assert reply.startswith(BOXING_T["preferred_unavailable"]["ru"])
    assert "Мади" in reply and "Тимур" not in reply


def test_an_unreal_time_is_not_saved(chat):
    _to_class_list(chat)
    chat.send("в 25:00", {"preferred_time_start": "25:00"})
    assert chat.draft.get("preferred_time_start") is None


def test_a_long_list_is_capped_and_numbered_across_days(chat):
    many = [{**CHILD_GROUPS[0], "group_id": i, "date": TUE + timedelta(days=i)} for i in range(12)]
    chat.set_child_classes(many)
    reply = _to_class_list(chat)
    assert "8. 15:00" in reply and "9. 15:00" not in reply

    chat.send("8")

    assert chat.draft["group_id"] == 7


def test_a_named_day_with_one_class_picks_it(chat):
    _to_class_list(chat)
    reply = chat.send("в среду", {"preferred_date": WED.isoformat()})
    assert chat.draft["group_id"] == 2
    assert "Подтверждаете запись?" in reply


def test_a_day_without_classes_shows_the_list_with_a_note(chat):
    _to_class_list(chat)
    reply = chat.send("в субботу", {"preferred_date": "2026-10-31"})
    assert reply.startswith(BOXING_T["preferred_unavailable"]["ru"])
    assert chat.draft.get("group_id") is None


def test_a_trainers_name_does_not_replace_the_childs(chat):
    _to_class_list(chat)
    chat.send("Тимурға жазыңыз", {"child_name": "Тимур"})
    assert chat.draft["child_name"] == "Алихан"


def test_summary_invites_changes(chat):
    _to_class_list(chat)
    summary = chat.send("1")
    assert summary.endswith("Подтверждаете запись?\n\nЕсли нужно что-то изменить, подскажите.")


def test_the_router_confirms_at_the_summary(chat):
    _to_class_list(chat)
    chat.send("1")
    chat.send("иә, бәрі дұрыс", intent="confirm_yes")
    assert chat.calls == ["confirmed"]


def test_the_router_declines_at_the_summary(chat):
    _to_class_list(chat)
    chat.send("1")
    assert chat.send("нет", intent="confirm_no") == BOXING_T["declined"]["ru"]
    assert chat.calls == [("cancelled", "user_declined_gated_trial")]


def test_a_message_that_changes_nothing_at_the_summary_shows_it_again(chat):
    _to_class_list(chat)
    summary = chat.send("1")
    assert chat.send("не знаю") == summary
    assert chat.calls == []


def test_router_failure_at_the_summary_shows_it_again(chat):
    _to_class_list(chat)
    summary = chat.send("1")
    assert chat.send("да", intent=None) == summary
    assert chat.calls == []


def test_extractor_failure_at_the_summary_keeps_the_signup(chat):
    _to_class_list(chat)
    summary = chat.send("1")
    assert chat.send("поменяйте что-нибудь", {"failed": True}) == summary
    assert chat.calls == []


def test_a_pick_from_the_list_survives_an_extractor_failure(chat):
    _to_class_list(chat)
    chat.extracted["1"] = {"failed": True}
    handler = LlmTrialFlowHandler()
    handler.handle("chat-1", "7700", chat.bot, "1", chat.history, chat.lang, intent="trial_continue")
    assert chat.draft["group_id"] and not handler.extractor_failed


@pytest.mark.parametrize("intent", ["confirm_yes", "confirm_no"])
def test_a_yes_or_no_before_the_summary_decides_nothing(chat, intent):
    chat.send("хочу записать ребёнка")
    assert chat.send("да", intent=intent) == _ask("ask_name")
    assert chat.calls == []


def test_a_correction_at_the_summary_is_applied(chat):
    _to_class_list(chat)
    chat.send("1")
    reply = chat.send("нет, ему оказывается 9", {"child_birth_year": TODAY.year - 9})
    assert chat.calls == []
    assert chat.draft["child_birth_year"] == TODAY.year - 9
    assert chat.draft["child_name"] == "Алихан" and chat.draft["experience"] == "Beginner"
    assert chat.draft["group_id"] is None and reply.startswith("Выберите тренировку")


def test_another_day_at_the_summary_repicks(chat):
    _to_class_list(chat)
    chat.send("1")
    reply = chat.send("давайте лучше в среду", {"preferred_date": WED.isoformat()})
    assert chat.draft["group_id"] == 2 and "Ср 28.10.2026" in reply


def test_change_request_without_details_shows_the_list(chat):
    _to_class_list(chat)
    chat.send("1")
    reply = chat.send("хочу поменять время", {"wants_change": True})
    assert chat.draft["group_id"] is None and reply.startswith("Выберите тренировку")


def test_age_out_of_range_at_the_summary_ends_the_signup(chat):
    _to_class_list(chat)
    chat.send("1")
    assert chat.send("ему 61 лет", {"child_birth_year": TODAY.year - 61}).startswith("К сожалению, для возраста 61 лет группы нет")
    assert chat.calls == [("cancelled", "age_out_of_range")]


def test_a_new_answer_after_the_pick_drops_the_class(chat):
    _to_class_list(chat)
    chat.send("1")
    reply = chat.send("ой, ему 9 лет", {"child_birth_year": TODAY.year - 9})
    assert chat.draft["group_id"] is None
    assert chat.draft["child_birth_year"] == TODAY.year - 9
    assert reply.startswith(T["choose_class"]["ru"].split("\n")[0])


def test_no_fitting_class_sends_to_the_admin(chat):
    chat.no_child_classes()
    chat.send("хочу записать ребёнка")
    for answer in ("Алихан", "8", "3"):
        chat.send(answer)
    assert chat.send("2") == BOXING_T["no_groups_call_admin"]["ru"]


def test_kazakh_signup_uses_kazakh_texts(chat):
    chat.lang = "kk"
    assert "1. Балалар тобы (3–16 жас)" in chat.send("сынақ сабағына жазылғым келеді")
    assert chat.send("1") == _ask("ask_name", "kk")


def test_for_myself_alone_does_not_pick_the_group(chat):
    reply = chat.send("для себя", {"signup_for": "self"})
    assert "1. Детская группа (3–16 лет)" in reply
    assert chat.draft.get("audience") is None


def test_a_child_signing_up_alone_stays_in_the_childrens_group(chat):
    chat.send("для себя, мне 12", {"audience": "child", "signup_for": "self",
                                   "child_birth_year": TODAY.year - 12})
    assert chat.draft["audience"] == "child"
    assert chat.draft["child_birth_year"] == TODAY.year - 12


def test_adult_age_from_the_first_message_is_kept(chat):
    reply = chat.send("Жасым 36 та, мені ересектер тобына боксқа сынақ сабағына жазасың ба?", {
        "audience": "adult", "signup_for": "self", "child_birth_year": TODAY.year - 36,
    })
    assert reply.endswith(_ask("ask_name_self"))

    listing = chat.send("Есімім Әсет", {"child_name": "Әсет"})

    assert chat.draft["child_birth_year"] == TODAY.year - 36
    assert listing.startswith(T["choose_class"]["ru"].split("\n")[0])
    assert chat.calls == []


def test_an_adult_age_in_the_child_flow_moves_the_same_draft(chat):
    chat.send("хочу записать ребёнка")
    chat.send("Аня")

    chat.send("25")

    assert chat.draft["audience"] == "adult"
    assert chat.draft["child_name"] == "Аня"
    assert chat.calls == []


def test_a_stated_adult_stays_adult_with_no_adult_classes(chat):
    chat.no_adult_classes()
    chat.send("ересектер тобына жазыңыз", {"audience": "adult", "signup_for": "self"})
    assert chat.draft["audience"] == "adult"
    chat.send("Әсет")

    reply = chat.send("36")

    assert chat.draft["audience"] == "adult" and chat.draft["child_birth_year"] == TODAY.year - 36
    assert reply == BOXING_T["no_groups_call_admin"]["ru"]
    assert chat.calls == []


def test_an_adult_age_with_no_adult_classes_still_moves_to_the_adult_group(chat):
    chat.no_adult_classes()
    chat.send("хочу записать ребёнка")
    chat.send("Аня")

    reply = chat.send("36")

    assert chat.draft["audience"] == "adult"
    assert reply == BOXING_T["no_groups_call_admin"]["ru"]
    assert chat.calls == []


def test_football_keeps_a_stated_adult(chat):
    chat.bot = "dopsy_fs_school"
    chat.send("хочу во взрослую группу", {"audience": "adult"})
    assert chat.draft["audience"] == "adult"


def test_the_group_is_asked_even_with_no_adult_classes(chat):
    chat.no_adult_classes()
    assert "2. Взрослая группа (17–60 лет)" in chat.send("хочу на пробное")
    assert chat.draft.get("audience") is None

    chat.send("2")

    assert chat.draft["audience"] == "adult"


def test_the_extractor_decides_what_a_name_is(chat):
    chat.send("хочу записать ребёнка")
    chat.send("Ботагөз", {"child_name": "Ботагөз"})
    assert chat.draft["child_name"] == "Ботагөз"


def test_the_extractor_is_told_the_step(chat, monkeypatch):
    steps = []
    monkeypatch.setattr(llm_trial_flow, "extract_trial_details",
                        lambda history, text, answering=None, wish=None: steps.append(answering) or {})
    chat.send("хочу на пробное")
    chat.send("Әсет")
    assert steps == [None, "audience"]


@pytest.mark.parametrize(("now", "expected"), [
    (datetime(2026, 10, 9, 16, 0), date(2026, 10, 9)),   # Friday class at 17:00 still ahead
    (datetime(2026, 10, 9, 21, 16), date(2026, 10, 16)),  # already over → next Friday
])
def test_class_that_already_started_today_moves_to_next_week(monkeypatch, now, expected):
    tz = ZoneInfo("Asia/Almaty")
    monkeypatch.setattr("utils.now_almaty", lambda: now.replace(tzinfo=tz))
    assert trial_logic._next_class_date(4, time(17, 0)) == expected
