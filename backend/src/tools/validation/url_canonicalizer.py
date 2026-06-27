"""URL canonicalization for deduplication."""

from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# Query parameters to strip (tracking / session / noise)
STRIP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "source", "session_id", "sid",
    "_ga", "_gl", "mc_cid", "mc_eid",
}


def lowercase_host(url: str) -> str:
    """Lowercase scheme + host of `url`, leaving path/query/fragment untouched.

    HTTP scheme and DNS host are case-insensitive, but path and query may be
    case-sensitive (S3 keys, GitHub paths, etc.). When the LLM emits a
    canonical-source hint as ``"API.bls.gov"`` it leaks unchanged into
    ``source.url`` and ``source.api_spec.endpoint`` and reads to the user
    as a broken link. ``canonicalize_url`` already does this lowercasing
    but also strips trailing slashes / query params, which is too lossy
    when we still want to surface the original URL — this helper does the
    minimum case fold and nothing else.
    """
    if not url:
        return url
    parsed = urlparse(url)
    if not parsed.hostname:
        return url
    host = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port else ""
    return urlunparse((
        parsed.scheme.lower(),
        f"{host}{port}",
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment,
    ))


def canonicalize_url(url: str) -> str:
    """Normalize URL for deduplication purposes.

    - Lowercase scheme and host
    - Remove trailing slash
    - Remove fragment (#...)
    - Remove tracking query parameters
    - HTTP → HTTPS
    - Remove www. prefix
    """
    parsed = urlparse(url)

    # HTTP → HTTPS
    scheme = "https" if parsed.scheme in ("http", "https") else parsed.scheme

    # Lowercase host, remove www.
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    # Add port if non-standard
    port = parsed.port
    port_str = f":{port}" if port and port not in (80, 443) else ""

    # Clean path: remove trailing slash
    path = parsed.path.rstrip("/") or "/"

    # Slug normalization on the last path segment: collapse `-` and `_` so that
    # /covidvaccinations and /covid-vaccinations resolve to the same dedup key.
    # Scoped to the last segment with no file extension to avoid collapsing
    # things like /data/abc-1.csv vs /data/abc-2.csv.
    if path != "/":
        parts = path.split("/")
        last = parts[-1]
        if last and "." not in last:
            collapsed = last.replace("-", "").replace("_", "")
            if collapsed != last:
                parts[-1] = collapsed
                path = "/".join(parts)

    # Clean query: remove tracking params, sort remaining
    query_params = parse_qs(parsed.query, keep_blank_values=False)
    cleaned_params = {
        k: v for k, v in sorted(query_params.items()) if k.lower() not in STRIP_PARAMS
    }
    query = urlencode(cleaned_params, doseq=True)

    # Reconstruct without fragment
    return urlunparse((scheme, f"{host}{port_str}", path, "", query, ""))
