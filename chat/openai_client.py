"""Shared OpenAI client, instrumented for the agent-test console.

Every LLM call in the pipeline goes through one of three `OpenAI(...)` objects
(`chat/llm.py`, `handlers/extractor.py`, `handlers/academy_extractor.py`).
Routing all three through this module means the console trace captures the
model, the exact system prompt, the tool schemas, token usage and latency for
*every* call, without a single edit at the ~6 call sites — and without those
call sites having to know the console exists.

Outside a console turn `test_context.record()` is a no-op, so the only cost on
the production path is one ContextVar lookup and a `perf_counter` pair.
"""

import time
from typing import Any

from openai import OpenAI

import config
from integrations import test_context

def _system_prompt_of(messages: Any) -> str | None:
    try:
        for m in messages or []:
            if m.get("role") == "system":
                return m.get("content")
    except Exception:  # noqa: BLE001 — tracing must never break a real call
        pass
    return None


class _TracingCompletions:
    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def __getattr__(self, name: str) -> Any:
        # Only `create` is traced; `.parse`, `.stream`, `.with_raw_response` and
        # anything the SDK adds later must still reach the real client. These
        # call sites are on the LIVE pipeline, not just the console.
        return getattr(self._raw, name)

    def create(self, *args: Any, **kwargs: Any) -> Any:
        if not test_context.is_test_mode():
            return self._raw.create(*args, **kwargs)

        started = time.perf_counter()
        error = None
        response = None
        try:
            response = self._raw.create(*args, **kwargs)
            return response
        except Exception as exc:  # noqa: BLE001 — re-raised below
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            usage = getattr(response, "usage", None)
            reply = None
            tool_calls = None
            try:
                message = response.choices[0].message if response else None
                reply = getattr(message, "content", None)
                raw_calls = getattr(message, "tool_calls", None) or []
                tool_calls = [
                    {"name": c.function.name, "arguments": c.function.arguments}
                    for c in raw_calls
                ]
            except Exception:  # noqa: BLE001
                pass

            test_context.record(
                "llm_call",
                model=kwargs.get("model"),
                system_prompt=_system_prompt_of(kwargs.get("messages")),
                messages=kwargs.get("messages"),
                tool_choice=kwargs.get("tool_choice"),
                tools=[
                    t.get("function", {}).get("name")
                    for t in (kwargs.get("tools") or [])
                    if isinstance(t, dict)
                ],
                temperature=kwargs.get("temperature"),
                reply=reply,
                tool_calls=tool_calls,
                prompt_tokens=getattr(usage, "prompt_tokens", None),
                completion_tokens=getattr(usage, "completion_tokens", None),
                total_tokens=getattr(usage, "total_tokens", None),
                elapsed_ms=elapsed_ms,
                error=error,
            )


class _TracingChat:
    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self.completions = _TracingCompletions(raw.completions)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


class TracingOpenAI:
    """Thin proxy over `OpenAI` — traces chat completions, passes the rest through."""

    def __init__(self, raw: OpenAI) -> None:
        self._raw = raw
        self.chat = _TracingChat(raw.chat)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


client = TracingOpenAI(OpenAI(api_key=config.OPENAI_API_KEY))
