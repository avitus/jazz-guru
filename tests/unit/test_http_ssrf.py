"""SSRF guard tests for http_get / http_post.

`socket.getaddrinfo` is monkey-patched so we don't perform real DNS, and
`httpx.MockTransport` lets us return canned responses (including
redirects) without going to the network.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import httpx
import pytest

from jazz_guru.actions.tools.http import (
    BlockedTargetError,
    _ip_block_reason,
    _request_with_validation,
    _validate_url,
)

# ---------- _ip_block_reason ---------------------------------------------


@pytest.mark.parametrize(
    "ip,expected_substr",
    [
        ("127.0.0.1", "loopback"),
        ("::1", "loopback"),
        ("10.0.0.1", "private"),
        ("172.16.5.5", "private"),
        ("192.168.1.1", "private"),
        ("fd00::1", "private"),
        ("169.254.169.254", "link-local"),  # AWS metadata
        ("169.254.169.123", "link-local"),  # AWS time-sync
        ("100.100.100.200", "metadata"),  # Alibaba — caught by the explicit metadata-IP list
        ("fe80::1", "link-local"),
        ("224.0.0.1", "multicast"),
        ("0.0.0.0", "unspecified"),
    ],
)
def test_ip_block_reason_blocks(ip: str, expected_substr: str) -> None:
    reason = _ip_block_reason(ipaddress.ip_address(ip))
    assert reason is not None
    assert expected_substr.lower() in reason.lower()


@pytest.mark.parametrize(
    "ip",
    [
        "8.8.8.8",
        "1.1.1.1",
        "93.184.216.34",  # example.com at time of writing
        "2606:4700:4700::1111",
    ],
)
def test_ip_block_reason_allows_public(ip: str) -> None:
    assert _ip_block_reason(ipaddress.ip_address(ip)) is None


# ---------- _validate_url -------------------------------------------------


def _stub_resolver(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr: tuple[Any, ...] = (ip, 0, 0, 0) if ":" in ip else (ip, 0)

    def fake_gai(host: str, port: Any, **kw: Any) -> list[tuple[Any, ...]]:
        return [(family, socket.SOCK_STREAM, 0, "", sockaddr)]

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)


def test_validate_url_rejects_non_http_scheme() -> None:
    with pytest.raises(BlockedTargetError, match="scheme"):
        _validate_url("file:///etc/passwd")
    with pytest.raises(BlockedTargetError, match="scheme"):
        _validate_url("gopher://example.com/")


def test_validate_url_rejects_resolved_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_resolver(monkeypatch, "127.0.0.1")
    with pytest.raises(BlockedTargetError, match="loopback"):
        _validate_url("https://malicious.example.test/foo")


def test_validate_url_rejects_resolved_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_resolver(monkeypatch, "169.254.169.254")
    with pytest.raises(BlockedTargetError, match="link-local"):
        _validate_url("https://malicious.example.test/")


def test_validate_url_rejects_literal_rfc1918() -> None:
    with pytest.raises(BlockedTargetError, match="private"):
        _validate_url("http://10.0.0.5/admin")


def test_validate_url_rejects_ipv6_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_resolver(monkeypatch, "fd00:ec2::254")
    with pytest.raises(BlockedTargetError, match="metadata"):
        _validate_url("https://imds.example.test/")


def test_validate_url_accepts_public(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_resolver(monkeypatch, "8.8.8.8")
    _validate_url("https://example.test/")  # no exception


def test_validate_url_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*a: Any, **k: Any) -> Any:
        raise socket.gaierror(-2, "Name not known")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(BlockedTargetError, match="DNS"):
        _validate_url("https://nonexistent.example.test/")


# ---------- redirect validation -------------------------------------------
async def test_redirect_to_private_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 302 to a private host must be caught by the per-hop revalidation."""
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        # First hop is a public host; redirect target is loopback.
        if req.url.host == "ok.example.test":
            return httpx.Response(302, headers={"location": "https://internal.example.test/"})
        seen["reached_private"] = str(req.url)
        return httpx.Response(200, text="should not happen")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, follow_redirects=False)

    # First call: ok.example.test resolves public; second hop:
    # internal.example.test resolves to 127.0.0.1.
    call_count = {"n": 0}

    def fake_gai(host: str, *a: Any, **k: Any) -> list[tuple[Any, ...]]:
        call_count["n"] += 1
        ip = "8.8.8.8" if host == "ok.example.test" else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)

    with pytest.raises(BlockedTargetError, match="loopback"):
        await _request_with_validation("GET", "https://ok.example.test/", client=client)
    assert "reached_private" not in seen
    await client.aclose()


async def test_public_redirect_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: public→public redirect is followed and final body is returned."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "a.example.test":
            return httpx.Response(302, headers={"location": "https://b.example.test/"})
        return httpx.Response(200, text="hello")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, follow_redirects=False)

    def fake_gai(host: str, *a: Any, **k: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)

    r = await _request_with_validation("GET", "https://a.example.test/", client=client)
    assert r["status_code"] == 200
    assert r["body"] == "hello"
    assert r["truncated"] is False
    await client.aclose()


async def test_response_truncated_to_max_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Streaming should stop reading once max_bytes is hit."""
    big = "x" * 5000

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=big)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, follow_redirects=False)

    def fake_gai(host: str, *a: Any, **k: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)

    r = await _request_with_validation(
        "GET",
        "https://example.test/",
        client=client,
        max_bytes=100,
    )
    assert len(r["body"].encode("utf-8")) <= 100
    assert r["truncated"] is True
    await client.aclose()


async def test_redirect_loop_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://loop.example.test/"})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, follow_redirects=False)

    def fake_gai(host: str, *a: Any, **k: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)

    with pytest.raises(BlockedTargetError, match="redirects"):
        await _request_with_validation("GET", "https://loop.example.test/", client=client)
    await client.aclose()
