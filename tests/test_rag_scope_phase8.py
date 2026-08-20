from pathlib import Path

from rag import retriever
from rag.vector_store import _document_scope


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
