import pytest

from handlers import llm_trial_flow
from handlers.llm_trial_flow import BOXING_T, T, LlmTrialFlowHandler

pytestmark = pytest.mark.no_db


def _ask_name_via_flow(monkeypatch, bot_name):
    return LlmTrialFlowHandler()._evaluate_and_respond("chat-1", bot_name, {"id": 1}, "ru")


def test_boxing_bot_uses_friendly_texts(monkeypatch):
    assert _ask_name_via_flow(monkeypatch, "dopsy_boxing") == BOXING_T["ask_name"]["ru"]


def test_football_bot_keeps_original_texts(monkeypatch):
    assert _ask_name_via_flow(monkeypatch, "dopsy_fs_school") == T["ask_name"]["ru"]


def test_bot_scope_is_reset_after_the_call(monkeypatch):
    _ask_name_via_flow(monkeypatch, "dopsy_boxing")
    assert llm_trial_flow._loc("ru", "ask_name") == T["ask_name"]["ru"]


def test_boxing_texts_keep_placeholders_and_options():
    for key, entry in BOXING_T.items():
        for lang in ("ru", "kk"):
            original, friendly = T[key][lang], entry[lang]
            for placeholder in ("{year}", "{min_age}", "{max_age}", "{max_len}", "{requested}",
                                "{offered}", "{date}", "{start}", "{end}", "{name}",
                                "{birth_year}", "{experience}", "{school_shift}"):
                assert (placeholder in original) == (placeholder in friendly), (key, lang, placeholder)
            for option in ("1. ", "2. ", "3. ", "«моя запись»", "«перенести запись»"):
                assert (option in original) == (option in friendly), (key, lang, option)
