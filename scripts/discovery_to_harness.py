"""Bridge: Zillusion discovery output → harness runs.

Generalizes ``scripts/crawl_reddit_for_harness.py`` (a reddit one-off that
only built a seed for a hardcoded URL). Given a completed discovery run's
output (a list of ``DataSource`` dicts) + the original user query, this:

  1. **Stages** one harness input bundle per harness-bound source:
     ``embedded`` → an EXTRACTION goal (``goal.md`` + ``seed.json``);
     ``file`` → a DOWNLOAD goal (same bundle); ``api`` → an API goal
     (``goal.md`` + ``api_spec.json``, the merged spec that is also the
     input-side type marker; plus ``credentials_needed.json`` when a key is
     required but absent). The discovered type is only a STARTING
     HYPOTHESIS — goal.md tells the agent to verify it and switch workflow
     type if the real shape differs (api runs may only switch to download).
  2. **Defers** unknown-typed sources to ``inputs/_deferred/<site_id>.json``
     placeholder records — kept visible, never silently dropped.
  3. **Launches** one harness ``explore-loop`` process per READY staged site,
     with a bounded concurrency cap (default 2). api sites are launch-GATED:
     they run only when the API needs no key or the user has written
     ``inputs/<site>/credentials.json`` (api sites also default to
     ``--max-iters 2``). Each run is isolated; the harness uses its OWN
     venv python.
  4. Writes a ``inputs/_handoff_manifest.json`` summarizing staged /
     awaiting-credentials / deferred / launch results.

Design notes:
  - goal.md is generated DETERMINISTICALLY (no LLM). The user query becomes the
    Goal prose; the field list is drawn from the source's discovered page-tree /
    embedded spec and flagged as a HYPOTHESIS — the harness decides + verifies
    the real field set (see hypothesis-loop SKILL.md + runtime/validate.py).
  - Operates on the JSON form of ``src.models.data_source.DataSource`` so this
    bridge stays decoupled from the backend's deps. Field names mirror that model.

    # stage + auto-launch (concurrency 2):
    python scripts/discovery_to_harness.py <discovery_output.json> "<user query>"
    # stage only (review before spending):
    python scripts/discovery_to_harness.py <discovery_output.json> "<query>" --no-launch
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS_DIR = ROOT / "harness"
HARNESS_INPUTS = HARNESS_DIR / "inputs"
# The harness MUST run on its own venv python (see harness CLAUDE.md).
HARNESS_PY = HARNESS_DIR / ".venv" / "Scripts" / "python.exe"

# DataSourceType.EMBEDDED_DATA value (src/models/data_source.py).
EMBEDDED = "embedded"
DEFERRED_DIRNAME = "_deferred"

# Source type → harness workflow type. All three kinds go to the harness; the
# discovered type is only a STARTING HYPOTHESIS the explore agent verifies
# (and may switch within the pivot rules). Unknown types are deferred.
# api sites are STAGED always but LAUNCHED only when credentials are present
# (or the API needs none) — see needs_credentials / main()'s gating.
_WORKFLOW_TYPE = {"embedded": "extraction", "file": "download", "api": "api"}

# Failure-side iteration cap for api explore-loops (operator decision
# 2026-06-10): API exploration has no browser wrangling and usually converges
# in one iter; a third failing iter rarely rescues. Extraction/download keep
# the CLI default. An explicit --max-iters passthrough overrides this.
API_DEFAULT_MAX_ITERS = 2

# DataPageNode.page_type → harness seed node_type.
_PAGE_TYPE_TO_NODE_TYPE = {"list": "list_page", "detail": "detail_page"}


# ── identity ─────────────────────────────────────────────────────────


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or "source"


def site_id_for(source: dict) -> str:
    """Filesystem-safe, distinct site id for a source-PRODUCT.

    ``<provider-or-name-or-host slug>-<6-hex digest>``. The digest folds in the
    source ``id`` + ``url`` + ``source_type``, so two co-equal products at the
    SAME url (e.g. an embedded table AND a downloadable file on one dataset
    page) get DISTINCT ids instead of clobbering each other's inputs/<site>/.
    Deterministic for a given discovery output (re-staging the same report is
    idempotent).
    """
    label = source.get("provider") or source.get("name") or ""
    if not label:
        host = re.sub(r"^https?://", "", source.get("url", "")).split("/")[0]
        label = host
    # Compose the key from id + url + source_type so distinct products never
    # collide: differing id OR differing type is enough to separate them.
    key = "|".join(
        [
            source.get("id") or "",
            source.get("url") or "",
            (source.get("source_type") or "").lower(),
        ]
    ).strip("|") or label
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:6]
    return f"{_slugify(label)}-{digest}"


# ── seed.json (page tree) ────────────────────────────────────────────


def _node_to_harness(node: dict | None, seed_url: str) -> dict | None:
    """DataPageNode dict → harness seed page-tree node.

    Same mapping as the reddit precedent: url→url_pattern, page_type→node_type,
    keep is_sampled / fields_available / children (recursed).
    """
    if not node:
        return None
    return {
        "url_pattern": node.get("url") or seed_url,
        "node_type": _PAGE_TYPE_TO_NODE_TYPE.get(node.get("page_type"), "list_page"),
        "is_sampled": bool(node.get("is_sampled", False)),
        "fields_available": list(node.get("fields_available") or []),
        "children": [
            c
            for c in (_node_to_harness(ch, seed_url) for ch in (node.get("children") or []))
            if c
        ],
    }


def build_seed(source: dict) -> dict:
    """Build harness ``seed.json`` from a discovered EMBEDDED source.

    Uses the source's discovered page-tree (``embedded_spec.page_tree.root``);
    falls back to a single ``list_page`` node at the source URL when absent.
    ``fields_available`` is carried through as a HYPOTHESIS — the harness
    probes each before trusting it.
    """
    seed_url = source.get("url", "")
    spec = source.get("embedded_spec") or {}
    tree = spec.get("page_tree") or {}
    harness_root = _node_to_harness(tree.get("root"), seed_url)
    if harness_root is None:
        harness_root = {
            "url_pattern": seed_url,
            "node_type": "list_page",
            "is_sampled": False,
            "fields_available": list(spec.get("fields_present") or []),
            "children": [],
        }
    notes = (
        "Auto-generated by scripts/discovery_to_harness.py from a Zillusion "
        "discovery run. fields_available is a HYPOTHESIS inferred from discovery "
        "(page card schema / embedded spec), NOT verified by extraction — the "
        "harness must probe each before trusting it."
    )
    summary = tree.get("tree_summary")
    if summary:
        notes += f" Tree summary: {summary}"
    return {"seed_url": seed_url, "page_tree": {"root": harness_root}, "notes": notes}


# ── api_spec.json (merged canonical spec for api sites) ─────────────


def _first(*vals):
    """First value that isn't None/''/[]/{} — merge precedence helper."""
    return next((v for v in vals if v not in (None, "", [], {})), None)


def _normalize_auth_type(raw, access_level: str | None) -> str:
    """Free-text auth hint → canonical token (api_key/oauth/hmac/none/unknown).

    Sources disagree on where auth info lives AND how it's phrased
    (APISpec.auth_type is already canonical-ish; the local directory's
    metadata.auth_method is prose like 'API Key (free tier)')."""
    s = str(raw or "").strip().lower()
    if not s or s == "unknown":
        # An explicitly-open source with no auth hint almost certainly needs
        # no key; anything else stays unknown (→ gated, conservative).
        return "none" if (access_level or "").lower() == "open" else "unknown"
    if "oauth" in s:
        return "oauth"
    if "hmac" in s:
        return "hmac"
    if s in ("none", "no", "open", "free", "public") or "no key" in s or "no auth" in s:
        return "none"
    if "key" in s or "token" in s or "bearer" in s:
        return "api_key"
    return "unknown"


def build_api_spec(source: dict) -> dict:
    """Merged canonical ``inputs/<site>/api_spec.json`` for an api source.

    Field-level merge, first-present wins: ``source.api_spec`` (web-discovered
    sources carry it, often sparse) → ``source.metadata`` (the local API
    directory adapters put signup_url/auth_method/openapi_url/free-tier info
    THERE with api_spec=None) → top-level DataSource fields → null. The raw
    inputs ride along under ``raw`` so nothing is lost in the merge.
    """
    a = source.get("api_spec") or {}
    m = source.get("metadata") or {}
    access_level = source.get("access_level")
    auth_type = _normalize_auth_type(_first(a.get("auth_type"), m.get("auth_method")), access_level)
    site_id = site_id_for(source)
    spec = {
        "workflow_type": "api",
        "site_id": site_id,
        "source": {
            "name": source.get("name"),
            "provider": source.get("provider"),
            "url": source.get("url"),
            "description": source.get("description"),
        },
        "endpoint": _first(a.get("endpoint"), source.get("url")),
        "method": a.get("method") or "GET",
        "auth": {
            "type": auth_type,
            "location": a.get("auth_location"),
            "param_name": a.get("auth_param_name"),
            "raw_method_note": _first(m.get("auth_method"), a.get("auth_type")),
        },
        "signup": {
            "url": _first(a.get("signup_url"), m.get("signup_url")),
            "instructions": a.get("signup_instructions"),
            "steps": list(m.get("signup_steps") or []),
        },
        "docs": {
            "documentation_url": _first(a.get("documentation_url"), m.get("docs_url"), source.get("url")),
            "openapi_spec_url": _first(a.get("openapi_spec_url"), m.get("openapi_url")),
        },
        "sdk": {
            "has_sdk": bool(_first(a.get("has_sdk"), m.get("sdk_languages"))),
            "languages": list(m.get("sdk_languages") or []),
            "example_code": a.get("example_code"),
        },
        "access": {
            "access_level": access_level,
            "license": source.get("license"),
            "pricing": _first(source.get("pricing"), m.get("paid_starting_price")),
            "pricing_url": m.get("pricing_url"),
            "rate_limit": source.get("rate_limit"),
            "free_tier_limit": m.get("free_tier_limit"),
            "has_free_tier": m.get("has_free_tier"),
        },
        "field_hypotheses": list(m.get("fields_present") or []),
        "raw": {"api_spec": source.get("api_spec"), "metadata": m},
    }
    spec["credentials"] = {
        "required": auth_type not in ("none",),
        "file": f"inputs/{site_id}/credentials.json",
        "shape": {"api_key": "<your key>", "extra": {}},
    }
    return spec


def needs_credentials(spec: dict) -> bool:
    """Launch gate: True unless the API needs no key (auth none — which
    _normalize_auth_type already grants to hint-less OPEN sources). unknown
    stays gated, conservative: probing a keyed API without a key just burns
    an iteration to learn what discovery already suspected."""
    return (spec.get("auth") or {}).get("type") != "none"


# ── goal.md ──────────────────────────────────────────────────────────


def _hypothesis_fields(source: dict) -> list[str]:
    """Best-guess field list (a hypothesis) from the source's discovered shape.

    Priority: per-level field_progression (list_page then detail_page) →
    root node fields_available → embedded_spec.fields_present. Deduped, order
    preserved.
    """
    spec = source.get("embedded_spec") or {}
    tree = spec.get("page_tree") or {}
    out: list[str] = []

    def _add(items):
        for f in items or []:
            if f not in out:
                out.append(f)

    prog = tree.get("field_progression") or {}
    _add(prog.get("list_page"))
    _add(prog.get("detail_page"))
    _add((tree.get("root") or {}).get("fields_available"))
    _add(spec.get("fields_present"))
    return out


def _constraints(source: dict) -> list[str]:
    out: list[str] = []
    access = (source.get("access_level") or "unknown").lower()
    if access == "open":
        out.append("No login required (discovered access level: open).")
    elif access in ("free_reg", "api_key_free", "api_key_paid", "oauth", "paywall"):
        out.append(
            f"Access may be gated (discovered access level: {access}) — probe for "
            "a login/consent wall before assuming the data is public."
        )
    else:
        out.append("Access level unknown — probe for a login/consent wall before extracting.")
    if source.get("license"):
        out.append(f"License (discovered): {source['license']} — respect it.")
    return out


# How to describe a sibling category (another product of the SAME source,
# handled by its own run/consumer) in a goal.md so an agent stays in its lane.
_CATEGORY_PHRASE = {
    "embedded": "embedded page data (an extraction run handles it)",
    "file": "a downloadable file (a download run handles it)",
    "api": "an API (an api run handles it)",
}


def _type_hypothesis_note(workflow_type: str, sibling_categories: list[str]) -> list[str]:
    """The 'Workflow type' block of goal.md.

    Single-category source: the discovered type is a hypothesis — SWITCH if it
    proves mis-classified (api runs may only switch to download: their browser
    is disabled, so api→extraction is reported, not pivoted). Multi-category
    source (siblings present): the other kinds are owned by SEPARATE runs, so
    STAY IN YOUR LANE — don't pivot to or duplicate a sibling's data; only flag
    if YOUR assigned type doesn't exist. (This prevents the failure where a
    file-run sees embedded data, wrongly pivots to extraction, and the file
    workflow never gets built.)
    """
    assigned = {
        "download": "a downloadable file",
        "api": "an HTTP API",
    }.get(workflow_type, "embedded page data")
    this_wf = {"download": "DOWNLOAD", "api": "API"}.get(workflow_type, "EXTRACTION")
    lines = ["", "## Workflow type — verify (this is a hypothesis)", ""]
    if sibling_categories:
        others = ", ".join(_CATEGORY_PHRASE.get(c, c) for c in sibling_categories)
        lines.append(
            f"This source has MULTIPLE kinds of data. You are assigned **{assigned}** → build a "
            f"{this_wf} workflow. The other kind(s) — {others} — are owned by SEPARATE runs. "
            f"**Stay in your lane:** even if you notice that other data while probing, do NOT "
            f"extract/download it and do NOT switch your workflow type — a sibling run handles it. "
            f"Only if your assigned {assigned} turns out NOT to exist on this source at all, emit "
            f"INCONCLUSIVE with that reason instead of silently pivoting."
        )
    elif workflow_type == "api":
        lines.append(
            "This source was discovered as **an HTTP API**, but the discovered type is a "
            "HYPOTHESIS, not a fact — confirm by probing (browser tools are disabled in this "
            "run; probe over HTTP per the `api-probe` skill). If the API actually hands you a "
            "downloadable bulk file (a stable CSV/JSON dump URL you should fetch whole), SWITCH "
            "to a DOWNLOAD workflow: produce `download_manifest.yaml` + `downloads/`. If the "
            "data turns out to be page-embedded only (no real API), do NOT pivot to extraction "
            "— report via `report_off_goal` and emit INCONCLUSIVE."
        )
    elif workflow_type == "download":
        lines.append(
            "This source was discovered as **a downloadable file**, but the discovered type is a "
            "HYPOTHESIS, not a fact — confirm by probing. If the data turns out to be **embedded in "
            "the page** (HTML tables / inline JSON) rather than a downloadable file, SWITCH to an "
            "EXTRACTION workflow: produce `selectors.yaml` + records instead of a download."
        )
    else:
        lines.append(
            "This source was discovered as **embedded page data**, but the discovered type is a "
            "HYPOTHESIS, not a fact — confirm by probing. If the data turns out to be **a "
            "downloadable file** (a CSV/JSON/XLSX/… you should fetch whole) rather than embedded "
            "page data, SWITCH to a DOWNLOAD workflow: produce `download_manifest.yaml` + "
            "`downloads/` instead of records."
        )
    lines.append("")
    lines.append(
        "Found a DIFFERENT product (not your assigned one, not a sibling's — a different URL "
        "or a category nobody here owns)? Don't handle it inline; report it via "
        "`report_discovered_source(url, types, note)` so it gets its own run."
    )
    lines.append("")
    return lines


def build_goal_md(source: dict, user_query: str, workflow_type: str = "extraction",
                  sibling_categories: list[str] | None = None) -> str:
    """Deterministic ``goal.md`` from the user query + discovered source info.

    ``workflow_type`` ('extraction' | 'download') is the STARTING hypothesis —
    goal.md tells the agent to verify it and switch if the real shape differs.
    The extraction field list / download target are likewise hypotheses the
    harness verifies (completeness converges over rounds, never a 1-pass FAIL).
    """
    provider = source.get("provider") or source.get("name") or "the source"
    url = source.get("url", "")
    spec = source.get("embedded_spec") or {}
    desc = (source.get("description") or "").strip()

    lines = ["# Goal", "", f"User need: {user_query.strip()}", ""]

    if workflow_type == "download":
        fspec = source.get("file_spec") or {}
        dl_url = fspec.get("download_url") or url
        fmt = fspec.get("file_format") or "unknown"
        cols = fspec.get("column_headers")
        lines.append(f"Produce a **download workflow** for the file data source at **{provider}** ({url}).")
        if desc:
            lines += ["", desc]
        lines += _type_hypothesis_note("download", sibling_categories or [])
        lines += [
            "## Download target (hypothesis)",
            "",
            f"- Download URL (discovered): {dl_url}",
            f"- Format (discovered): {fmt}",
        ]
        if cols:
            lines.append(f"- Columns (discovered — verify): {', '.join(cols)}")
        lines += [
            "",
            "## What the workflow must do",
            "",
            "- Actually download the file(s) to `downloads/` — fetch the bytes, do NOT just record the URL.",
            "- Verify each: non-empty, the format matches, and it parses (csv rows / json loads / xlsx opens / …).",
            "- Declare **only the actual data files** (csv / json / xlsx / … payloads) in",
            "  `download_manifest.yaml` via the `download_manifest_write` tool (the download analog of",
            "  selectors.yaml) so the validator can re-download + verify each — do NOT declare generator",
            "  scripts, READMEs, or other helper files, even if you ran a script to produce the data.",
        ]
    elif workflow_type == "api":
        aspec = build_api_spec(source)
        auth, signup, docs, access = aspec["auth"], aspec["signup"], aspec["docs"], aspec["access"]
        lines.append(f"Produce an **API crawl workflow** for **{provider}** ({url}).")
        if desc:
            lines += ["", desc]
        lines += _type_hypothesis_note("api", sibling_categories or [])
        lines += ["## API (discovered — hypotheses)", ""]
        lines.append(f"- Endpoint (discovered): {aspec['endpoint']}")
        if aspec.get("method") and aspec["method"] != "GET":
            lines.append(f"- Method: {aspec['method']}")
        auth_bits = [f"type={auth['type']}"]
        if auth.get("location"):
            auth_bits.append(f"location={auth['location']}")
        if auth.get("param_name"):
            auth_bits.append(f"param={auth['param_name']}")
        if auth.get("raw_method_note"):
            auth_bits.append(f"note: {auth['raw_method_note']}")
        lines.append(f"- Auth (discovered — verify by probing): {', '.join(auth_bits)}")
        if docs.get("documentation_url"):
            lines.append(f"- Docs: {docs['documentation_url']}")
        if docs.get("openapi_spec_url"):
            lines.append(f"- OpenAPI spec: {docs['openapi_spec_url']}")
        if access.get("rate_limit"):
            lines.append(f"- Rate limit (discovered): {access['rate_limit']}")
        if access.get("free_tier_limit"):
            lines.append(f"- Free tier limit: {access['free_tier_limit']}")
        if aspec["sdk"].get("has_sdk"):
            langs = ", ".join(aspec["sdk"]["languages"]) or "yes"
            lines.append(f"- Official SDK: {langs} (an SDK existing ≠ you must use it — plain httpx is fine)")
        lines += ["", "## Credentials", ""]
        if not needs_credentials(aspec):
            lines.append(
                "- No key appears to be required (discovered auth: none) — verify with an "
                "unauthenticated call first."
            )
        else:
            lines += [
                f"- The user-supplied key lives at `inputs/{aspec['site_id']}/credentials.json` "
                f'(shape: {{"api_key": "...", "extra": {{...}}}}). The validator and the '
                f"production runner copy it next to workflow.py.",
                "- workflow.py must read it via env `API_KEY` → credentials.json walk-up (use the "
                "`_find_credentials()` snippet from the `api-probe` skill).",
                "- NEVER hardcode, print, or write the key value into any artifact, log, or command "
                "string — a secret-leak scan gates validation.",
            ]
            if signup.get("url"):
                lines.append(f"- Signup (for reference — the USER obtains keys, not you): {signup['url']}")
            if signup.get("instructions"):
                lines.append(f"- Signup notes: {signup['instructions']}")
            for step in (signup.get("steps") or [])[:6]:
                lines.append(f"  - {step}")
        fields = list(aspec.get("field_hypotheses") or [])
        lines += ["", "## Required fields", ""]
        if fields:
            lines += [f"- `{f}`" for f in fields]
        else:
            lines.append("- (none discovered — determine the extractable field set by probing)")
        lines += [
            "",
            "> The field list above is a HYPOTHESIS inferred from discovery, NOT a",
            "> verified or user-fixed schema. Probe each field against the live API;",
            "> drop unextractable ones, add ones you find, fix any whose real meaning",
            "> differs. Field completeness converges over explore↔validate rounds.",
        ]
    else:
        data_shape = spec.get("data_shape") or "data"
        lines.append(f"Extract the {data_shape} described below from **{provider}** ({url}).")
        if desc:
            lines += ["", desc]
        lines += _type_hypothesis_note("extraction", sibling_categories or [])
        fields = _hypothesis_fields(source)
        lines += ["## Required fields", ""]
        if fields:
            lines += [f"- `{f}`" for f in fields]
        else:
            lines.append("- (none discovered — determine the extractable field set by probing)")
        lines += [
            "",
            "> The field list above is a HYPOTHESIS inferred from discovery, NOT a",
            "> verified or user-fixed schema. Probe each field against the live page;",
            "> drop unextractable ones, add ones you find, fix any whose real meaning",
            "> differs. Field completeness converges over explore↔validate rounds.",
        ]

    # Shared scope + constraints.
    if workflow_type == "api":
        lines += ["", "## Scope", "", f"- API base / seed endpoint: {url}"]
        lines += [
            "- Discover pagination by probing; sample mode (CRAWL_MODE=sample) crawls 1-2 pages, "
            "full mode the goal's complete scope.",
            "- Heartbeat: print a one-line progress update at least every ~60s, INCLUDING during "
            "rate-limit sleeps (the production runner kills 5-minute stdout silences).",
        ]
    else:
        lines += ["", "## Scope", "", f"- Seed page: {url}"]
        if workflow_type == "download":
            lines.append("- The seed may be a landing/dataset page — follow links to the actual file if needed.")
        else:
            has_detail = bool((spec.get("page_tree") or {}).get("root", {}).get("children"))
            lines.append(
                "- List → detail structure: enrich listing records by visiting detail pages."
                if has_detail
                else "- Single listing/page level unless probing reveals deeper structure."
            )
    if source.get("temporal_coverage"):
        lines.append(f"- Temporal coverage (discovered): {source['temporal_coverage']}")
    if source.get("geographic_coverage"):
        lines.append(f"- Geographic coverage (discovered): {', '.join(source['geographic_coverage'])}")
    lines += ["", "## Constraints", ""]
    lines += [f"- {c}" for c in _constraints(source)]
    if workflow_type == "api":
        lines.append(
            "- Respect the API's rate limit: observe X-RateLimit-*/Retry-After headers while "
            "probing, sleep between calls accordingly, and record the pacing in api_manifest.yaml."
        )
    lines.append("")
    return "\n".join(lines)


# ── staging ──────────────────────────────────────────────────────────


def stage_source(
    source: dict, user_query: str, workflow_type: str = "extraction", inputs_dir: Path = HARNESS_INPUTS,
    sibling_categories: list[str] | None = None,
) -> str:
    """Write one harness input bundle and return the site_id.

    extraction/download: ``inputs/<site_id>/{goal.md, seed.json}``.
    api: ``inputs/<site_id>/{goal.md, api_spec.json}`` (api_spec.json is also
    the input-side type marker the explore runtime keys on) — plus
    ``credentials_needed.json`` when the API needs a key the user hasn't
    supplied yet (the durable awaiting marker main()'s launch gate + the
    future backend read). ``sibling_categories`` are the other categories of
    the SAME source (handled by sibling runs) — surfaced in goal.md so the
    agent stays in its lane."""
    site_id = site_id_for(source)
    d = inputs_dir / site_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "goal.md").write_text(
        build_goal_md(source, user_query, workflow_type, sibling_categories), encoding="utf-8"
    )
    if workflow_type == "api":
        aspec = build_api_spec(source)
        (d / "api_spec.json").write_text(
            json.dumps(aspec, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if needs_credentials(aspec) and not (d / "credentials.json").exists():
            needed = {
                "site_id": site_id,
                "auth_type": aspec["auth"]["type"],
                "signup_url": aspec["signup"]["url"],
                "signup_instructions": aspec["signup"]["instructions"],
                "signup_steps": aspec["signup"]["steps"],
                "write_to": f"inputs/{site_id}/credentials.json",
                "shape": aspec["credentials"]["shape"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            (d / "credentials_needed.json").write_text(
                json.dumps(needed, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    else:
        (d / "seed.json").write_text(
            json.dumps(build_seed(source), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return site_id


def api_site_ready(site_id: str, inputs_dir: Path = HARNESS_INPUTS) -> bool:
    """Launch gate for a staged api site: ready when the API needs no key or
    the user has written inputs/<site>/credentials.json."""
    d = inputs_dir / site_id
    if (d / "credentials.json").exists():
        return True
    try:
        aspec = json.loads((d / "api_spec.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False  # unreadable spec — stay gated
    return not needs_credentials(aspec)


def stage_deferred(source: dict, inputs_dir: Path = HARNESS_INPUTS) -> str:
    """Write a placeholder record for an UNKNOWN-typed source.

    embedded/file/api all stage as harness sites now — this path remains only
    for source types the harness has no workflow for, kept visible (under
    ``inputs/_deferred/``) so nothing is silently dropped. Returns the site_id.
    """
    site_id = site_id_for(source)
    stype = (source.get("source_type") or "unknown").lower()
    handler = {
        "api": "API client — use api_spec (endpoint, auth, openapi_spec_url)",
        "file": "direct download — use file_spec (download_url, file_format)",
    }.get(stype, "manual review")
    d = inputs_dir / DEFERRED_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    record = {
        "site_id": site_id,
        "source_type": stype,
        "name": source.get("name"),
        "provider": source.get("provider"),
        "url": source.get("url"),
        "description": source.get("description"),
        "reason": "harness is a browser crawler; non-embedded sources are not auto-explored",
        "suggested_handler": handler,
        "api_spec": source.get("api_spec"),
        "file_spec": source.get("file_spec"),
        "source": source,
    }
    (d / f"{site_id}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return site_id


def _load_discovery(path: Path) -> tuple[list[dict], str | None]:
    """Load ``(sources, query)`` from a discovery output file.

    Recognizes the pipeline's ``FinalReport`` shape (``src.models.report``):
    ``all_sources_ranked`` (cross-type unified ranking) is preferred, else the
    ``embedded_sources`` + ``api_sources`` + ``file_sources`` lists are combined.
    The report's ``query`` is returned so the CLI query arg can be optional.
    Also accepts a plain JSON list, a ``{"sources": [...]}`` object, or JSONL
    (``query`` is ``None`` in those cases).
    """
    text = path.read_text(encoding="utf-8")
    head = text.lstrip()[:1]
    if head == "[":
        return json.loads(text), None
    if head == "{":
        obj = json.loads(text)
        if isinstance(obj, dict):
            query = obj.get("query")
            grouped = ("embedded_sources", "api_sources", "file_sources")
            if "all_sources_ranked" in obj or any(k in obj for k in grouped):
                ranked = obj.get("all_sources_ranked")
                if isinstance(ranked, list) and ranked:
                    return ranked, query
                combined: list[dict] = []
                for k in grouped:
                    if isinstance(obj.get(k), list):
                        combined += obj[k]
                return combined, query
            for key in ("sources", "data_sources", "results"):
                if isinstance(obj.get(key), list):
                    return obj[key], query
            return [obj], query
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()], None


# ── launch (bounded-parallel harness processes) ──────────────────────


# Flags defined on runtime.cli's TOP parser must precede the subcommand;
# everything else in passthrough is an explore-loop subcommand arg (comes after).
_GLOBAL_CLI_FLAGS = {"--model"}


def _build_explore_cmd(site_id: str, passthrough: list[str], inputs_dir: Path = HARNESS_INPUTS) -> list[str]:
    """Argv for one harness explore-loop run. `--quiet` / `--model` are GLOBAL
    runtime.cli args (BEFORE the subcommand); `--max-iters` / `--max-cost-usd`
    are explore-loop subcommand args (AFTER `explore-loop <site>`). api sites
    (input marker: api_spec.json) default to the lower API_DEFAULT_MAX_ITERS
    cap unless an explicit --max-iters came through."""
    glob: list[str] = ["--quiet"]
    loop: list[str] = []
    i = 0
    while i < len(passthrough):
        tok = passthrough[i]
        val = passthrough[i + 1] if i + 1 < len(passthrough) else None
        target = glob if tok in _GLOBAL_CLI_FLAGS else loop
        target += [tok] + ([val] if val is not None else [])
        i += 2
    if "--max-iters" not in loop and (inputs_dir / site_id / "api_spec.json").exists():
        loop += ["--max-iters", str(API_DEFAULT_MAX_ITERS)]
    return [str(HARNESS_PY), "-m", "runtime.cli", *glob, "explore-loop", site_id, *loop]


def _parse_last_json(text: str) -> dict | None:
    """Extract the last top-level ``{...}`` JSON object from text.

    ``explore-loop --quiet`` prints its final summary dict as pretty JSON at the
    end of stdout; this pulls it back out for the manifest.
    """
    depth = 0
    start: int | None = None
    last: str | None = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                last = text[start : i + 1]
    if last:
        try:
            return json.loads(last)
        except json.JSONDecodeError:
            return None
    return None


async def _run_one(
    site_id: str, sem: asyncio.Semaphore, passthrough: list[str], inputs_dir: Path = HARNESS_INPUTS
) -> dict:
    async with sem:
        proc = await asyncio.create_subprocess_exec(
            *_build_explore_cmd(site_id, passthrough, inputs_dir),
            cwd=str(HARNESS_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        text = out.decode("utf-8", "replace")
        summary = _parse_last_json(text)
        verdict = (summary or {}).get("final_verdict")
        print(f"  [{site_id}] exit={proc.returncode} verdict={verdict}", flush=True)
        return {
            "site_id": site_id,
            "exit_code": proc.returncode,
            "verdict": verdict,
            "summary": summary,
            "stdout_tail": text[-1500:],
        }


async def launch_all(
    site_ids: list[str], concurrency: int, passthrough: list[str], inputs_dir: Path = HARNESS_INPUTS
) -> list[dict]:
    sem = asyncio.Semaphore(max(1, concurrency))
    return list(await asyncio.gather(*(_run_one(s, sem, passthrough, inputs_dir) for s in site_ids)))


def _harvest_discovered(
    site_ids: list[str], query: str, inputs_dir: Path, seen_products: set[tuple[str, str]]
) -> list[dict]:
    """Read each just-run site's discovered_sources.yaml and stage a NEW site per
    genuinely-new (url, type) product. Dedup via ``seen_products`` (mutated in
    place). Unknown types are skipped. Mid-run-discovered api sources stage like
    the rest but almost always land ``awaiting_credentials`` (their synthetic
    source has no auth info → conservative gate) — staged, surfaced, NOT queued
    for the next wave. Returns the newly-staged site dicts (with ``status``)."""
    try:
        import yaml
    except ImportError:
        print("[warn] pyyaml not installed — skipping discovered-source harvest", file=sys.stderr)
        return []
    newly: list[dict] = []
    for sid in site_ids:
        path = HARNESS_DIR / "workspaces" / sid / "discovered_sources.yaml"
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] unreadable {path}: {exc}", file=sys.stderr)
            continue
        for ds in (data.get("sources") or []):
            url = (ds.get("url") or "").strip()
            types = [t for t in (ds.get("types") or []) if t in ("embedded", "file", "api")]
            if not url or not types:
                continue
            for t in types:
                if (url, t) in seen_products:
                    continue
                seen_products.add((url, t))
                wtype = _WORKFLOW_TYPE.get(t)
                if not wtype:  # unknown type → recorded as seen, no site
                    continue
                syn = {
                    "url": url,
                    "source_type": t,
                    "name": (ds.get("note") or url)[:80],
                    "provider": re.sub(r"^https?://", "", url).split("/")[0],
                    "description": ds.get("note") or "discovered mid-run by a sibling run",
                    "metadata": {"discovered_mid_run": True, "found_on_url": ds.get("found_on_url", "")},
                }
                siblings = [x for x in types if x != t]
                new_sid = stage_source(syn, query, wtype, inputs_dir, sibling_categories=siblings)
                status = (
                    ("ready" if api_site_ready(new_sid, inputs_dir) else "awaiting_credentials")
                    if wtype == "api"
                    else "ready"
                )
                newly.append({
                    "site_id": new_sid, "workflow_type": wtype, "status": status,
                    "sibling_categories": siblings, "discovered_from": sid,
                })
    return newly


async def _run_waves(
    initial_site_ids: list[str], seen_products: set[tuple[str, str]], query: str, inputs_dir: Path,
    concurrency: int, passthrough: list[str], max_waves: int,
) -> tuple[list[dict], list[dict]]:
    """Launch sites in waves. After each wave, harvest discovered_sources.yaml from
    the just-run sites, stage genuinely-new products as new sites, and run THOSE
    next wave — until no new products surface or max_waves is hit. Dedup by
    (url, type) guarantees termination; max_waves caps recursion depth.
    Returns (all launch_results, newly_staged site dicts)."""
    all_results: list[dict] = []
    newly_staged: list[dict] = []
    queue = list(initial_site_ids)
    wave = 0
    while queue and wave < max_waves:
        wave += 1
        print(f"── wave {wave}: {len(queue)} site(s) ──", flush=True)
        all_results.extend(await launch_all(queue, concurrency, passthrough, inputs_dir))
        fresh = _harvest_discovered(queue, query, inputs_dir, seen_products)
        if fresh:
            print(f"  +{len(fresh)} new product(s) discovered mid-run → staged as new site(s)", flush=True)
        gated = [s for s in fresh if s.get("status") == "awaiting_credentials"]
        for s in gated:
            print(
                f"  [gated] {s['site_id']} (api) staged but awaiting credentials — "
                f"see inputs/{s['site_id']}/credentials_needed.json",
                flush=True,
            )
        newly_staged.extend(fresh)
        queue = [s["site_id"] for s in fresh if s.get("status") != "awaiting_credentials"]
    if queue:  # ran out of waves with pending sites — surface, don't silently drop
        print(
            f"[cap] max_waves={max_waves} reached; {len(queue)} discovered site(s) staged but NOT "
            f"launched (raise --max-waves to run them): {queue}",
            file=sys.stderr,
        )
    return all_results, newly_staged


# ── skill feedback (Phase 4): harness truth → backend skill_library ───
#
# After the harness runs, a source's REAL type is whichever artifact its
# workspace produced (selectors.yaml=embedded / download_manifest.yaml=file;
# download wins if both) — the same artifact-presence test the validator uses
# (runtime/validate.py:_detect_workflow_type). We write a DECOUPLED
# skill_feedback.json (URL + real_type [+ relevance report if explore wrote
# one]); the BACKEND consumer (backend/scripts/apply_skill_feedback.py)
# re-matches each URL against its skill library and corrects it. The bridge
# stays free of backend deps — it only writes the JSON.


def _detect_real_type(site_id: str) -> str | None:
    ws = HARNESS_DIR / "workspaces" / site_id
    if (ws / "api_manifest.yaml").exists():
        return "api"
    if (ws / "download_manifest.yaml").exists():
        return "file"
    if (ws / "selectors.yaml").exists():
        return "embedded"
    return None  # inconclusive / nothing built → no verified type


def _read_relevance_report(site_id: str) -> dict | None:
    """Read the explore agent's off-goal report if present (Piece C):
    ``workspaces/<id>/off_goal_report.yaml`` = {reason, fresh_description}."""
    path = HARNESS_DIR / "workspaces" / site_id / "off_goal_report.yaml"
    if not path.exists():
        return None
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or not data.get("reason"):
        return None
    return {
        "relevance": "irrelevant",
        "reason": data.get("reason"),
        "fresh_description": data.get("fresh_description") or {},
    }


def _emit_skill_feedback(site_ids: list[str], inputs_dir: Path) -> tuple[Path | None, int]:
    """Write ``inputs/skill_feedback.json`` from each run's artifacts + relevance
    report. URL is read from ``inputs/<site>/seed.json`` (seed_url)."""
    items: list[dict] = []
    for sid in site_ids:
        url = None
        seed = inputs_dir / sid / "seed.json"
        aspec = inputs_dir / sid / "api_spec.json"
        try:
            if seed.exists():
                url = (json.loads(seed.read_text(encoding="utf-8")) or {}).get("seed_url")
            elif aspec.exists():  # api sites have no seed.json
                url = ((json.loads(aspec.read_text(encoding="utf-8")) or {}).get("source") or {}).get("url")
        except Exception:  # noqa: BLE001
            continue
        if not url:
            continue
        real_type = _detect_real_type(sid)
        rel = _read_relevance_report(sid)
        if not real_type and not rel:
            continue
        item: dict = {"url": url, "real_type": real_type}
        if rel:
            item.update(rel)
        items.append(item)
    if not items:
        return None, 0
    path = inputs_dir / "skill_feedback.json"
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, len(items)


# ── manifest + CLI ───────────────────────────────────────────────────


def _write_manifest(
    inputs_dir: Path, query: str, staged: list[dict], deferred: list[str], launch_results: list[dict]
) -> Path:
    manifest = {
        "query": query,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "harness_sites": staged,  # [{site_id, workflow_type, status, ...}]
        "awaiting_credentials": [
            s["site_id"] for s in staged if s.get("status") == "awaiting_credentials"
        ],
        "deferred_sites": deferred,
        "launch_results": launch_results,
    }
    path = inputs_dir / "_handoff_manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Stage + launch harness runs from Zillusion discovery output."
    )
    p.add_argument("sources", help="Discovery output: FinalReport JSON / list / JSONL of DataSource.")
    p.add_argument(
        "query",
        nargs="?",
        default=None,
        help="Original user query. If omitted, taken from the discovery report's `query` field.",
    )
    p.add_argument("--inputs-dir", default=str(HARNESS_INPUTS), help="Harness inputs/ directory.")
    p.add_argument("--no-launch", action="store_true", help="Stage inputs only; do not launch harness runs.")
    p.add_argument("--concurrency", type=int, default=2, help="Max concurrent harness processes (default 2).")
    p.add_argument("--max-waves", type=int, default=3,
                   help="Max launch waves: initial sites + sites auto-spawned from mid-run discoveries (default 3).")
    p.add_argument("--max-iters", type=int, default=None, help="Pass-through cap to each explore-loop.")
    p.add_argument("--max-cost-usd", type=float, default=None, help="Pass-through cost cap to each explore-loop.")
    p.add_argument("--model", default=None, help="Pass-through model id for the harness explore-loop (e.g. deepseek-v4-pro).")
    args = p.parse_args(argv)

    sources, report_query = _load_discovery(Path(args.sources))
    query = args.query or report_query
    if not query:
        print(
            "[ERR] no query provided and none found in the discovery file "
            "(FinalReport.query). Pass the query as the 2nd argument.",
            file=sys.stderr,
        )
        return 1
    inputs_dir = Path(args.inputs_dir)
    # Group categories by url so each fanned-out site knows its SIBLINGS (other
    # products of the SAME source, owned by their own runs) — its goal.md then
    # tells the agent to stay in its lane instead of pivoting to / duplicating them.
    url_types: dict[str, list[str]] = {}
    for src in sources:
        st = (src.get("source_type") or "").lower()
        if st:
            lst = url_types.setdefault(src.get("url") or "", [])
            if st not in lst:
                lst.append(st)

    staged: list[dict] = []  # [{site_id, workflow_type, status, sibling_categories}]
    deferred: list[str] = []
    for src in sources:
        st = (src.get("source_type") or "").lower()
        wtype = _WORKFLOW_TYPE.get(st)
        siblings = [t for t in url_types.get(src.get("url") or "", []) if t != st]
        if wtype:  # embedded → extraction, file → download, api → api
            sid = stage_source(src, query, wtype, inputs_dir, sibling_categories=siblings)
            status = (
                ("ready" if api_site_ready(sid, inputs_dir) else "awaiting_credentials")
                if wtype == "api"
                else "ready"
            )
            staged.append({
                "site_id": sid, "workflow_type": wtype, "status": status,
                "sibling_categories": siblings,
            })
        else:  # unknown type → no harness workflow for it
            deferred.append(stage_deferred(src, inputs_dir))

    awaiting = [s for s in staged if s["status"] == "awaiting_credentials"]
    site_ids = [s["site_id"] for s in staged if s["status"] == "ready"]
    n_ext = sum(1 for s in staged if s["workflow_type"] == "extraction")
    n_dl = sum(1 for s in staged if s["workflow_type"] == "download")
    n_api = sum(1 for s in staged if s["workflow_type"] == "api")
    print(
        f"staged {len(staged)} → harness ({n_ext} extraction, {n_dl} download, {n_api} api; "
        f"{len(awaiting)} api site(s) awaiting credentials), "
        f"deferred {len(deferred)} unknown → {inputs_dir}"
    )
    for s in staged:
        bundle = "goal.md + api_spec.json" if s["workflow_type"] == "api" else "goal.md + seed.json"
        gate = "  [awaiting credentials]" if s["status"] == "awaiting_credentials" else ""
        print(f"  inputs/{s['site_id']}/  ({s['workflow_type']}: {bundle}){gate}")
    for sid in deferred:
        print(f"  inputs/{DEFERRED_DIRNAME}/{sid}.json  (deferred — unknown type)")
    if awaiting:
        print("\nawaiting credentials — to unlock each api site:")
        for s in awaiting:
            needed_path = inputs_dir / s["site_id"] / "credentials_needed.json"
            signup = None
            try:
                signup = (json.loads(needed_path.read_text(encoding="utf-8")) or {}).get("signup_url")
            except (OSError, json.JSONDecodeError):
                pass
            print(f"  {s['site_id']}:")
            if signup:
                print(f"    1. get a key: {signup}")
            else:
                print("    1. get a key from the provider (see inputs/<site>/goal.md Credentials)")
            print(f'    2. write inputs/{s["site_id"]}/credentials.json  {{"api_key": "<key>", "extra": {{}}}}')
            print(f"    3. run: .venv\\Scripts\\python.exe -m runtime.cli explore-loop {s['site_id']}")

    launch_results: list[dict] = []
    if site_ids and not args.no_launch:
        if not HARNESS_PY.exists():
            print(f"[ERR] harness venv python not found: {HARNESS_PY}", file=sys.stderr)
            print("      Create it / fix the path, or re-run with --no-launch to stage only.", file=sys.stderr)
            return 1
        passthrough: list[str] = []
        if args.max_iters is not None:
            passthrough += ["--max-iters", str(args.max_iters)]
        if args.max_cost_usd is not None:
            passthrough += ["--max-cost-usd", str(args.max_cost_usd)]
        if args.model:
            passthrough += ["--model", args.model]
        # Seed dedup with the initial products so the harvest never re-stages
        # a source already covered by this run or a sibling.
        seen_products = {(s.get("url") or "", (s.get("source_type") or "").lower()) for s in sources}
        print(
            f"launching {len(site_ids)} harness run(s), concurrency={args.concurrency}, "
            f"max_waves={args.max_waves}{' ' + ' '.join(passthrough) if passthrough else ''} ...",
            flush=True,
        )
        launch_results, discovered = asyncio.run(_run_waves(
            site_ids, seen_products, query, inputs_dir, args.concurrency, passthrough, args.max_waves
        ))
        staged.extend(discovered)
        passed = sum(1 for r in launch_results if r.get("verdict") == "PASS")
        print(
            f"done: {passed}/{len(launch_results)} PASS across {len(launch_results)} run(s); "
            f"{len(discovered)} site(s) auto-spawned from mid-run discoveries"
        )

        # Phase 4: emit skill feedback (harness truth → backend skill_library).
        ran_site_ids = site_ids + [d["site_id"] for d in discovered]
        fb_path, fb_n = _emit_skill_feedback(ran_site_ids, inputs_dir)
        if fb_path:
            print(
                f"skill_feedback: {fb_n} item(s) → {fb_path}\n"
                f"  apply with: python backend/scripts/apply_skill_feedback.py {fb_path} --apply"
            )
    elif site_ids:
        print("\n--no-launch: staged only. To explore a site (from the harness dir, harness venv):")
        for sid in site_ids[:3]:
            print(f"  .venv\\Scripts\\python.exe -m runtime.cli explore-loop {sid}")

    manifest = _write_manifest(inputs_dir, query, staged, deferred, launch_results)
    print(f"manifest → {manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
