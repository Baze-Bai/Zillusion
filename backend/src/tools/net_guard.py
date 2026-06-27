"""SSRF guard: reject URLs that resolve to non-public address space.

Applied at the entry of every agent-/candidate-driven network tool (fetch,
probe, map, crawl). The discovery agent is steerable by injected page content,
and candidate URLs come from untrusted search results — without this guard
either can make the backend fetch cloud-metadata (169.254.169.254), a localhost
admin/metrics port, or an RFC1918 host, turning the fetch/probe tools into an
internal read / SSRF primitive (probe_url even returns status/length, and
fetch_page persists the body where the agent reads it).

Policy: scheme must be http/https; the host must resolve to ONLY public
addresses (every A/AAAA record is checked — a name with one public and one
private record is rejected).

Known residual (follow-up, not P1): resolve-then-connect is a DNS-rebinding
TOCTOU (the name can re-resolve between this check and the library's own
connect), and an allowed public URL that HTTP-redirects to an internal address
is followed by httpx internally. This guard blocks the dominant cases —
IP-literals, static internal names, and metadata endpoints — which is the bulk
of real SSRF; pinning the resolved IP through connect-time and validating each
redirect hop is the next increment.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

_ALLOWED_SCHEMES = {"http", "https"}


class UnsafeURLError(ValueError):
    """Raised when a URL is not a public http(s) endpoint."""


def _addr_is_public(ip_str: str) -> bool:
    """True only for a globally-routable unicast address. Unwraps IPv4-mapped
    IPv6 (``::ffff:127.0.0.1``) so loopback can't hide inside a v6 literal."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 169.254.0.0/16 — AWS/GCP/Azure metadata
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified  # 0.0.0.0 / ::
    )


def assert_safe_url(url: str) -> None:
    """Raise ``UnsafeURLError`` unless ``url`` is an http(s) URL whose host
    resolves exclusively to public addresses. Blocking — resolves DNS; call via
    :func:`assert_safe_url_async` from async code."""
    if not url or not isinstance(url, str):
        raise UnsafeURLError("empty or non-string URL")
    parts = urlparse(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme {scheme or '(none)'!r} not allowed (http/https only)")
    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL has no host")
    port = parts.port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURLError(f"cannot resolve host {host!r}: {e}") from e
    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise UnsafeURLError(f"host {host!r} resolved to no address")
    bad = sorted(a for a in addrs if not _addr_is_public(a))
    if bad:
        raise UnsafeURLError(f"host {host!r} resolves to non-public address(es): {', '.join(bad)}")


async def assert_safe_url_async(url: str) -> None:
    """Async wrapper — runs the blocking DNS resolution off the event loop."""
    await asyncio.to_thread(assert_safe_url, url)


def is_safe_url(url: str) -> bool:
    """Non-raising convenience: True iff :func:`assert_safe_url` would pass."""
    try:
        assert_safe_url(url)
        return True
    except UnsafeURLError:
        return False


__all__ = ["assert_safe_url", "assert_safe_url_async", "is_safe_url", "UnsafeURLError"]
