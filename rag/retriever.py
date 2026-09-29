"""RAG retriever — fetches relevant document chunks for a user query."""

from functools import lru_cache
from pathlib import Path

from langchain_chroma import Chroma

import config
from integrations import test_context
from rag.vector_store import get_vector_store


@lru_cache(maxsize=1)
def _load_store() -> Chroma:
    """Load vector store once and cache it for the lifetime of the process."""
    return get_vector_store()


def _scope_filter(bot_name: str | None) -> dict | None:
    if bot_name == "dopsy_bot":
        return {"scope": "arena"}
    if bot_name == "dopsy_fs_school":
        return {"$or": [{"scope": "academy_football"}, {"scope": "academy_shared"}]}
    if bot_name == "dopsy_boxing":
        return {"$or": [{"scope": "academy_boxing"}, {"scope": "academy_shared"}]}
    return None


# Bots whose knowledge base is small enough to send in full instead of
# running a similarity search. Files are read from disk on every call, so
# edits to documents/ take effect without re-ingesting or restarting.
_FULL_CONTEXT_PREFIXES: dict[str, tuple[str, ...]] = {
    "dopsy_boxing": ("academy_boxing_", "academy_shared_"),
}


def _full_context_docs(bot_name: str) -> list[tuple[str, str]]:
    prefixes = _FULL_CONTEXT_PREFIXES[bot_name]
    files = sorted(
        p for p in Path(config.DOCUMENTS_PATH).glob("*.md")
        if p.name.lower().startswith(prefixes)
    )
    return [(p.name, p.read_text(encoding="utf-8").strip()) for p in files]


def retrieve_context(
    query: str,
    k: int = config.TOP_K_RESULTS,
    bot_name: str | None = None,
) -> str:
    """
    Retrieve the top-k most relevant document chunks for a query.
    Returns a single formatted string to inject into the system prompt.
    """
    if bot_name in _FULL_CONTEXT_PREFIXES:
        docs = _full_context_docs(bot_name)
        test_context.record(
            "rag",
            query=query,
            bot_name=bot_name,
            mode="full",
            chunks=[{"source": name, "scope": "full", "text": text} for name, text in docs],
        )
        return "\n\n".join(f"[{name}]\n{text}" for name, text in docs)

    store = _load_store()
    filter_ = _scope_filter(bot_name)
    try:
        results = store.similarity_search(query, k=k, filter=filter_)
    except Exception:
        if filter_:
            results = store.similarity_search(query, k=k)
        else:
            raise

    test_context.record(
        "rag",
        query=query,
        k=k,
        bot_name=bot_name,
        filter=filter_,
        chunks=[
            {
                "source": d.metadata.get("source"),
                "scope": d.metadata.get("scope"),
                "text": d.page_content,
            }
            for d in results
        ],
    )

    if not results:
        return ""

    parts = []
    for i, doc in enumerate(results, 1):
        source = doc.metadata.get("source", "unknown")
        scope = doc.metadata.get("scope", "unknown")
        parts.append(f"[{i}] ({source}; {scope})\n{doc.page_content.strip()}")

    return "\n\n".join(parts)

def invalidate_cache() -> None:
    """Call this after re-ingesting documents to reload the store."""
    _load_store.cache_clear()
