"""Batch CKAN portal health probe.

Hits each portal in `src.adapters.government.ckan_portals.CKAN_PORTALS`
on `/api/3/action/package_search`. For 404s, tries two fallback shapes:
POST `/api/hub/search/search` (EU style) and GET `/api/action/package_search`
(legacy CKAN without /3/). Reports per-portal status, latency, and the
detected response shape (CKAN-style dict vs HTML vs JSON-but-different).

Run from project root:
    python -m scripts.probe_ckan_portals

Or:
    python backend/scripts/probe_ckan_portals.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

# Make `src.*` imports resolve when run as a standalone script.
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from src.adapters.government.ckan_portals import CKAN_PORTALS  # noqa: E402

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
TIMEOUT = 10.0
QUERY = {"q": "weather", "rows": 1}
PAYLOAD_JSON = {"q": "weather", "limit": 1}

STATUS_OK = "OK"
STATUS_OK_VARIANT = "OK_VARIANT"
STATUS_HTML = "HTML"
STATUS_JSON_OTHER = "JSON_OTHER"
STATUS_404 = "404"
STATUS_OTHER_HTTP = "HTTP_ERR"
STATUS_NETWORK = "NETWORK"


def _classify_response(resp: httpx.Response) -> tuple[str, str]:
    """Return (status_label, detail) for a successful (2xx) response."""
    ctype = resp.headers.get("content-type", "").lower()
    body = resp.text[:500]

    if "html" in ctype or body.lstrip().startswith("<"):
        return STATUS_HTML, "HTML page (not JSON API)"
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError):
        return STATUS_JSON_OTHER, f"non-JSON body: {body[:80]!r}"

    # CKAN canonical shape: {"success": true, "result": {"results": [...]}}
    if isinstance(data, dict) and "result" in data:
        result = data.get("result")
        if isinstance(result, dict) and "results" in result:
            count = result.get("count", "?")
            return STATUS_OK, f"CKAN shape, count={count}"
    return STATUS_JSON_OTHER, f"JSON keys: {list(data.keys())[:5]}"


async def _probe_one(client: httpx.AsyncClient, entry: dict) -> dict:
    base_url = entry["base_url"].rstrip("/")
    name = entry["portal_name"]
    headers = {"User-Agent": UA}
    primary = f"{base_url}/api/3/action/package_search"

    started = time.monotonic()
    result = {
        "portal": name,
        "base_url": base_url,
        "country": entry.get("country", "—"),
        "status": "",
        "http": "",
        "detail": "",
        "ms": 0,
        "fix_hint": "",
    }

    try:
        resp = await client.get(primary, params=QUERY, headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        result["status"] = STATUS_NETWORK
        result["detail"] = f"{type(e).__name__}: {e}"
        result["ms"] = round((time.monotonic() - started) * 1000)
        return result

    result["http"] = str(resp.status_code)
    if 200 <= resp.status_code < 300:
        label, detail = _classify_response(resp)
        result["status"] = label
        result["detail"] = detail
        result["ms"] = round((time.monotonic() - started) * 1000)
        return result

    if resp.status_code != 404:
        result["status"] = STATUS_OTHER_HTTP
        result["detail"] = f"HTTP {resp.status_code}"
        result["ms"] = round((time.monotonic() - started) * 1000)
        return result

    # ── 404 path: try two variants ──
    legacy = f"{base_url}/api/action/package_search"  # no /3/
    eu_post = f"{base_url}/api/hub/search/search"  # EU portal style

    try:
        r2 = await client.get(legacy, params=QUERY, headers=headers, timeout=TIMEOUT)
        if 200 <= r2.status_code < 300:
            label, detail = _classify_response(r2)
            if label == STATUS_OK:
                result["status"] = STATUS_OK_VARIANT
                result["http"] = f"404→200 (legacy)"
                result["detail"] = f"{detail}; use /api/action/ (no /3/)"
                result["fix_hint"] = "drop_v3"
                result["ms"] = round((time.monotonic() - started) * 1000)
                return result
    except httpx.HTTPError:
        pass

    try:
        r3 = await client.post(eu_post, json=PAYLOAD_JSON, headers=headers, timeout=TIMEOUT)
        if 200 <= r3.status_code < 300:
            label, detail = _classify_response(r3)
            if label == STATUS_OK:
                result["status"] = STATUS_OK_VARIANT
                result["http"] = f"404→200 (POST)"
                result["detail"] = f"{detail}; use POST /api/hub/search/search"
                result["fix_hint"] = "post_hub_search"
                result["ms"] = round((time.monotonic() - started) * 1000)
                return result
    except httpx.HTTPError:
        pass

    result["status"] = STATUS_404
    result["detail"] = "primary 404, no variant worked"
    result["ms"] = round((time.monotonic() - started) * 1000)
    return result


async def main() -> None:
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    async with httpx.AsyncClient(
        limits=limits,
        follow_redirects=True,
        verify=True,
        http2=False,
    ) as client:
        tasks = [_probe_one(client, e) for e in CKAN_PORTALS]
        results = await asyncio.gather(*tasks)

    # ── Print table grouped by status ──
    order = [
        STATUS_OK,
        STATUS_OK_VARIANT,
        STATUS_HTML,
        STATUS_JSON_OTHER,
        STATUS_OTHER_HTTP,
        STATUS_404,
        STATUS_NETWORK,
    ]
    by_status: dict[str, list[dict]] = {s: [] for s in order}
    for r in results:
        by_status.setdefault(r["status"], []).append(r)

    print(f"\nProbed {len(results)} CKAN portals.\n")
    print(f"{'STATUS':<14} {'HTTP':<18} {'PORTAL':<42} {'ms':>6}  DETAIL")
    print("-" * 130)
    for status in order:
        for r in by_status.get(status, []):
            portal = r["portal"][:40]
            print(
                f"{status:<14} {r['http']:<18} {portal:<42} {r['ms']:>5}  {r['detail']}"
            )

    # ── Summary ──
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for status in order:
        n = len(by_status.get(status, []))
        if n:
            print(f"  {status:<14} {n:>3}")

    fixable = [r for r in results if r["status"] == STATUS_OK_VARIANT]
    if fixable:
        print(f"\nFixable (path variant works) — {len(fixable)} portal(s):")
        for r in fixable:
            print(f"  • {r['portal']}: {r['detail']}")

    dead = [r for r in results if r["status"] in (STATUS_404, STATUS_HTML, STATUS_NETWORK)]
    if dead:
        print(f"\nDead / needs investigation — {len(dead)} portal(s):")
        for r in dead:
            print(f"  • {r['portal']} [{r['status']}]: {r['detail']}")


if __name__ == "__main__":
    asyncio.run(main())
