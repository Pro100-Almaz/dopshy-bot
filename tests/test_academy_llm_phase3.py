import json
from types import SimpleNamespace

import pytest

from chat import llm
from handlers import academy_extractor

pytestmark = pytest.mark.no_db


def _completion(args: dict | None):
    tool_calls = []
    if args is not None:
        tool_calls = [
            SimpleNamespace(
                function=SimpleNamespace(arguments=json.dumps(args))
            )
        ]
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(tool_calls=tool_calls)
            )
        ]
    )


def test_route_trial_message_parses_intent_and_language(monkeypatch):
    seen = {}

    def fake_create(**kwargs):
        seen.update(kwargs)
        return _completion({"type": "trial_new", "lang": "kk"})

    monkeypatch.setattr(llm._client.chat.completions, "create", fake_create)

    intent, lang = llm.route_trial_message([], "баламды сынақ сабаққа жазғым келеді")

    assert (intent, lang) == ("trial_new", "kk")
    assert seen["tool_choice"]["function"]["name"] == "route_trial_message"


def test_route_trial_message_falls_back_on_missing_tool_call(monkeypatch):
    monkeypatch.setattr(
        llm._client.chat.completions,
        "create",
        lambda **kwargs: _completion(None),
    )

    assert llm.route_trial_message([], "hello") == ("other", "ru")


def test_extract_trial_details_parses_all_supported_fields(monkeypatch):
    payload = {
        "child_name": "Али",
        "child_birth_year": 2016,
        "audience": "child",
        "experience": "Beginner",
        "school_shift": "morning",
        "preferred_date": "2026-08-21",
        "preferred_weekday": 4,
        "preferred_time_start": "18:00",
        "preferred_time_end": "19:30",
        "preferred_weekdays": [4],
        "preferred_trainers": None,
    }
    seen = {}

    def fake_create(**kwargs):
        seen.update(kwargs)
        return _completion(payload)

    monkeypatch.setattr(academy_extractor.client.chat.completions, "create", fake_create)

    result = academy_extractor.extract_trial_details(
        [], "Али 2016 beginner, утром учится, хотим в пятницу 18:00"
    )

    assert result == payload
    assert seen["tool_choice"]["function"]["name"] == "extract_trial_data"
    prompt = seen["messages"][0]["content"]
    assert "Твоя задача — извлечение, а не вывод информации" in prompt
    assert "категорически запрещено выводить данные из предыдущих пробных занятий" in prompt
    assert "{today}" not in prompt
    assert "{now}" not in prompt


def test_extract_trial_details_merges_missing_keys_with_empty_template(monkeypatch):
    monkeypatch.setattr(
        academy_extractor.client.chat.completions,
        "create",
        lambda **kwargs: _completion({"child_birth_year": 2014}),
    )

    result = academy_extractor.extract_trial_details([], "2014")

    assert result["child_birth_year"] == 2014
    assert result["child_name"] is None
    assert result["preferred_time_start"] is None


def test_the_extractor_is_told_the_saved_wish(monkeypatch):
    seen = {}

    def fake_create(**kwargs):
        seen.update(kwargs)
        return _completion({})

    monkeypatch.setattr(academy_extractor.client.chat.completions, "create", fake_create)

    academy_extractor.extract_trial_details(
        [], "Тимурдың уақыттары?", "slot", {"preferred_time_start": "18:00", "preferred_trainers": None},
    )

    prompt = seen["messages"][0]["content"]
    assert '{"preferred_time_start": "18:00"}' in prompt and "preferred_trainers\": null" not in prompt


def test_a_failed_extraction_is_told_from_an_empty_one(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("openai down")

    monkeypatch.setattr(academy_extractor.client.chat.completions, "create", boom)

    result = academy_extractor.extract_trial_details([], "Алихан")

    assert result["failed"] is True and result["child_name"] is None


def test_extract_trial_details_is_told_the_step_being_answered(monkeypatch):
    seen = {}

    def fake_create(**kwargs):
        seen.update(kwargs)
        return _completion({"child_name": "Әсет"})

    monkeypatch.setattr(academy_extractor.client.chat.completions, "create", fake_create)

    academy_extractor.extract_trial_details([], "Есімім Әсет", "child_name")

    assert "Бот сейчас спросил имя ученика" in seen["messages"][0]["content"]
