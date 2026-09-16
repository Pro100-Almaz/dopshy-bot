import json
from types import SimpleNamespace

from chat import llm
from handlers import academy_extractor


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
        "experience": "Beginner",
        "school_shift": "morning",
        "preferred_date": "2026-08-21",
        "preferred_weekday": 4,
        "preferred_time_start": "18:00",
        "preferred_time_end": "19:30",
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
