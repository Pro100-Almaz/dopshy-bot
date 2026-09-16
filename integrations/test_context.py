"""Ambient console-test context.

The admin agent-test console runs the REAL pipeline
(`handlers.message_handler.handle_incoming_message`) but must not touch
production: no WhatsApp delivery, no Google Sheets rows, no client
notifications, no payment invoices, and no slot reservations that would block
a real customer.

Threading an `is_test` argument through every flow function would touch ~15
call sites and rot on the next refactor, and monkeypatching is unsafe in a
live process that serves real webhooks concurrently. So the flag is ambient,
carried by `contextvars` and read at the few seams that actually cause a side
effect.

Three values travel together for the duration of one console turn:

  * the sandbox flag   — read by repo/integration guards
  * the outbox         — replies captured instead of being sent
  * the trace          — debug events (RAG, routing, prompt, tokens, latency)

`record()` and `capture_reply()` are cheap no-ops outside console mode, so the
instrumentation added to production paths costs a single ContextVar lookup.
"""

import contextvars
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

_test_mode: contextvars.ContextVar[bool] = contextvars.ContextVar("agent_test_mode", default=False)
_outbox: contextvars.ContextVar[list | None] = contextvars.ContextVar("agent_test_outbox", default=None)
_trace: contextvars.ContextVar[list | None] = contextvars.ContextVar("agent_test_trace", default=None)
_phone: contextvars.ContextVar[str | None] = contextvars.ContextVar("agent_test_phone", default=None)


def is_test_mode() -> bool:
    """True while a console turn is being processed on this context."""
    return _test_mode.get()


def test_phone() -> str | None:
    """The synthetic sender of the console turn in flight, if any."""
    return _phone.get()


@contextmanager
def console_session(phone: str | None = None) -> Iterator[tuple[list, list]]:
    """Run a block in sandbox mode, collecting captured replies and trace events."""
    outbox: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    tokens = (_test_mode.set(True), _outbox.set(outbox),
              _trace.set(trace), _phone.set(phone))
    try:
        yield outbox, trace
    finally:
        _phone.reset(tokens[3])
        _trace.reset(tokens[2])
        _outbox.reset(tokens[1])
        _test_mode.reset(tokens[0])


def capture_reply(to: str, text: str) -> None:
    """Record an outbound message that was suppressed instead of delivered."""
    box = _outbox.get()
    if box is None:
        return
    box.append({"to": to, "text": text, "at": time.time()})


def record(kind: str, **data: Any) -> None:
    """Append a debug event to the current trace. No-op outside console mode."""
    events = _trace.get()
    if events is None:
        return
    events.append({"kind": kind, "at": time.time(), **data})


def spawn_thread(target: Callable[..., Any], *args: Any, **kwargs: Any) -> threading.Thread:
    """Start a daemon thread that INHERITS the current context.

    `threading.Thread` starts with a fresh, empty context, so a plain spawn
    would silently drop the sandbox flag and let a background Sheets sync or
    client notification escape into production. Copying the context keeps the
    guards effective inside the child.
    """
    ctx = contextvars.copy_context()
    thread = threading.Thread(target=lambda: ctx.run(target, *args, **kwargs), daemon=True)
    thread.start()
    return thread
