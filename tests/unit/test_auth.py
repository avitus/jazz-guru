from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jazz_guru import auth


def _build_app() -> FastAPI:
    app = FastAPI()
    auth.install(app)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/sessions")
    async def sessions() -> list[str]:
        return []

    @app.get("/")
    async def index() -> dict[str, str]:
        return {"index": "yes"}

    return app


def test_no_auth_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JG_API_KEY", raising=False)
    client = TestClient(_build_app())
    assert client.get("/health").status_code == 200
    assert client.get("/sessions").status_code == 200


def test_protected_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JG_API_KEY", "topsecret")
    client = TestClient(_build_app())
    # exempt paths still open
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
    # protected path
    assert client.get("/sessions").status_code == 401
    # with header
    r = client.get("/sessions", headers={"x-api-key": "topsecret"})
    assert r.status_code == 200
    # with query param
    r = client.get("/sessions?key=topsecret")
    assert r.status_code == 200
    # wrong key
    r = client.get("/sessions", headers={"x-api-key": "nope"})
    assert r.status_code == 401


def test_require_ws(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    monkeypatch.setenv("JG_API_KEY", "abc")
    auth.require_ws("abc")  # ok
    with pytest.raises(HTTPException):
        auth.require_ws("bad")
    monkeypatch.delenv("JG_API_KEY", raising=False)
    auth.require_ws(None)  # ok when unset
