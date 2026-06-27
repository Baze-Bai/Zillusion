"""Unit tests for src.tools.net_guard — the SSRF URL guard (H3)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.tools.net_guard import (
    UnsafeURLError,
    _addr_is_public,
    assert_safe_url,
    is_safe_url,
)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # RFC1918
        "172.16.3.4",  # RFC1918
        "192.168.1.1",  # RFC1918
        "169.254.169.254",  # link-local / cloud metadata
        "0.0.0.0",  # unspecified
        "::1",  # v6 loopback
        "::ffff:127.0.0.1",  # v4-mapped loopback (must be unwrapped)
        "fe80::1",  # v6 link-local
        "224.0.0.1",  # multicast
    ],
)
def test_addr_is_public_rejects_internal(ip):
    assert _addr_is_public(ip) is False


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:2800:220:1::1"])
def test_addr_is_public_allows_public(ip):
    assert _addr_is_public(ip) is True


def test_addr_is_public_garbage_is_not_public():
    assert _addr_is_public("not-an-ip") is False


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # AWS metadata
        "http://127.0.0.1:8000/",
        "http://localhost:9090/metrics",
        "http://10.0.0.5/admin",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://metadata.google.internal/",  # resolves to 169.254.169.254
    ],
)
def test_assert_safe_url_blocks_internal(url):
    with pytest.raises(UnsafeURLError):
        assert_safe_url(url)
    assert is_safe_url(url) is False


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://host/x", "gopher://x/", "data:text/plain,hi", ""])
def test_assert_safe_url_blocks_non_http(url):
    with pytest.raises(UnsafeURLError):
        assert_safe_url(url)


def test_assert_safe_url_allows_public_ip_literal():
    # IP literals resolve offline (no DNS) — 8.8.8.8 is global unicast.
    assert_safe_url("https://8.8.8.8/")
    assert is_safe_url("http://1.1.1.1/path?q=1") is True


def test_assert_safe_url_mixed_resolution_is_blocked():
    # A hostname with one public AND one private A record must be rejected.
    fake = [
        (2, 1, 6, "", ("93.184.216.34", 443)),
        (2, 1, 6, "", ("127.0.0.1", 443)),
    ]
    with patch("src.tools.net_guard.socket.getaddrinfo", return_value=fake):
        with pytest.raises(UnsafeURLError):
            assert_safe_url("https://evil.example/")


def test_assert_safe_url_all_public_resolution_allowed():
    fake = [(2, 1, 6, "", ("93.184.216.34", 443))]
    with patch("src.tools.net_guard.socket.getaddrinfo", return_value=fake):
        assert_safe_url("https://example.com/")


def test_assert_safe_url_unresolvable_is_blocked():
    import socket as _s

    with patch("src.tools.net_guard.socket.getaddrinfo", side_effect=_s.gaierror("nope")):
        with pytest.raises(UnsafeURLError):
            assert_safe_url("https://nonexistent.invalid/")
