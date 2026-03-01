"""
Persistent conversation history — in-memory cache backed by SQLite.

Strategy:
  - Write-through: every mutation is immediately written to SQLite.
  - Lazy load: a chat's history is fetched from DB on its first access
    within a process lifetime, then kept in memory for fast reads.
  - Thread-safe: a single lock guards both the cache and DB writes,
    since Flask processes multiple webhook requests concurrently.

SQLite is used over JSON files because it handles concurrent writes
safely and requires no extra dependencies.
"""

import json
import os
import sqlite3
import threading
from typing import TypedDict

import config


class Message(TypedDict):
    role: str       # "user" | "assistant"
    content: str


_lock = threading.Lock()
_cache: dict[str, list[Message]] = {}   # chat_id -> messages (hot cache)
_loaded: set[str] = set()               # chat_ids already fetched from DB


# ---------------------------------------------------------------------------
# DB bootstrap — runs once when the module is first imported
# ---------------------------------------------------------------------------

def _init_db() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(config.CONVERSATION_DB_PATH)), exist_ok=True)
    with sqlite3.connect(config.CONVERSATION_DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")   # better concurrent-write performance
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                chat_id    TEXT PRIMARY KEY,
                messages   TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)


_init_db()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_from_db(chat_id: str) -> list[Message]:
    with sqlite3.connect(config.CONVERSATION_DB_PATH) as conn:
        row = conn.execute(
            "SELECT messages FROM conversations WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return json.loads(row[0]) if row else []


def _save_to_db(chat_id: str, messages: list[Message]) -> None:
    with sqlite3.connect(config.CONVERSATION_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO conversations (chat_id, messages, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(chat_id) DO UPDATE SET
                messages   = excluded.messages,
                updated_at = excluded.updated_at
            """,
            (chat_id, json.dumps(messages, ensure_ascii=False)),
        )


def _ensure_loaded(chat_id: str) -> None:
    """Populate the cache from DB on the first access for this chat_id."""
    if chat_id not in _loaded:
        _cache[chat_id] = _load_from_db(chat_id)
        _loaded.add(chat_id)


# ---------------------------------------------------------------------------
# Public API (same interface as before — no changes needed elsewhere)
# ---------------------------------------------------------------------------

def append_message(chat_id: str, role: str, content: str) -> None:
    """Append a message and persist the updated history to SQLite."""
    with _lock:
        _ensure_loaded(chat_id)
        _cache[chat_id].append({"role": role, "content": content})
        if len(_cache[chat_id]) > config.MAX_HISTORY_MESSAGES:
            _cache[chat_id] = _cache[chat_id][-config.MAX_HISTORY_MESSAGES:]
        _save_to_db(chat_id, _cache[chat_id])


def get_history(chat_id: str) -> list[Message]:
    """Return the conversation history, loading from DB if needed."""
    with _lock:
        _ensure_loaded(chat_id)
        return list(_cache[chat_id])


def clear_history(chat_id: str) -> None:
    """Delete all messages for a chat (e.g. user sends /reset)."""
    with _lock:
        _cache[chat_id] = []
        _loaded.add(chat_id)
        _save_to_db(chat_id, [])
