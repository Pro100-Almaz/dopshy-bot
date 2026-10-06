from pathlib import Path

import pytest

from rag import retriever
from rag.vector_store import _document_scope

pytestmark = pytest.mark.no_db


def test_document_scope_from_filename():
    assert _document_scope(Path("academy_football_faq.md")) == "academy_football"
    assert _document_scope(Path("academy_boxing_faq.md")) == "academy_boxing"
    assert _document_scope(Path("academy_shared_trial_policy.md")) == "academy_shared"
    assert _document_scope(Path("05_faq_and_short_answers.md")) == "arena"


def test_scope_filter_by_bot_name():
    assert retriever._scope_filter("dopsy_bot") == {"scope": "arena"}
    assert retriever._scope_filter("dopsy_fs_school") == {
        "$or": [{"scope": "academy_football"}, {"scope": "academy_shared"}]
    }
    assert retriever._scope_filter("dopsy_boxing") == {
        "$or": [{"scope": "academy_boxing"}, {"scope": "academy_shared"}]
    }
    assert retriever._scope_filter("unknown") is None


def _write_docs(tmp_path):
    for name in (
        "academy_boxing_faq.md",
        "academy_shared_trial_policy.md",
        "academy_football_faq.md",
        "05_faq_and_short_answers.md",
    ):
        (tmp_path / name).write_text(f"content of {name}", encoding="utf-8")


def test_boxing_bot_gets_full_boxing_and_shared_docs(tmp_path, monkeypatch):
    _write_docs(tmp_path)
    monkeypatch.setattr(retriever.config, "DOCUMENTS_PATH", str(tmp_path))
    monkeypatch.setattr(
        retriever, "_load_store",
        lambda: (_ for _ in ()).throw(AssertionError("vector store must not be used")),
    )

    context = retriever.retrieve_context("сколько стоит?", bot_name="dopsy_boxing")

    assert "content of academy_boxing_faq.md" in context
    assert "content of academy_shared_trial_policy.md" in context
    assert "academy_football_faq.md" not in context
    assert "05_faq_and_short_answers.md" not in context


def test_other_bots_still_use_vector_search(tmp_path, monkeypatch):
    _write_docs(tmp_path)
    monkeypatch.setattr(retriever.config, "DOCUMENTS_PATH", str(tmp_path))
    calls = []

    class FakeStore:
        def similarity_search(self, query, k, filter):
            calls.append(filter)
            return []

    monkeypatch.setattr(retriever, "_load_store", lambda: FakeStore())

    assert retriever.retrieve_context("q", bot_name="dopsy_bot") == ""
    assert retriever.retrieve_context("q", bot_name="dopsy_fs_school") == ""
    assert calls == [
        retriever._scope_filter("dopsy_bot"),
        retriever._scope_filter("dopsy_fs_school"),
    ]


class _BrokenFilterStore:
    def __init__(self):
        self.calls = []

    def similarity_search(self, query, k, filter=None):
        self.calls.append(filter)
        if filter is not None:
            raise ValueError("filter not supported")
        return []


def test_academy_bot_never_falls_back_to_unscoped_search(monkeypatch):
    store = _BrokenFilterStore()
    monkeypatch.setattr(retriever, "_load_store", lambda: store)

    with pytest.raises(ValueError):
        retriever.retrieve_context("q", bot_name="dopsy_fs_school")
    assert store.calls == [retriever._scope_filter("dopsy_fs_school")]


def test_arena_bot_keeps_unscoped_fallback(monkeypatch):
    store = _BrokenFilterStore()
    monkeypatch.setattr(retriever, "_load_store", lambda: store)

    assert retriever.retrieve_context("q", bot_name="dopsy_bot") == ""
    assert store.calls == [retriever._scope_filter("dopsy_bot"), None]


def test_voucher_docs_get_their_own_scope():
    assert _document_scope(Path("academy_football_voucher_transfer_steps.md")) == "academy_voucher"
    # ...which no bot's similarity-search filter includes.
    for bot in ("dopsy_bot", "dopsy_fs_school", "dopsy_boxing"):
        assert "academy_voucher" not in str(retriever._scope_filter(bot))


def test_intent_context_loads_voucher_doc_only_for_football_voucher(tmp_path, monkeypatch):
    (tmp_path / "academy_football_voucher_transfer_steps.md").write_text(
        "voucher steps", encoding="utf-8"
    )
    monkeypatch.setattr(retriever.config, "DOCUMENTS_PATH", str(tmp_path))

    assert retriever.intent_context("dopsy_fs_school", "question_voucher") == (
        "[academy_football_voucher_transfer_steps.md]\nvoucher steps"
    )
    assert retriever.intent_context("dopsy_boxing", "question_voucher") is None
    assert retriever.intent_context("dopsy_fs_school", "question_price") is None


def test_voucher_doc_exists_in_documents():
    name = retriever._INTENT_DOCS[("dopsy_fs_school", "question_voucher")]
    assert (Path(__file__).parent.parent / "documents" / name).is_file()
