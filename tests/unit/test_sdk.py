from __future__ import annotations

import httpx
import pytest

from jazz_guru.client.sdk import JazzGuruClient, ServerError


async def test_health_round_trip() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    c = JazzGuruClient("http://test")
    c._client = httpx.AsyncClient(base_url="http://test", transport=transport, headers=c._headers())
    try:
        out = await c.health()
        assert out == {"status": "ok"}
    finally:
        await c.close()


async def test_create_session_and_chat_returns_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"id": "00000000-0000-0000-0000-000000000001"})
        if request.url.path.endswith("/chat"):
            return httpx.Response(
                200,
                json={
                    "text": "hi",
                    "tool_calls": 2,
                    "rounds": 3,
                    "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001},
                    "errors": [],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    c = JazzGuruClient("http://test")
    c._client = httpx.AsyncClient(base_url="http://test", transport=transport, headers=c._headers())
    try:
        sid = await c.create_session(title="t")
        assert sid == "00000000-0000-0000-0000-000000000001"
        res = await c.chat(sid, "hello")
        assert res.text == "hi"
        assert res.tool_calls == 2
        assert res.usage["cost_usd"] == 0.001
    finally:
        await c.close()


async def test_server_error_raises_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    c = JazzGuruClient("http://test")
    c._client = httpx.AsyncClient(base_url="http://test", transport=transport)
    try:
        with pytest.raises(ServerError) as ei:
            await c.health()
        assert ei.value.status == 500
    finally:
        await c.close()


async def test_api_key_header_is_attached() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k: v for k, v in request.headers.items() if k.lower() == "x-api-key"})
        return httpx.Response(200, json={"status": "ok"})

    c = JazzGuruClient("http://test", api_key="test-placeholder-xxxxxxxx")
    c._client = httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler), headers=c._headers()
    )
    try:
        await c.health()
        assert seen.get("x-api-key") == "test-placeholder-xxxxxxxx"
    finally:
        await c.close()
