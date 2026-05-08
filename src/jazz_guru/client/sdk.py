"""Async Python SDK for the jazz-guru FastAPI server.

Wraps the REST + websocket surface and gives you typed return values.
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import websockets


class ServerError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"server error {status}: {body[:300]}")
        self.status = status
        self.body = body


@dataclass
class ChatResult:
    text: str
    tool_calls: int
    rounds: int
    usage: dict[str, Any]
    errors: list[str]


@dataclass
class MemoryHit:
    id: str
    kind: str
    text: str
    score: float


class JazzGuruClient:
    """Thin async client. Use as a context manager for connection pooling.

    >>> async with JazzGuruClient("http://127.0.0.1:8000") as c:
    ...     sid = await c.create_session()
    ...     async for evt in c.stream_chat(sid, "hello"):
    ...         print(evt)
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        api_key: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("JG_API_KEY") or None
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    # -- lifecycle -----------------------------------------------------------
    async def __aenter__(self) -> JazzGuruClient:
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def open(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._timeout,
                headers=self._headers(),
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        h = {"accept": "application/json"}
        if self.api_key:
            h["x-api-key"] = self.api_key
        return h

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("client not opened; use `async with` or call .open()")
        return self._client

    # -- low-level helpers ---------------------------------------------------
    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        r = await self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise ServerError(r.status_code, r.text)
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r.text

    # -- REST ----------------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def goal(self) -> dict[str, Any]:
        return await self._request("GET", "/goal")

    async def create_session(self, *, title: str | None = None, goal_profile: str = "default") -> str:
        out = await self._request("POST", "/sessions", json={"title": title, "goal_profile": goal_profile})
        return str(out["id"])

    async def chat(self, session_id: str, message: str) -> ChatResult:
        out = await self._request("POST", f"/sessions/{session_id}/chat", json={"message": message})
        return ChatResult(
            text=out.get("text", ""),
            tool_calls=int(out.get("tool_calls", 0)),
            rounds=int(out.get("rounds", 0)),
            usage=out.get("usage", {}),
            errors=list(out.get("errors", [])),
        )

    async def distill(self, session_id: str, *, sync: bool = True) -> dict[str, Any]:
        return await self._request("POST", f"/sessions/{session_id}/distill", params={"sync": sync})

    async def eval_run(self, *, only: str | None = None) -> dict[str, Any]:
        params = {"only": only} if only else None
        return await self._request("POST", "/eval/run", params=params)

    async def memory_search(
        self, query: str, *, k: int = 5, session_id: str | None = None
    ) -> list[MemoryHit]:
        body = {"query": query, "k": k, "session_id": session_id}
        out = await self._request("POST", "/memory/search", json=body)
        return [MemoryHit(**r) for r in out.get("results", [])]

    async def list_artifacts(self, session_id: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/artifacts/{session_id}")

    # -- WebSocket -----------------------------------------------------------
    @asynccontextmanager
    async def _ws(self, session_id: str) -> AsyncIterator[Any]:
        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        url = f"{ws_url}/ws/sessions/{session_id}/chat"
        if self.api_key:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}key={self.api_key}"
        async with websockets.connect(url, max_size=4 * 1024 * 1024) as ws:
            yield ws

    async def stream_chat(
        self,
        session_id: str,
        message: str,
        *,
        on_event: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a message over the websocket; yield each event including the final reply.

        Each yielded event is a dict with at least ``type``. Final reply has type ``final``.
        """

        async def _gen() -> AsyncIterator[dict[str, Any]]:
            async with self._ws(session_id) as ws:
                await ws.send(json.dumps({"message": message}))
                while True:
                    raw = await ws.recv()
                    evt = json.loads(raw)
                    if on_event is not None:
                        result = on_event(evt)
                        if result is not None:
                            await result
                    yield evt
                    if evt.get("type") in ("final", "error"):
                        break

        return _gen()
