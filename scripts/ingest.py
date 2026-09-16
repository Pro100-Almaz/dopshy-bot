"""
One-time script to load all documents from /documents into ChromaDB.
Run this before starting the Flask app for the first time, and
whenever you update the knowledge base files.

Usage:
    python scripts/ingest.py
"""

import sys
import os

# Allow imports from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rag.retriever import invalidate_cache
from rag.vector_store import ingest_documents
import config


def main():
    print(f"Documents path : {config.DOCUMENTS_PATH}")
    print(f"ChromaDB path  : {config.CHROMA_DB_PATH}")
    print(f"Collection     : {config.CHROMA_COLLECTION_NAME}")
    print("-" * 40)

    count = ingest_documents(config.DOCUMENTS_PATH)
    invalidate_cache()
    print(f"\nDone. {count} chunks stored in ChromaDB.")


if __name__ == "__main__":
    main()
