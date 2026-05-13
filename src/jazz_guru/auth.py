"""Optional X-API-Key middleware. Off unless ``JG_API_KEY`` is set in the env."""
from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse


def _api_key() -> str | None:
    """Read the expected key fresh every call so tests can monkeypatch the env."""
    v = os.environ.get("JG_API_KEY")
    return v or None


_EXEMPT_EXACT = {
    "/", "/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico",
    "/ui", "/static", "/user_manual",
}


def _exempt(path: str) -> bool:
    # Exact-match the bare paths plus prefix-match the trailing-slash variants
    # so we don't accidentally exempt e.g. /uiadmin.
    return path in _EXEMPT_EXACT or path.startswith(("/ui/", "/static/", "/user_manual/"))


def install(app: FastAPI) -> None:
    """Install the middleware. No-op-by-default; checks the key only when JG_API_KEY is set."""

    @app.middleware("http")
    async def _x_api_key(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        expected = _api_key()
        if expected is None:
            return await call_next(request)
        if _exempt(request.url.path):
            return await call_next(request)
        # HTTP clients must send the key in the X-API-Key header — query
        # params end up in proxy logs and browser history. WebSocket auth
        # is handled separately via `require_ws()`, where a query-string
        # token is the only practical option.
        provided = request.headers.get("x-api-key")
        if provided != expected:
            return JSONResponse(
                {"detail": "missing or invalid x-api-key"},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        return await call_next(request)


def require_ws(token: str | None) -> None:
    """Helper for websocket routes (middleware doesn't run on WS in starlette)."""
    expected = _api_key()
    if expected is None:
        return
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid key")
