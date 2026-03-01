"""ChromaDB vector store — document loading and indexing."""

import os
import glob
from pathlib import Path

import chromadb
from chromadb.config import Settings
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from langchain_community.document_loaders import TextLoader, DirectoryLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter

import config


def _get_embeddings() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=config.EMBEDDING_MODEL,
        openai_api_key=config.OPENAI_API_KEY,
    )


def _get_chroma_client() -> chromadb.PersistentClient:
    return chromadb.PersistentClient(
        path=config.CHROMA_DB_PATH,
        settings=Settings(anonymized_telemetry=False),
    )


def get_vector_store() -> Chroma:
    """Return the persistent Chroma vector store (read-only for queries)."""
    return Chroma(
        collection_name=config.CHROMA_COLLECTION_NAME,
        embedding_function=_get_embeddings(),
        persist_directory=config.CHROMA_DB_PATH,
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

    # Build Chroma store (creates or overwrites the collection)
    client = _get_chroma_client()
    # Delete existing collection to avoid duplicates on re-ingest
    try:
        client.delete_collection(config.CHROMA_COLLECTION_NAME)
    except Exception:
        pass

    Chroma.from_documents(
        documents=chunks,
        embedding=_get_embeddings(),
        collection_name=config.CHROMA_COLLECTION_NAME,
        persist_directory=config.CHROMA_DB_PATH,
        client_settings=Settings(anonymized_telemetry=False),
    )

    print(f"Ingested {len(chunks)} chunks from {len(files)} files.")
    return len(chunks)
