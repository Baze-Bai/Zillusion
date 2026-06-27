"""Tests 16-25: URL canonicalization for deduplication."""

from src.tools.validation.url_canonicalizer import canonicalize_url


# ── Test 16: HTTP upgraded to HTTPS ──────────────────────────────────
def test_http_to_https():
    assert canonicalize_url("http://example.com/data") == "https://example.com/data"


# ── Test 17: Trailing slash removed ──────────────────────────────────
def test_trailing_slash_removed():
    result = canonicalize_url("https://example.com/path/")
    assert not result.endswith("/path/")
    assert result.endswith("/path")


# ── Test 18: Root path preserves single slash ────────────────────────
def test_root_path_preserved():
    result = canonicalize_url("https://example.com/")
    assert result == "https://example.com/"


# ── Test 19: www. prefix removed ─────────────────────────────────────
def test_www_removed():
    result = canonicalize_url("https://www.example.com/data")
    assert "www." not in result
    assert "example.com" in result


# ── Test 20: UTM tracking parameters stripped ────────────────────────
def test_utm_params_stripped():
    url = "https://example.com/page?utm_source=google&utm_medium=cpc&useful=1"
    result = canonicalize_url(url)
    assert "utm_source" not in result
    assert "utm_medium" not in result
    assert "useful=1" in result


# ── Test 21: fbclid parameter stripped ───────────────────────────────
def test_fbclid_stripped():
    url = "https://example.com/page?fbclid=abc123&id=42"
    result = canonicalize_url(url)
    assert "fbclid" not in result
    assert "id=42" in result


# ── Test 22: Fragment removed ────────────────────────────────────────
def test_fragment_removed():
    result = canonicalize_url("https://example.com/page#section-2")
    assert "#" not in result


# ── Test 23: Query params sorted alphabetically ─────────────────────
def test_query_params_sorted():
    url = "https://example.com/api?z=1&a=2&m=3"
    result = canonicalize_url(url)
    idx_a = result.index("a=2")
    idx_m = result.index("m=3")
    idx_z = result.index("z=1")
    assert idx_a < idx_m < idx_z


# ── Test 24: Hostname lowercased ─────────────────────────────────────
def test_hostname_lowercased():
    result = canonicalize_url("https://DATA.GOV/Dataset")
    assert "data.gov" in result


# ── Test 25: Non-standard port preserved ─────────────────────────────
def test_non_standard_port_preserved():
    result = canonicalize_url("https://example.com:8080/api")
    assert ":8080" in result


# ── Test 25b: Standard port (443) removed ─────────────────────────────
def test_standard_port_removed():
    result = canonicalize_url("https://example.com:443/api")
    assert ":443" not in result
