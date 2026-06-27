"""Session-secret leak scan for the agentic / takeover path.

The deterministic API validator (``validator_api.check_no_secret_leak``) scans
shipped artifacts for ``inputs/<site>/credentials.json`` literals. That file does
NOT exist on a takeover / agentic run — there the secrets are the LIVE SESSION
cookies + localStorage tokens that ``browser_save_auth`` merged into
``auth_state.json`` (see ``mcp_server.browser._merge_storage_state``). This module
pulls the needles from THAT file and scans the agentic run's shipped artifacts.

Mechanical + spoof-proof: the verdict is re-derived from disk bytes, never the
agent's say-so. ``runtime.crawl_emit.finalize_crawl`` sets ``secrets_safe`` from
``scan_run_secrets``; ``runtime.run_tools.update_run_manifest`` re-runs the scan
as a HARD FLOOR so the dimension can never be recorded pass/n-a over a real leak.
"""

from __future__ import annotations

import json
from pathlib import Path

from runtime.validator_checks import resolve_auth_path

# Files under runs/<run_id>/ that ship or are read downstream — a session-secret
# literal in any of these is a leak. (auth_state.json is deliberately NOT here:
# it is the needle SOURCE, not a shipped deliverable — staging it out of the run
# tree is a separate concern.)
_GATING_RUN_ARTIFACTS = (
    "records.jsonl",
    "output.json",
    "manifest.yaml",
    "report.md",
    "feedback.yaml",
    "crawl_stdout.log",
)

# Skip short cookie/token values — a 1-6 char value ("1", "true", "en") would
# false-positive against ordinary record text. Real session secrets are long.
_MIN_NEEDLE_LEN = 8


def auth_state_needles(auth_path: Path) -> list[str]:
    """Every cookie value + localStorage value in an ``auth_state.json``, length
    >= ``_MIN_NEEDLE_LEN``. Empty when the file is absent / unparseable / carries
    no long secret (no takeover happened). Longest-first so the more-specific
    needle is reported when several overlap."""
    if not auth_path.exists():
        return []
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for c in data.get("cookies") or []:
        v = c.get("value") if isinstance(c, dict) else None
        if isinstance(v, str) and len(v) >= _MIN_NEEDLE_LEN:
            out.append(v)
    for origin in data.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        for kv in origin.get("localStorage") or []:
            v = kv.get("value") if isinstance(kv, dict) else None
            if isinstance(v, str) and len(v) >= _MIN_NEEDLE_LEN:
                out.append(v)
    return sorted(set(out), key=len, reverse=True)


def scan_run_secrets(run_dir: Path, auth_path: Path | None = None) -> dict:
    """Scan a run's shipped artifacts for ``auth_state.json`` session-secret
    literals. Returns ``{ok, na, leaks, needle_count, scanned}``.

    ``na=True`` (record ``secrets_safe`` n/a) when there are no session secrets to
    look for — no ``auth_state.json`` or it carries no long value (the run used no
    takeover auth). ``ok`` is ``not leaks``. Each leak is
    ``{"file", "count", "needle_len}`` — the needle VALUE is never echoed, only
    its length, so the scan result itself can't leak the secret."""
    auth = auth_path if auth_path is not None else resolve_auth_path()
    needles = auth_state_needles(auth)
    if not needles:
        return {
            "ok": True,
            "na": True,
            "reason": "no auth_state.json session secrets (no takeover auth) — record n/a",
            "leaks": [],
        }
    leaks: list[dict] = []
    scanned: list[str] = []
    for name in _GATING_RUN_ARTIFACTS:
        p = run_dir / name
        if not p.exists():
            continue
        scanned.append(name)
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for secret in needles:
            n = text.count(secret)
            if n:
                leaks.append({"file": name, "count": n, "needle_len": len(secret)})
    return {
        "ok": not leaks,
        "na": False,
        "leaks": leaks,
        "needle_count": len(needles),
        "scanned": scanned,
    }


__all__ = ["auth_state_needles", "scan_run_secrets"]
