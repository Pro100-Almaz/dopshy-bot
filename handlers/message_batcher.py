"""Debounce inbound WhatsApp messages per sender, backed by Redis.

People rarely send a request as one message — they split it: "Hello",
"I want to make a booking", "for tomorrow". Handling each fragment separately
makes the bot reply three times and reason on incomplete input (it asks for the
date on message 2 even though message 3 supplies it).

This module buffers a sender's *text* messages and only forwards them to
`handle_incoming_message` once the sender has been quiet for
`config.MESSAGE_BATCH_WINDOW_SECONDS`. Every new message resets the window
(debounce), so a burst of fragments collapses into a single combined message.

Why Redis: gunicorn runs multiple workers, and a sender's fragments can be
load-balanced across different workers. An in-process dict would split the
buffer per worker and defeat the batching. The buffer therefore lives in Redis,
shared by all workers.

How the debounce stays correct across workers:
  - Each message does INCR on a per-conversation sequence counter and gets a
    unique token, then appends its text to a Redis list.
  - The worker that received the message schedules a LOCAL threading.Timer for
    the window, carrying that token.
  - When a timer fires, an atomic Lua script drains the buffer *only if* the
    counter still equals the timer's token — i.e. no newer message arrived. Any
    later message bumps the counter, invalidating earlier timers, so exactly the
    last message's timer wins. The Lua DEL makes the drain race-free.

Non-text messages (documents / payment receipts, interactive button taps) are
NOT batched — they are discrete actions. Any pending text is flushed first so
ordering is preserved, then the non-text message is handled immediately.

If Redis is unreachable, we fail open: the message is handled immediately,
unbatched, so the bot keeps working (it just replies per-fragment as before).
"""
import dataclasses
import logging
import pickle
import threading

import redis

import config
from handlers.message_handler import handle_incoming_message
from integrations.providers.payload import IncomingWhatsAppMessage

logger = logging.getLogger(__name__)

_KEY_PREFIX = "batch"
# Self-cleaning safety net: if a worker dies before its timer fires, the keys
# expire on their own rather than leaking. Comfortably longer than the window.
_KEY_TTL_SECONDS = int(config.MESSAGE_BATCH_WINDOW_SECONDS) + 60

# Drain the buffer ONLY if the sequence counter still equals the caller's token
# (no newer message arrived). Returns [pickled_template, text, text, ...] or [].
_DRAIN_IF_CURRENT = """
local cur = redis.call('GET', KEYS[1])
if cur == ARGV[1] then
  local tpl = redis.call('GET', KEYS[3])
  local msgs = redis.call('LRANGE', KEYS[2], 0, -1)
  redis.call('DEL', KEYS[1], KEYS[2], KEYS[3])
  if tpl then table.insert(msgs, 1, tpl) end
  return msgs
end
return {}
"""

# Drain unconditionally (used when a non-text message forces a flush).
_DRAIN_NOW = """
local tpl = redis.call('GET', KEYS[3])
local msgs = redis.call('LRANGE', KEYS[2], 0, -1)
redis.call('DEL', KEYS[1], KEYS[2], KEYS[3])
if tpl then table.insert(msgs, 1, tpl) end
return msgs
"""

_client: redis.Redis | None = None
_drain_if_current = None
_drain_now = None
_client_lock = threading.Lock()


def _redis() -> redis.Redis:
    """Lazily build a shared client. Short timeouts so a Redis outage never
    hangs the webhook thread — it raises and we fall back to immediate handling."""
    global _client, _drain_if_current, _drain_now
    if _client is None:
        with _client_lock:
            if _client is None:
                client = redis.Redis.from_url(
                    config.REDIS_URL,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                    # Values are a mix of pickled bytes and UTF-8 text — decode
                    # manually, never globally.
                    decode_responses=False,
                )
                _drain_if_current = client.register_script(_DRAIN_IF_CURRENT)
                _drain_now = client.register_script(_DRAIN_NOW)
                _client = client
    return _client


def _keys(key: str) -> tuple[str, str, str]:
    """(seq, buffer, template) Redis keys for a conversation."""
    return (
        f"{_KEY_PREFIX}:seq:{key}",
        f"{_KEY_PREFIX}:buf:{key}",
        f"{_KEY_PREFIX}:tpl:{key}",
    )


def _conversation_key(payload: IncomingWhatsAppMessage) -> str:
    """Stable per-sender, per-bot key. Mirrors phone_number_id resolution in
    handle_incoming_message so batching lines up with how messages are routed."""
    if payload.provider == "ycloud":
        phone_number_id = config.WHATSAPP_PHONE_NUMBER_ID_BOT_1
    else:
        phone_number_id = payload.business.phone_number_id
    sender = payload.customer.phone if payload.customer else None
    return f"{phone_number_id}:{sender}"


def enqueue_incoming_message(payload: IncomingWhatsAppMessage) -> None:
    """Entry point for inbound messages. Non-blocking.

    Text → buffered in Redis and (re)scheduled for a delayed combined flush.
    Anything else → pending text flushed first, then handled right away.
    """
    if payload.message_type != "text" or not payload.text:
        _handle_non_text(payload)
        return

    key = _conversation_key(payload)
    seq_key, buf_key, tpl_key = _keys(key)

    try:
        r = _redis()
        token = r.incr(seq_key)
        pipe = r.pipeline()
        pipe.rpush(buf_key, payload.text)
        pipe.set(tpl_key, pickle.dumps(payload))
        pipe.expire(seq_key, _KEY_TTL_SECONDS)
        pipe.expire(buf_key, _KEY_TTL_SECONDS)
        pipe.expire(tpl_key, _KEY_TTL_SECONDS)
        pipe.execute()
    except Exception:
        # Redis down / misconfigured — fail open: process this message alone.
        logger.exception("[BATCH] Redis unavailable — handling message unbatched")
        _dispatch_one(payload)
        return

    timer = threading.Timer(
        config.MESSAGE_BATCH_WINDOW_SECONDS, _flush_if_current, args=(key, token),
    )
    timer.daemon = True
    timer.start()

    logger.info(
        "[BATCH] buffered message for %s (seq=%d), window=%.1fs",
        key, token, config.MESSAGE_BATCH_WINDOW_SECONDS,
    )


def _handle_non_text(payload: IncomingWhatsAppMessage) -> None:
    """Flush any buffered text for this sender, then process the non-text
    message. Runs in a background thread so the caller (webhook) never blocks."""
    key = _conversation_key(payload)
    seq_key, buf_key, tpl_key = _keys(key)

    def _run():
        try:
            # Ensure the client and Lua scripts are registered. A non-text
            # message can be the first thing a worker handles, in which case
            # enqueue_incoming_message() never ran and _drain_now is still None.
            _redis()
            raw = _drain_now(keys=[seq_key, buf_key, tpl_key])
            _dispatch_drained(raw)
        except Exception:
            logger.exception("[BATCH] failed flushing pending text for %s", key)
        try:
            handle_incoming_message(payload)
        except Exception:
            logger.exception("[BATCH] handler failed for non-text message %s", key)

    threading.Thread(target=_run, daemon=True).start()


def _flush_if_current(key: str, token: int) -> None:
    """Timer callback. Drains and dispatches only if no newer message arrived."""
    seq_key, buf_key, tpl_key = _keys(key)
    try:
        raw = _drain_if_current(keys=[seq_key, buf_key, tpl_key], args=[token])
    except Exception:
        logger.exception("[BATCH] drain failed for %s", key)
        return
    _dispatch_drained(raw)


def _dispatch_drained(raw: list) -> None:
    """Rebuild the combined payload from a Lua drain result and run the pipeline
    once. `raw` is [pickled_template, text_bytes, ...] or empty."""
    if not raw:
        return

    template = pickle.loads(raw[0])
    texts = [b.decode("utf-8") for b in raw[1:]]
    if not texts:
        return

    combined_text = "\n".join(texts)
    # Use the most recent payload as the template so mark_as_read targets the
    # last message id; only the text is replaced with the combined body.
    combined = dataclasses.replace(template, text=combined_text)

    if len(texts) > 1:
        logger.info(
            "[BATCH] combined %d messages → %.120s",
            len(texts), combined_text.replace("\n", " ⏎ "),
        )

    _dispatch_one(combined)


def _dispatch_one(payload: IncomingWhatsAppMessage) -> None:
    try:
        handle_incoming_message(payload)
    except Exception:
        logger.exception("[BATCH] handler failed for combined message")
