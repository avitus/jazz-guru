from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field

from jazz_guru.actions.registry import registry


class HttpGetInput(BaseModel):
    url: str
    headers: dict[str, str] | None = None
    params: dict[str, str] | None = None
    max_bytes: int = Field(500_000, description="Truncate body to this many bytes.")


class HttpPostInput(BaseModel):
    url: str
    json_body: dict[str, Any] | None = Field(None, description="JSON request body.")
    data: dict[str, Any] | None = Field(None, description="Form-encoded body.")
    headers: dict[str, str] | None = None
    max_bytes: int = 500_000


@registry.register(
    "http_get",
    description="HTTP GET; returns status_code, headers, and (truncated) body.",
    input_model=HttpGetInput,
    tags=("http",),
)
async def http_get(url: str, headers: dict[str, str] | None = None, params: dict[str, str] | None = None, max_bytes: int = 500_000) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        r = await client.get(url, headers=headers, params=params)
    return {
        "status_code": r.status_code,
        "headers": dict(r.headers),
        "body": r.text[:max_bytes],
        "truncated": len(r.text) > max_bytes,
    }


@registry.register(
    "http_post",
    description="HTTP POST with JSON or form body.",
    input_model=HttpPostInput,
    tags=("http",),
)
async def http_post(
    url: str,
    json_body: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    max_bytes: int = 500_000,
) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        r = await client.post(url, json=json_body, data=data, headers=headers)
    return {
        "status_code": r.status_code,
        "headers": dict(r.headers),
        "body": r.text[:max_bytes],
        "truncated": len(r.text) > max_bytes,
    }
