"""A signup untouched for 120 minutes is cancelled; its answers are kept as a
summary and prefill the client's next signup."""
from datetime import datetime, timedelta, timezone

import pytest

from handlers import llm_trial_flow
from integrations import trial_service
from tests.test_trial_intake_order import flow  # noqa: F401

pytestmark = pytest.mark.no_db


def _draft(minutes_idle):
    return {"id": 3, "updated_at": datetime.now(timezone.utc) - timedelta(minutes=minutes_idle)}


@pytest.mark.parametrize("minutes, in_progress, abandoned", [
    (10, True, False), (31, False, False), (119, False, False), (121, False, True),
])
def test_windows(minutes, in_progress, abandoned):
    assert llm_trial_flow.is_draft_in_progress(_draft(minutes)) is in_progress
    assert llm_trial_flow.is_draft_abandoned(_draft(minutes)) is abandoned


def test_abandon_cancels_and_keeps_answers(monkeypatch):
    calls = []
    monkeypatch.setattr(trial_service, "cancel_trial", lambda *a: calls.append(("cancel", a[3])))
    monkeypatch.setattr(trial_service.academy_repo, "save_trial_summary",
                        lambda phone, bot, answers: calls.append(("save", answers["child_name"])))

    trial_service.abandon_draft("dopsy_boxing", "chat", {"id": 3, "phone": "7700", "child_name": "Айдос"})

    assert calls == [("cancel", "abandoned_120m"), ("save", "Айдос")]


def test_abandon_with_no_answers_saves_nothing(monkeypatch):
    monkeypatch.setattr(trial_service, "cancel_trial", lambda *a: None)
    monkeypatch.setattr(trial_service.academy_repo, "save_trial_summary",
                        lambda *a: pytest.fail("nothing to keep"))

    trial_service.abandon_draft("dopsy_boxing", "chat", {"id": 3, "phone": "7700", "audience": "child"})


def test_new_signup_is_prefilled_from_the_summary(flow, monkeypatch):
    run, state = flow
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_trial_summary", lambda phone, bot: {
        "audience": "child", "child_name": "Айдос", "child_birth_year": 2016,
        "experience": "Beginner", "school_shift": None})

    reply = run("Сынақ сабағына жазылғым келеді")

    assert state["draft"]["child_name"] == "Айдос" and state["draft"]["child_birth_year"] == 2016
    assert "Алдыңғы жауаптарыңызды сақтап қойдым: Айдос, 2016 ж.т., Бастапқы" in reply
    assert reply.endswith(llm_trial_flow.BOXING_T["ask_school_shift"]["kk"])


def test_abandoned_draft_in_the_flow_is_replaced(flow, monkeypatch):
    run, state = flow
    abandoned = []
    state["draft"] = {**_draft(180), "phone": "7700", "child_name": "Айдос"}
    monkeypatch.setattr(llm_trial_flow.trial_service, "abandon_draft",
                        lambda bot, chat, draft: (abandoned.append(draft["id"]), state.update(draft=None)))
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_trial_summary", lambda phone, bot: None)

    reply = run("Сынақ сабағына жазылғым келеді")

    assert abandoned == [3]
    assert "тегін" in reply
