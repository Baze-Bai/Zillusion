"""API-workflow validator checks — live endpoint probe + secret-leak scan.

The api analog of validator_browser.py (extraction's ground truth is the
live page; an api workflow's ground truth is the LIVE API RESPONSE) and the
secrets gate that extraction/download don't need (api workflow.py handles a
user-supplied credential).

  - probe_api_endpoint   -> dimension `endpoint_match` (+ raw preview for
                            the LLM's field-semantics judgment)
  - check_no_secret_leak -> dimension `secrets_safe`

**Isolation invariant**: writes land ONLY under validation/<run_id>/
(the probe saves the full response body there). Explore artifacts are
read-only throughout.

**Secret discipline**: credential values are loaded only to (a) sign the
probe request and (b) define the leak-scan needles; every returned/saved
string is masked. The full response body is saved to disk UNMASKED only
after the scan-needles check passes over it — if the API echoes the key,
the saved file is masked too.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx

from mcp_server.schemas import APIManifestFile
from runtime.validator_checks import PROJECT_ROOT, _load_records, _unwrap, _workspace

# Response-preview cap returned to the LLM (chars). The full body is always
# saved to disk — this only bounds what enters the model's context. The value
# is an operator decision (2026-06-10): not exposed as a tool parameter.
DEFAULT_PREVIEW_CHARS = 8000


# ── credentials ──────────────────────────────────────────────────────


def _credentials_path(site_id: str) -> Path:
    return PROJECT_ROOT / "inputs" / site_id / "credentials.json"


def _load_credentials(site_id: str) -> dict:
    p = _credentials_path(site_id)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _secret_leaves(obj: Any, prefix: str = "") -> list[tuple[str, str]]:
    """(key_path, value) for every string leaf long enough to be a secret.
    Short strings (< 8 chars) are skipped — scanning for them would flood
    false positives (e.g. 'none', 'api')."""
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_secret_leaves(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_secret_leaves(v, f"{prefix}[{i}]"))
    elif isinstance(obj, str) and len(obj) >= 8:
        out.append((prefix, obj))
    return out


def _mask(secret: str) -> str:
    return f"{secret[:3]}…{secret[-3:]}" if len(secret) > 8 else "***"


def _mask_all(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, _mask(s))
    return text


# ── dimension: endpoint_match ────────────────────────────────────────


def _apply_auth(url: str, headers: dict, ep_auth, creds: dict) -> tuple[str, dict, str | None]:
    """Sign (url, headers) per the manifest's auth block. Returns the masked
    key actually used (None = unauthenticated call)."""
    if ep_auth is None or ep_auth.type == "none":
        return url, headers, None
    extra = creds.get("extra") if isinstance(creds.get("extra"), dict) else {}
    key = creds.get("api_key") or extra.get("access_token") or ""
    if not key:
        return url, headers, None  # no credential available — probe unauthenticated
    if ep_auth.type == "api_key":
        param = ep_auth.param_name or "X-Api-Key"
        if ep_auth.location == "query":
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{param}={key}"
        else:
            headers[param] = key
    elif ep_auth.type == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif ep_auth.type == "basic":
        import base64

        user = str(extra.get("username") or key)
        pw = str(extra.get("password") or "")
        headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    return url, headers, _mask(key)


def _records_at_path(data: Any, record_path: str | None):
    """Walk a dot-path to the record list; fall back to _unwrap heuristics."""
    if record_path:
        node = data
        for part in record_path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                node = None
                break
        if isinstance(node, list):
            return [r for r in node if isinstance(r, dict)]
    recs = _unwrap(data)
    if isinstance(recs, list):
        return [r for r in recs if isinstance(r, dict)]
    return None


def _scalar_needles(record: dict) -> dict[str, list[str]]:
    """field -> candidate needle strings for the live body. Skips bools/None/
    containers and values shorter than 3 chars (too noisy). String values get
    BOTH the raw form and the JSON-escaped form (the body is raw JSON text —
    a value containing newlines/quotes appears there escaped, and matching
    only the raw form would systematically miss multi-line fields)."""
    out: dict[str, list[str]] = {}
    for k, v in record.items():
        if isinstance(v, bool) or v is None or isinstance(v, (list, dict)):
            continue
        s = str(v)
        if len(s) < 3:
            continue
        cands = [s]
        if isinstance(v, str):
            escaped = json.dumps(v, ensure_ascii=False)[1:-1]  # as it appears inside JSON text
            if escaped != s:
                cands.append(escaped)
        out[k] = cands
    return out


def _identifier_present(body: str, field: str, value: Any) -> bool:
    """Is the record's identifier in the live body? Matched as a JSON
    `"field": value` pair, not a bare substring — short identifiers (small
    int ids) would otherwise be either skipped by the needle length floor or
    drowned in false positives."""
    if value is None or isinstance(value, (list, dict)):
        return False
    if isinstance(value, bool):
        pat = rf'"{re.escape(field)}"\s*:\s*{str(value).lower()}'
    elif isinstance(value, (int, float)):
        pat = rf'"{re.escape(field)}"\s*:\s*{re.escape(str(value))}(?=[,\}}\s\]])'
    else:
        pat = rf'"{re.escape(field)}"\s*:\s*"{re.escape(str(value))}"'
    return re.search(pat, body) is not None


async def probe_api_endpoint(
    site_id: str,
    run_id: str,
    *,
    endpoint_index: int = 0,
    sample_size: int = 3,
    max_preview_chars: int = DEFAULT_PREVIEW_CHARS,
    timeout_s: float = 30.0,
) -> dict:
    """ONE live call to the manifest's declared probe_url; ground-truth the
    workflow's output against the real response.

    Returns request facts (status / timing / rate-limit headers), where the
    full body was saved (validation/<run_id>/samples/), a bounded
    raw_preview for field-semantics judgment, and `value_match`: for up to
    `sample_size` records of explore's output_sample.json, which stringified
    field values (and the identifier_field specifically) appear in the live
    body. Low hit ratios can be legitimate transforms — the caller judges
    from raw_preview, this only reports facts. Single request by design
    (rate-limit etiquette)."""
    ws = _workspace(site_id)
    manifest = APIManifestFile.load(ws / "api_manifest.yaml")  # loud if missing
    if not (0 <= endpoint_index < len(manifest.endpoints)):
        return {
            "error": f"endpoint_index {endpoint_index} out of range (have {len(manifest.endpoints)})"
        }
    ep = manifest.endpoints[endpoint_index]

    creds = _load_credentials(site_id)
    secrets = [v for _, v in _secret_leaves(creds)]
    url, headers, used_key = _apply_auth(ep.probe_url, {}, ep.auth, creds)

    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
        try:
            resp = await client.request(ep.method or "GET", url, headers=headers)
        except httpx.HTTPError as exc:
            return {
                "error": f"request failed: {type(exc).__name__}: {_mask_all(str(exc), secrets)}",
                "probe_url": _mask_all(url, secrets),
            }

    body = resp.text or ""
    # Save the WHOLE body (masked if the API echoed the key) under validation/.
    samples_dir = ws / "validation" / run_id / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    saved = samples_dir / f"probe_ep{endpoint_index}.json"
    saved.write_text(_mask_all(body, secrets), encoding="utf-8")

    rate_headers = {
        k: v
        for k, v in resp.headers.items()
        if "ratelimit" in k.lower().replace("-", "") or k.lower() == "retry-after"
    }

    records_found: int | None = None
    try:
        records = _records_at_path(json.loads(body), ep.record_path)
        records_found = len(records) if records is not None else None
    except (json.JSONDecodeError, ValueError):
        pass

    # value_match: explore's shipped sample vs the live body.
    output = _load_records(ws / "output_sample.json")
    value_match: dict[str, Any] = {"output_records_checked": 0}
    if output:
        sampled = output[:sample_size]
        identifier = manifest.identifier_field
        ids_found = 0
        per_record: list[float] = []
        miss_counts: dict[str, int] = {}
        for rec in sampled:
            if identifier and _identifier_present(body, identifier, rec.get(identifier)):
                ids_found += 1
            needles = _scalar_needles(rec)
            if not needles:
                continue
            hits = {f: any(c in body for c in cands) for f, cands in needles.items()}
            per_record.append(sum(hits.values()) / len(hits))
            for f, hit in hits.items():
                if not hit:
                    miss_counts[f] = miss_counts.get(f, 0) + 1
        checked = len(sampled)
        value_match = {
            "output_records_checked": checked,
            "identifier_field": identifier,
            "identifiers_found_in_response": ids_found,
            "per_record_value_hit_ratio": [round(r, 3) for r in per_record],
            "fields_never_found": sorted(f for f, n in miss_counts.items() if n == checked),
        }

    return {
        "endpoint_index": endpoint_index,
        "probe_url": _mask_all(url, secrets),
        "method": ep.method or "GET",
        "authenticated_as": used_key,  # masked key or None
        "status_code": resp.status_code,
        "elapsed_ms": round(resp.elapsed.total_seconds() * 1000, 1),
        "content_type": resp.headers.get("content-type"),
        "body_chars": len(body),
        "saved_to": str(saved),
        "records_found": records_found,
        "record_path": ep.record_path,
        "rate_limit_headers": rate_headers,
        "raw_preview": _mask_all(body[:max_preview_chars], secrets),
        "value_match": value_match,
    }


# ── dimension: secrets_safe ──────────────────────────────────────────

# Shipped artifacts — a credential literal in any of these gates FAIL.
_GATING_SCAN_FILES = ("workflow.py", "helpers.py", "api_manifest.yaml", "output_sample.json")
# Working notes — a leak here is reported but advisory (they don't ship).
_ADVISORY_SCAN_FILES = ("exploration_log.md", "task_plan.md", "iter_summary.md")


def check_no_secret_leak(site_id: str, run_id: str) -> dict:
    """Scan shipped artifacts (+ the isolated rerun output) for credential
    literals from inputs/<site>/credentials.json. Dimension: secrets_safe.
    Returns na=True when the site has no credentials (open API) — record
    the dimension as n/a in that case."""
    creds = _load_credentials(site_id)
    leaves = _secret_leaves(creds)
    if not leaves:
        return {"ok": True, "na": True, "reason": "no credentials.json (open API) — record n/a"}

    ws = _workspace(site_id)
    targets: list[tuple[Path, bool]] = [(ws / f, True) for f in _GATING_SCAN_FILES]
    targets.append((ws / "validation" / run_id / "rerun" / "output_sample.json", True))
    targets.extend((ws / f, False) for f in _ADVISORY_SCAN_FILES)

    leaks: list[dict] = []
    advisory: list[dict] = []
    scanned: list[str] = []
    for path, gating in targets:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        scanned.append(path.name)
        for key_path, secret in leaves:
            n = text.count(secret)
            if n:
                entry = {
                    "file": str(path.relative_to(ws)),
                    "credential_key": key_path,
                    "masked": _mask(secret),
                    "count": n,
                }
                (leaks if gating else advisory).append(entry)

    return {
        "ok": not leaks,
        "na": False,
        "secrets_checked": len(leaves),
        "files_scanned": scanned,
        "leaks": leaks,
        "advisory_leaks": advisory,
    }


__all__ = ["probe_api_endpoint", "check_no_secret_leak", "DEFAULT_PREVIEW_CHARS"]
