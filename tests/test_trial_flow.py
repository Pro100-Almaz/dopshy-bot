"""The academy trial signup, message by message, on an in-memory draft.

Child: who → name → age → level → school shift → class list → pick → summary → yes.
Adult: who → name → age → class list → pick → summary → yes.
Every message's answers are saved to the draft, a side question's too, so a
step is never asked twice.
"""

import re
from datetime import date, datetime, time
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



def _plain_name(text: str) -> dict:
    """What the extractor gives for a bare name answer like «Алихан»."""
    match = re.fullmatch(r"([A-ZА-ЯЁӘҒҚҢӨҰҮҺІ][a-zа-яёәғқңөұүһі]+)(?:,.*)?", text or "")
    return {"child_name": match.group(1)} if match else {}


@pytest.fixture
def chat(monkeypatch):
    """Send messages one by one; .draft is the saved draft, .calls what happened."""
    state = {"draft": None, "extract": {}, "history": [], "calls": [], "child": list(CHILD_GROUPS)}

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
        if audience == "adult":
            return ADULT_GROUPS if bot_name == "dopsy_boxing" else []
        return state["child"] + (ADULT_GROUPS if bot_name == "dopsy_boxing" else [])

    monkeypatch.setattr(llm_trial_flow, "today_almaty", lambda: TODAY)
    monkeypatch.setattr(llm_trial_flow, "academy_location", lambda *a: "")
    monkeypatch.setattr(llm_trial_flow, "extract_trial_details",
                        lambda history, text: dict(state["extract"].pop(text, None) or _plain_name(text)))
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
    monkeypatch.setattr(logic, "get_adult_trial_slots", lambda bot: list(ADULT_GROUPS))
    monkeypatch.setattr(logic, "get_birth_year_trial_slots", lambda *a: [])
    monkeypatch.setattr(logic, "is_trial_slot_eligible", lambda bot, draft: True)

    class Chat:
        bot = "dopsy_boxing"
        lang = "ru"

        def send(self, text, extracted=None):
            if extracted:
                state["extract"][text] = extracted
            reply = LlmTrialFlowHandler().handle("chat-1", "7700", self.bot, text, state["history"], self.lang)
            self._remember(text, reply)
            return reply

        def ask(self, text, extracted=None):
            """A side question: answers saved, the pending step returned."""
            if extracted:
                state["extract"][text] = extracted
            handler = LlmTrialFlowHandler()
            handler.absorb("chat-1", "7700", self.bot, text, state["history"], self.lang)
            reply = handler.pending_prompt(self.bot, self.draft, self.lang, text)
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

        def no_child_classes(self):
            state["child"] = []

    return Chat()


def _ask(key, lang="ru"):
    return BOXING_T.get(key, T[key])[lang]


def test_child_signup_end_to_end(chat):
    assert "1. Ребёнка (3–16 лет)" in chat.send("Здравствуйте, хочу записаться")
    assert chat.send("1") == _ask("ask_name")
    assert chat.send("Алихан") == _ask("ask_birth_year")
    assert chat.send("8") == _ask("ask_experience")
    assert chat.send("1") == _ask("ask_school_shift")
    listing = chat.send("2")
    assert "1. Вт 27.10.2026 — 15:00–16:00 — Бейсенов Тимур" in listing
    assert "3. Чт 29.10.2026 — 16:00–17:00 — Махмуд Мади" in listing

    summary = chat.send("3")
    assert "Имя ребенка: Алихан" in summary and "Чт 29.10.2026" in summary
    assert "Тренер: Махмуд Мади" in summary
    assert chat.draft["group_id"] == 3

    done = chat.send("да")
    assert chat.calls == ["confirmed"]
    assert "Что взять с собой:" in done and "сменную обувь" in done
    assert chat.draft["child_birth_year"] == TODAY.year - 8
    assert (chat.draft["experience"], chat.draft["school_shift"]) == ("Beginner", "afternoon")


def test_adult_signup_end_to_end(chat):
    assert chat.send("хочу заниматься сам").endswith(_ask("ask_name_self"))
    assert chat.send("Ерлан") == BOXING_T["ask_birth_year_self"]["ru"]
    listing = chat.send("25")
    assert listing == T["choose_class"]["ru"].format(lines="1. Вт 27.10.2026 — 19:15 — Бейсенов Тимур")

    summary = chat.send("1")
    assert "Взрослая группа" in summary and "Имя: Ерлан" in summary

    done = chat.send("да")
    assert chat.calls == ["confirmed"]
    assert "Что взять с собой" in done
    assert chat.draft["audience"] == "adult" and chat.draft.get("experience") is None


def test_football_has_no_adult_group_so_never_asks_who(chat):
    chat.bot = "dopsy_fs_school"
    assert chat.send("хочу на пробное").endswith(T["ask_name"]["ru"])
    chat.send("Тимур, 8 лет")
    assert (chat.draft["child_name"], chat.draft["child_birth_year"]) == ("Тимур", TODAY.year - 8)
    chat.send("новичок")
    chat.send("утренняя")
    chat.send("1")
    done = chat.send("да")
    assert "Для первой тренировки вам понадобятся:" in done


def test_side_question_keeps_its_answer(chat):
    chat.send("хочу записать ребёнка")
    chat.send("Алихан")

    pending = chat.ask("8 лет, а сколько стоит?")

    assert chat.draft["child_birth_year"] == TODAY.year - 8
    assert pending == _ask("ask_experience")


def test_side_question_without_an_answer_repeats_the_step(chat):
    chat.send("хочу записать ребёнка")
    assert chat.ask("где вы находитесь?") == _ask("ask_name")
    assert chat.draft.get("child_name") is None


def test_answers_given_ahead_are_not_asked_again(chat):
    reply = chat.send("Хочу записать сына Алихана, ему 8 лет", {"child_name": "Алихан"})
    assert chat.draft["audience"] == "child"
    assert reply.endswith(_ask("ask_experience"))


def test_parents_own_age_is_not_the_childs(chat):
    chat.send("мне 35 лет, хочу записать сына")
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
    reply = chat.send("Абдурахманов Алихан Ерланулы Сарсенбаев")
    assert reply == BOXING_T["name_too_long"]["ru"].format(max_len=llm_trial_flow.CHILD_NAME_MAX_LEN)


def _to_class_list(chat):
    chat.send("хочу записать ребёнка")
    for answer in ("Алихан", "8", "1", "2"):
        reply = chat.send(answer)
    return reply


def test_a_named_trainer_narrows_the_list_and_keeps_its_numbering(chat):
    _to_class_list(chat)
    narrowed = chat.send("Махмуд")
    assert "Тимур" not in narrowed and "2. Чт 29.10.2026" in narrowed

    chat.send("2")

    assert chat.draft["group_id"] == 3


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


@pytest.mark.parametrize("text", ["да", "всё верно", "хорошо", "ага", "иә, бәрі дұрыс"])
def test_yes_like_words_confirm(chat, text):
    _to_class_list(chat)
    chat.send("1")
    chat.send(text)
    assert chat.calls == ["confirmed"]


@pytest.mark.parametrize("text", ["нет", "отмена", "не знаю", "кстати можешь дать информацию по тренерам?"])
def test_anything_else_at_the_summary_cancels(chat, text):
    _to_class_list(chat)
    chat.send("1")
    assert chat.send(text) == BOXING_T["declined"]["ru"]
    assert chat.calls == [("cancelled", "user_declined_gated_trial")]


def test_a_correction_at_the_summary_is_applied(chat):
    _to_class_list(chat)
    chat.send("1")
    reply = chat.send("нет, ему оказывается 9")
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
    reply = chat.send("хочу поменять время")
    assert chat.draft["group_id"] is None and reply.startswith("Выберите тренировку")


def test_age_out_of_range_at_the_summary_ends_the_signup(chat):
    _to_class_list(chat)
    chat.send("1")
    assert chat.send("ему 61 лет").startswith("К сожалению, для возраста 61 лет группы нет")
    assert chat.calls == [("cancelled", "age_out_of_range")]


def test_a_new_answer_after_the_pick_drops_the_class(chat):
    _to_class_list(chat)
    chat.send("1")
    reply = chat.send("ой, ему 9 лет")
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
    assert "1. Баланы (3–16 жас)" in chat.send("сынақ сабағына жазылғым келеді")
    assert chat.send("1") == _ask("ask_name", "kk")


@pytest.mark.parametrize(("text", "expected"), [
    ("хочу заниматься сам", "adult"),
    ("для себя", "adult"),
    ("ересектер тобына", "adult"),
    ("мне 25 лет", "adult"),
    ("мне 17 лет", "adult"),
    ("хочу записать сына", "child"),
    ("мне 35, хочу записать сына", "child"),
    ("балам 8 жаста", "child"),
    ("хочу на пробное", None),
])
def test_detect_audience(text, expected):
    assert llm_trial_flow._detect_audience(text) == expected


@pytest.mark.parametrize(("text", "expected"), [("1", "child"), ("2", "adult"), ("2.", "adult"), ("Себя", "adult")])
def test_audience_choice(text, expected):
    assert llm_trial_flow._audience_choice(text) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("менің атым Айдос", "Айдос"),
    ("меня зовут Алихан", "Алихан"),
    ("сына зовут Алихан", "Алихан"),
    ("Мен Айдоспын", "Айдос"),
    ("Анна Мария", "Анна Мария"),
])
def test_name_lead_in_is_stripped(text, expected):
    assert llm_trial_flow._normalize_manual_value("child_name", text) == expected


@pytest.mark.parametrize(("now", "expected"), [
    (datetime(2026, 10, 9, 16, 0), date(2026, 10, 9)),   # Friday class at 17:00 still ahead
    (datetime(2026, 10, 9, 21, 16), date(2026, 10, 16)),  # already over → next Friday
])
def test_class_that_already_started_today_moves_to_next_week(monkeypatch, now, expected):
    tz = ZoneInfo("Asia/Almaty")
    monkeypatch.setattr("utils.now_almaty", lambda: now.replace(tzinfo=tz))
    assert trial_logic._next_class_date(4, time(17, 0)) == expected
