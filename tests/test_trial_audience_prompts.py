"""The draft's saved group (adult / child) reaches every LLM of the academy
trial flow, so a stale or short chat history can't turn an adult signup into a
child-sounding one.

The offline tests check what each prompt is given. The live tests at the end
call the real model and run only with RUN_LLM_EVALS=1.
"""
import json
import os
from types import SimpleNamespace

import pytest

from chat import llm
from handlers import academy_extractor, llm_trial_flow
from handlers.llm_trial_flow import LlmTrialFlowHandler
from utils import today_almaty

pytestmark = pytest.mark.no_db

_ADULT_AGE = 30
_ADULT_DRAFT = {
    "id": 1, "audience": "adult", "signup_for": "self", "child_name": "Арман",
    "child_birth_year": today_almaty().year - _ADULT_AGE,
}
_CHILD_DRAFT = {
    "id": 2, "audience": "child", "signup_for": "other", "child_name": "Али",
    "child_birth_year": today_almaty().year - 10, "experience": "Beginner", "school_shift": "morning",
}
# The stale history from the logs: the age message never reached this worker.
_STALE_HISTORY = [
    {"role": "user", "content": "Бокска жазылғым келеді"},
    {"role": "assistant", "content": "Қай топқа жазыласыз?"},
]


def _chat_completion(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _tool_completion(args: dict):
    call = SimpleNamespace(function=SimpleNamespace(arguments=json.dumps(args)))
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))])


def _system_prompt_of_question_reply(monkeypatch, **kwargs) -> str:
    seen = {}

    def fake_create(**call):
        seen.update(call)
        return _chat_completion("ok")

    monkeypatch.setattr(llm._client.chat.completions, "create", fake_create)
    # conftest replaces get_question_reply with an offline stub; use the real one.
    monkeypatch.setattr(llm, "get_question_reply", _REAL_GET_QUESTION_REPLY)
    llm.get_question_reply("ru", "Как вас зовут?", _STALE_HISTORY, **kwargs)
    return seen["messages"][0]["content"]


_REAL_GET_QUESTION_REPLY = llm.get_question_reply


# ── (a) + (b) + (d): question rewording ──

def test_adult_question_prompt_forbids_child_wording_and_trusts_the_draft(monkeypatch):
    prompt = _system_prompt_of_question_reply(monkeypatch, actor="self", audience="adult")

    assert "ВЗРОСЛАЯ" in prompt
    assert "Никогда не упоминай ребёнка" in prompt
    assert "верь заявке, а не истории" in prompt


def test_adult_signing_up_another_adult_is_not_asked_about_a_child(monkeypatch):
    prompt = _system_prompt_of_question_reply(monkeypatch, actor="other", audience="adult")

    assert "другого взрослого" in prompt
    assert "Клиент записывает ребёнка" not in prompt


def test_child_question_prompt_keeps_the_child_wording(monkeypatch):
    prompt = _system_prompt_of_question_reply(monkeypatch, actor="other", audience="child")

    assert "ДЕТСКАЯ" in prompt and "Клиент записывает ребёнка" in prompt
    assert "Никогда не упоминай ребёнка" not in prompt


def test_unknown_group_adds_nothing(monkeypatch):
    prompt = _system_prompt_of_question_reply(monkeypatch, actor=None, audience=None)

    assert "Группа записи" not in prompt and "верь заявке" not in prompt


def test_flow_passes_the_stored_audience_to_the_question_llm(monkeypatch):
    seen = {}

    def fake_reply(lang, template, history, actor=None, audience=None):
        seen["audience"] = audience
        return "Как вас зовут?"

    monkeypatch.setattr(llm, "get_question_reply", fake_reply)

    LlmTrialFlowHandler()._evaluate_and_respond(
        "chat-1", "dopsy_boxing", {"id": 1, "audience": "adult", "signup_for": "self"}, "ru",
        history=_STALE_HISTORY,
    )

    assert seen["audience"] == "adult"


# ── (c) + (d) + (f): side-question LLM ──

def test_adult_draft_state_gives_age_and_adult_only_rules():
    ctx = LlmTrialFlowHandler().draft_state_context("dopsy_boxing", _ADULT_DRAFT)

    assert "Группа: ВЗРОСЛАЯ" in ctx and "Кто записывается: сам клиент" in ctx
    assert f"Возраст: {_ADULT_AGE} лет" in ctx
    assert str(_ADULT_DRAFT["child_birth_year"]) not in ctx
    assert "Смена" not in ctx and "Опыт" not in ctx
    assert "верь заявке, а не истории" in ctx
    assert "никогда не предлагай время детских групп" in ctx
    assert "Текущий шаг" not in ctx


def test_child_draft_state_keeps_child_fields_and_rules():
    ctx = LlmTrialFlowHandler().draft_state_context("dopsy_boxing", _CHILD_DRAFT)

    assert "Группа: ДЕТСКАЯ" in ctx and "Кто записывается: ребёнок клиента" in ctx
    assert "Смена:" in ctx and "Опыт:" in ctx
    assert "никогда не о взрослой" in ctx


def test_no_draft_no_state():
    assert LlmTrialFlowHandler().draft_state_context("dopsy_boxing", None) is None


# ── (e): extractor ──

def _extractor_prompt(monkeypatch, **kwargs) -> str:
    seen = {}

    def fake_create(**call):
        seen.update(call)
        return _tool_completion({})

    monkeypatch.setattr(academy_extractor.client.chat.completions, "create", fake_create)
    academy_extractor.extract_trial_details(_STALE_HISTORY, "Арман", "child_name", **kwargs)
    return seen["messages"][0]["content"]


def test_extractor_is_told_the_saved_group_and_who_signs_up(monkeypatch):
    prompt = _extractor_prompt(monkeypatch, known={"audience": "adult", "signup_for": "self"})

    assert "Уже сохранено в заявке" in prompt
    assert "audience=adult" in prompt and "signup_for=self" in prompt


def test_extractor_prompt_unchanged_without_saved_group(monkeypatch):
    assert "Уже сохранено в заявке" not in _extractor_prompt(monkeypatch)
    assert "Уже сохранено в заявке" not in _extractor_prompt(monkeypatch, known={"audience": None})


# ── ages only in client texts ──

def test_adult_summary_shows_age_not_birth_year():
    draft = {**_ADULT_DRAFT, "trial_day": "2026-11-21", "start_time": "12:00"}

    summary = llm_trial_flow._adult_confirmation(draft, "ru")

    assert f"Возраст: {_ADULT_AGE}" in summary
    assert str(_ADULT_DRAFT["child_birth_year"]) not in summary


# ── live evals: the real model, opt-in ──

_live = pytest.mark.skipif(os.getenv("RUN_LLM_EVALS") != "1", reason="set RUN_LLM_EVALS=1 to call the real LLM")
# Checked in the model's output only — the bot itself never decides by words.
_CHILD_WORDS = ("ребён", "ребен", "детск", "школ", "садик", "родител", "бала", "мектеп", "ата-ана")


@_live
@pytest.mark.parametrize("lang", ["ru", "kk"])
def test_live_adult_question_with_stale_history_never_mentions_a_child(monkeypatch, lang):
    monkeypatch.setattr(llm, "get_question_reply", _REAL_GET_QUESTION_REPLY)
    template = "Как вас зовут?" if lang == "ru" else "Есіміңіз кім?"

    reply = llm.get_question_reply(lang, template, _STALE_HISTORY, actor="self", audience="adult").lower()

    assert not any(word in reply for word in _CHILD_WORDS), reply


@_live
def test_live_adult_friday_question_offers_no_child_times():
    import config

    boxing_id = next((pid for pid, bot in config.BOT_CONFIGS.items() if bot["name"] == "dopsy_boxing"), None)
    if boxing_id is None:
        pytest.skip("dopsy_boxing is not configured")
    # The schedule the side-question LLM gets before the age is saved: child
    # groups only (the backend fix for that is separate).
    child_schedule = "Свободные пробные: Пт 17:00–18:00 (детская группа), Пт 18:30–19:30 (детская группа)."
    state = LlmTrialFlowHandler().draft_state_context("dopsy_boxing", _ADULT_DRAFT)

    reply, _ = llm.get_ai_response(
        boxing_id, "eval-chat", "В пятницу есть тренировка?", list(_STALE_HISTORY),
        f"{child_schedule}\n\n{state}",
    )

    assert "17:00" not in reply and "18:30" not in reply, reply
    assert not any(word in reply.lower() for word in _CHILD_WORDS), reply
