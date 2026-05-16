from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import anthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from jazz_guru.config import get_settings

DeltaPayload = dict[str, Any]
OnDelta = Callable[[DeltaPayload], Awaitable[None] | None]


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: LLMUsage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd


# Approximate pricing for sonnet-class models (USD per 1M tokens). Override via env later if needed.
_PRICE_INPUT_PER_M = 3.0
_PRICE_OUTPUT_PER_M = 15.0


def _price(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000.0) * _PRICE_INPUT_PER_M + (output_tokens / 1_000_000.0) * _PRICE_OUTPUT_PER_M


@dataclass
class LLMResponse:
    raw: Any
    text: str
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    usage: LLMUsage = field(default_factory=LLMUsage)


@lru_cache(maxsize=1)
def get_client() -> anthropic.AsyncAnthropic:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


async def complete(
    messages: list[dict[str, Any]],
    *,
    system: str | list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.7,
    model: str | None = None,
    on_delta: OnDelta | None = None,
) -> LLMResponse:
    """Issue a Messages call via the streaming API.

    Streaming is used unconditionally — it removes the SDK's ~21k non-streaming
    token guard and lets callers tap incremental events via ``on_delta``. The
    returned :class:`LLMResponse` is assembled from the final accumulated
    message, so callers that don't need streaming UX can ignore ``on_delta``
    and treat this function identically to the previous non-streaming version.

    ``on_delta`` receives one of:

    - ``{"type": "text", "index": int, "text": str}`` for each text-token delta
    - ``{"type": "input_json", "index": int, "partial_json": str}`` for each
      streamed chunk of a tool-use input

    The callback may be sync or async; both are awaited if needed.

    Connection / TLS / 5xx / rate-limit failures BEFORE the first delta are
    retried inside ``_open_stream``. Errors mid-stream are surfaced to the
    caller without retry, so ``on_delta`` is never replayed with deltas it
    has already observed.
    """
    settings = get_settings()
    client = get_client()
    kwargs: dict[str, Any] = {
        "model": model or settings.anthropic_model,
        "max_tokens": max_tokens or settings.anthropic_max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if system is not None:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools
    if tool_choice:
        kwargs["tool_choice"] = tool_choice

    manager, stream = await _open_stream(client, kwargs)
    try:
        if on_delta is not None:
            async for event in stream:
                payload = _delta_payload(event)
                if payload is None:
                    continue
                ret = on_delta(payload)
                if inspect.isawaitable(ret):
                    await ret
        msg = await stream.get_final_message()
    finally:
        await manager.__aexit__(None, None, None)

    text_chunks: list[str] = []
    tool_uses: list[dict[str, Any]] = []
    # ``msg.content`` is a discriminated union (TextBlock | ToolUseBlock | ...);
    # we duck-type by the ``type`` tag rather than maintaining an exhaustive
    # isinstance ladder, so we widen to Any inside the loop.
    for raw_block in msg.content:
        block: Any = raw_block
        btype = getattr(block, "type", None)
        if btype == "text":
            text_chunks.append(block.text)
        elif btype == "tool_use":
            tool_uses.append({"id": block.id, "name": block.name, "input": block.input})

    usage = LLMUsage(
        input_tokens=msg.usage.input_tokens,
        output_tokens=msg.usage.output_tokens,
        cost_usd=_price(msg.usage.input_tokens, msg.usage.output_tokens),
    )

    return LLMResponse(
        raw=msg,
        text="".join(text_chunks),
        tool_uses=tool_uses,
        stop_reason=msg.stop_reason,
        usage=usage,
    )


def _delta_payload(event: Any) -> DeltaPayload | None:
    """Translate a raw stream event into the small public payload, or None.

    Only content_block_delta events carry incremental content; everything else
    (message_start, content_block_start/stop, message_delta, message_stop) is
    structural and we let it flow into ``get_final_message``.
    """
    if getattr(event, "type", None) != "content_block_delta":
        return None
    delta = getattr(event, "delta", None)
    dtype = getattr(delta, "type", None)
    index = getattr(event, "index", 0)
    if dtype == "text_delta":
        return {"type": "text", "index": index, "text": getattr(delta, "text", "")}
    if dtype == "input_json_delta":
        return {"type": "input_json", "index": index, "partial_json": getattr(delta, "partial_json", "")}
    return None


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=10),
    retry=retry_if_exception_type(
        (
            anthropic.APIConnectionError,
            anthropic.APITimeoutError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
        )
    ),
)
async def _open_stream(
    client: anthropic.AsyncAnthropic, kwargs: dict[str, Any]
) -> tuple[Any, Any]:
    """Open a streaming response with the same tenacity policy as ``complete``.

    Returns ``(manager, stream)``. The HTTP request happens inside
    ``__aenter__``, so retrying that call retries connect / TLS / 5xx /
    rate-limit errors before any deltas have been observed by the caller.
    Mid-stream errors after the first delta are NOT retried here — that
    would replay text the caller has already seen.
    """
    manager = client.messages.stream(**kwargs)
    stream = await manager.__aenter__()
    return manager, stream


async def complete_stream(
    messages: list[dict[str, Any]],
    *,
    system: str | list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.7,
    model: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream a Claude response, yielding incremental events.

    Yields dicts of these shapes:
      - {"type": "text_delta", "delta": str}
      - {"type": "done", "response": LLMResponse}

    The pre-first-delta phase (HTTP connect, auth, initial response) goes
    through the same tenacity retry policy as ``complete``. Once the stream
    starts producing deltas, errors are surfaced to the caller without
    retry — a mid-stream restart would replay text already shipped.
    """
    settings = get_settings()
    client = get_client()
    kwargs: dict[str, Any] = {
        "model": model or settings.anthropic_model,
        "max_tokens": max_tokens or settings.anthropic_max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if system is not None:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools
    if tool_choice:
        kwargs["tool_choice"] = tool_choice

    manager, stream = await _open_stream(client, kwargs)
    try:
        async for evt in stream:
            if getattr(evt, "type", None) == "text":
                # Helper events expose ``.text`` for content_block_delta of
                # type "text"; mypy can't narrow across the parsed-event
                # union without listing every variant, so getattr keeps it
                # honest.
                delta = getattr(evt, "text", "")
                if delta:
                    yield {"type": "text_delta", "delta": delta}
        msg = await stream.get_final_message()
    finally:
        await manager.__aexit__(None, None, None)

    text_chunks: list[str] = []
    tool_uses: list[dict[str, Any]] = []
    for block in msg.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_chunks.append(getattr(block, "text", ""))
        elif btype == "tool_use":
            tool_uses.append(
                {
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}),
                }
            )

    usage = LLMUsage(
        input_tokens=msg.usage.input_tokens,
        output_tokens=msg.usage.output_tokens,
        cost_usd=_price(msg.usage.input_tokens, msg.usage.output_tokens),
    )
    yield {
        "type": "done",
        "response": LLMResponse(
            raw=msg,
            text="".join(text_chunks),
            tool_uses=tool_uses,
            stop_reason=msg.stop_reason,
            usage=usage,
        ),
    }


async def health_check_detailed() -> tuple[bool, str]:
    """Returns (ok, message). Message is the model reply on success, or error detail on failure."""
    try:
        resp = await complete(
            [{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=8,
            temperature=0.0,
        )
        ok = "ok" in resp.text.lower()
        return ok, resp.text or f"(empty reply, stop_reason={resp.stop_reason})"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def health_check() -> bool:
    try:
        resp = await complete(
            [{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=8,
            temperature=0.0,
        )
        return "ok" in resp.text.lower()
    except Exception:
        return False
