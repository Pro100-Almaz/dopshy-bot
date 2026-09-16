"""RAG retriever — fetches relevant document chunks for a user query."""

from functools import lru_cache
from langchain_chroma import Chroma

import config
from rag.vector_store import get_vector_store


@lru_cache(maxsize=1)
def _load_store() -> Chroma:
    """Load vector store once and cache it for the lifetime of the process."""
    return get_vector_store()


def retrieve_context(query: str, k: int = config.TOP_K_RESULTS) -> str:
    """
    Retrieve the top-k most relevant document chunks for a query.
    Returns a single formatted string to inject into the system prompt.
    """
    store = _load_store()
    results = store.similarity_search(query, k=k)

    if not results:
        return ""

    parts = []
    for i, doc in enumerate(results, 1):
        source = doc.metadata.get("source", "unknown")
        parts.append(f"[{i}] ({source})\n{doc.page_content.strip()}")

    return "\n\n".join(parts)

def invalidate_cache() -> None:
    """Call this after re-ingesting documents to reload the store."""
    _load_store.cache_clear()
