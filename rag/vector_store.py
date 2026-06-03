"""ChromaDB vector store — document loading and indexing."""

from pathlib import Path

import chromadb
from chromadb.config import Settings
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from langchain_community.document_loaders import TextLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter

import config


def _get_embeddings() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=config.EMBEDDING_MODEL,
        openai_api_key=config.OPENAI_API_KEY,
    )


_chroma_client = None


def _get_chroma_client() -> chromadb.PersistentClient:
    """Return a module-level singleton PersistentClient.

    Both get_vector_store() and ingest_documents() must share the same client
    instance — ChromaDB raises ValueError if two clients are created for the
    same path with different settings objects.
    """
    global _chroma_client
    if _chroma_client is None:
        _chroma_client = chromadb.PersistentClient(
            path=config.CHROMA_DB_PATH,
            settings=Settings(anonymized_telemetry=False),
        )
    return _chroma_client


def get_vector_store() -> Chroma:
    """Return the persistent Chroma vector store (read-only for queries)."""
    return Chroma(
        collection_name=config.CHROMA_COLLECTION_NAME,
        embedding_function=_get_embeddings(),
        client=_get_chroma_client(),
    )


def ingest_documents(documents_path: str = config.DOCUMENTS_PATH) -> int:
    """
    Load all .md and .txt files from documents_path, split them into chunks,
    and upsert into ChromaDB. Returns the number of chunks added.
    """
    path = Path(documents_path)
    if not path.exists():
        raise FileNotFoundError(f"Documents directory not found: {documents_path}")

    # Collect all markdown and text files
    files = list(path.glob("**/*.md")) + list(path.glob("**/*.txt"))
    if not files:
        raise ValueError(f"No .md or .txt files found in {documents_path}")

    docs = []
    for file_path in files:
        loader = TextLoader(str(file_path), encoding="utf-8")
        loaded = loader.load()
        # Tag each document with its source filename
        for doc in loaded:
            doc.metadata["source"] = file_path.name
        docs.extend(loaded)

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.CHUNK_SIZE,
        chunk_overlap=config.CHUNK_OVERLAP,
        separators=["\n\n", "\n", ".", " ", ""],
    )
    chunks = splitter.split_documents(docs)

    # Drop and recreate the collection to avoid duplicates on re-ingest
    client = _get_chroma_client()
    try:
        client.delete_collection(config.CHROMA_COLLECTION_NAME)
    except Exception:
        pass

    Chroma.from_documents(
        documents=chunks,
        embedding=_get_embeddings(),
        collection_name=config.CHROMA_COLLECTION_NAME,
        client=client,
    )

    print(f"Ingested {len(chunks)} chunks from {len(files)} files.")
    return len(chunks)
