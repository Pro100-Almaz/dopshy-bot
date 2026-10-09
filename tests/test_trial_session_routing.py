"""The academy intent router used mid-signup."""
import json
from types import SimpleNamespace

import pytest

from chat import llm

pytestmark = pytest.mark.no_db


def test_router_gets_the_pending_step_and_can_fail_to_none(monkeypatch):
    seen = {}

    def failing_create(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("timeout")

    monkeypatch.setattr(llm._client.chat.completions, "create", failing_create)

    assert llm.route_trial_message([], "2", pending="Выберите группу", fallback=None) == (None, "ru")
    assert "Выберите группу" in seen["messages"][0]["content"]


def test_router_passes_intent_through(monkeypatch):
    completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[
        SimpleNamespace(function=SimpleNamespace(
            arguments=json.dumps({"type": "question_schedule", "lang": "ru"})))
    ]))])
    monkeypatch.setattr(llm._client.chat.completions, "create", lambda **kw: completion)

    assert llm.route_trial_message([], "расписание Асана?", pending="x", fallback=None) == (
        "question_schedule", "ru")
