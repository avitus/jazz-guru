"""HTTP fetch tools with SSRF protection.

Both ``http_get`` and ``http_post`` validate the destination IP before each
request and re-validate every redirect target. Blocked categories:

* loopback (127.0.0.0/8, ::1)
* RFC1918 / ULA private ranges (10/8, 172.16/12, 192.168/16, fc00::/7)
* link-local (169.254/16, fe80::/10) — covers AWS/Azure/GCP/OCI metadata
* multicast / reserved / unspecified
* explicit cloud-metadata endpoints (169.254.169.254, 100.100.100.200,
  fd00:ec2::254) — defense-in-depth even if the link-local check ever
  loosens.

Redirects are followed manually with a 5-hop cap; httpx's automatic
``follow_redirects=True`` would skip the per-hop check. Known limitation:
this does not fully defeat DNS rebinding — a hostile DNS could resolve to
a public IP at validation time and a private IP at connection time. A
fully hardened implementation would pin the validated IP into the
connection layer; that is out of scope here.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from jazz_guru.actions.registry import registry

_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_METADATA_IPS = {
    "169.254.169.254",   # AWS / Azure / GCP / OCI IMDS
    "169.254.169.123",   # AWS time sync
    "100.100.100.200",   # Alibaba Cloud
    "fd00:ec2::254",     # AWS IPv6 IMDS
}
_MAX_REDIRECTS = 5
_TIMEOUT_SEC = 30.0


class BlockedTargetError(ValueError):
    """Raised when a URL points at a disallowed destination."""


def _ip_block_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    # Order matters: many IPs match more than one classifier (e.g. 169.254/16
    # is BOTH link-local AND private in Python). We want the more specific
    # reason first so the error message is informative.
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local (covers cloud metadata endpoints)"
    if ip.is_multicast:
        return "multicast"
    if str(ip) in _BLOCKED_METADATA_IPS:
        return "cloud metadata endpoint"
    if ip.is_private:
        return "private (RFC1918 / ULA)"
    if ip.is_reserved:
        return "reserved"
    return None


def _resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise BlockedTargetError(f"DNS resolution failed for {host!r}: {e}") from e
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for _fam, _typ, _proto, _canon, sockaddr in infos:
        addrs.append(ipaddress.ip_address(sockaddr[0]))
    if not addrs:
        raise BlockedTargetError(f"no addresses resolved for {host!r}")
    return addrs


def _validate_url(url: str) -> None:
    """Raise ``BlockedTargetError`` if ``url`` resolves to a disallowed IP."""
    p = urlparse(url)
    if p.scheme not in _ALLOWED_SCHEMES:
        raise BlockedTargetError(
            f"scheme {p.scheme!r} not allowed (only http/https)"
        )
    host = p.hostname
    if not host:
        raise BlockedTargetError(f"no hostname in URL: {url!r}")
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        addrs = _resolve_host(host)
    for ip in addrs:
        reason = _ip_block_reason(ip)
        if reason:
            raise BlockedTargetError(f"{host} -> {ip} blocked: {reason}")


async def _request_with_validation(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    max_bytes: int = 500_000,
    client: httpx.AsyncClient | None = None,
) -> dict[str, object]:
    """Validate every hop (including redirects) and stream up to max_bytes.

    Returns a dict shaped like ``_format_response``. Streaming lets us stop
    reading the wire once we've accumulated ``max_bytes`` raw bytes, which
    is the only honest way to enforce the cap — the previous ``r.text[:N]``
    approach buffered the whole body in memory and counted *characters*,
    not bytes.
    """
    cur_method = method
    cur_url = url
    cur_json: dict[str, Any] | None = json_body
    cur_data: dict[str, Any] | None = data
    owns_client = client is None
    c = client or httpx.AsyncClient(timeout=_TIMEOUT_SEC, follow_redirects=False)
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            _validate_url(cur_url)
            req = c.build_request(
                cur_method, cur_url,
                headers=headers, params=params,
                json=cur_json, data=cur_data,
            )
            r = await c.send(req, stream=True)
            try:
                if 300 <= r.status_code < 400:
                    loc = r.headers.get("location")
                    if loc:
                        await r.aclose()
                        cur_url = str(httpx.URL(cur_url).join(loc))
                        if r.status_code not in (307, 308):
                            cur_method = "GET"
                            cur_json = None
                            cur_data = None
                        continue
                # Terminal hop — read up to max_bytes.
                buf = bytearray()
                truncated = False
                async for chunk in r.aiter_bytes():
                    remaining = max_bytes - len(buf)
                    if remaining <= 0:
                        truncated = True
                        break
                    if len(chunk) > remaining:
                        buf.extend(chunk[:remaining])
                        truncated = True
                        break
                    buf.extend(chunk)
                encoding = r.encoding or "utf-8"
                try:
                    text = buf.decode(encoding, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    text = buf.decode("utf-8", errors="replace")
                return {
                    "status_code": r.status_code,
                    "headers": dict(r.headers),
                    "body": text,
                    "truncated": truncated,
                    "final_url": str(r.request.url),
                }
            finally:
                await r.aclose()
        raise BlockedTargetError(f"exceeded {_MAX_REDIRECTS} redirects")
    finally:
        if owns_client:
            await c.aclose()


# ---------- tools ---------------------------------------------------------


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


def _format_error(e: BlockedTargetError) -> dict[str, object]:
    return {"error": "blocked", "reason": str(e)}


@registry.register(
    "http_get",
    description=(
        "HTTP GET; returns status_code, headers, and (truncated) body. "
        "Refuses URLs that resolve to private/loopback/link-local IPs or known "
        "cloud-metadata endpoints; re-validates each redirect hop."
    ),
    input_model=HttpGetInput,
    tags=("http",),
)
async def http_get(
    url: str,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    max_bytes: int = 500_000,
) -> dict[str, object]:
    try:
        return await _request_with_validation(
            "GET", url, headers=headers, params=params, max_bytes=max_bytes,
        )
    except BlockedTargetError as e:
        return _format_error(e)


@registry.register(
    "http_post",
    description=(
        "HTTP POST with JSON or form body. SSRF-guarded: refuses private "
        "destinations and re-validates each redirect hop."
    ),
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
    try:
        return await _request_with_validation(
            "POST", url, headers=headers, json_body=json_body, data=data,
            max_bytes=max_bytes,
        )
    except BlockedTargetError as e:
        return _format_error(e)
