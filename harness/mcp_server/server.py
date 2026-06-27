"""MCP server entry point (harness variant).

Differences from the Playwright variant:

  * ``browser_player`` executes async Python against the live page with
    ``page``, ``context``, ``cdp`` in scope. The agent uses this for
    one-shot probes that don't deserve a helper yet.
  * ``workspace_helper_append`` mirrors the append-only helpers.py
    discipline. ``workspace_write`` rejects helpers.py.
  * ``skill_*`` tools expose the cross-site domain_skills/ library.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from mcp_server import vision
from mcp_server.browser import Browser
from mcp_server.skill_library import SkillEntry, SkillLibrary
from mcp_server.tool_timeout import install_tool_timeout
from mcp_server.workspace import Memory, Workspace

PROJECT_ROOT = Path(os.environ.get("CRAWLER_EXPLORER_ROOT", Path(__file__).resolve().parent.parent))
WORKSPACES_ROOT = PROJECT_ROOT / "workspaces"
MEMORY_ROOT = PROJECT_ROOT / "memory"
SKILLS_ROOT = PROJECT_ROOT / "domain_skills"
HEADLESS = os.environ.get("CRAWLER_EXPLORER_HEADLESS", "1") != "0"
# Vision backstop. The orchestrator forwards the RESOLVED base-model mode (text
# vs multimodal) as CRAWLER_EXPLORER_VISION ("1"/"0") so this server's
# browser_screenshot refuses on a text-model run even if the tool somehow stays
# in context. Defense-in-depth behind the agent-side tool gate (disallowed_tools /
# allow-list); unset → enabled (the gate is the primary control, like the api
# CRAWLER_EXPLORER_WORKFLOW backstop). The disabled vocabulary MIRRORS
# runtime/modality._env_override so the two layers can't disagree on a raw value
# like "off"/"false" (the agent gate reads text, so must the backstop).
_VISION_OFF_VALUES = {"0", "false", "off", "no", "text"}
VISION_ENABLED = (
    os.environ.get("CRAWLER_EXPLORER_VISION", "").strip().lower() not in _VISION_OFF_VALUES
)
# Per-end-user auth store. Cookies for multiple sites accumulate in ONE file PER
# USER (the browser sends the right ones per domain), keyed by the owning user's
# id — forwarded as CRAWLER_EXPLORER_USER_ID by the orchestrator. It rides the
# bind-mounted workspaces/ tree (workspaces/_auth/<user_id>/auth_state.json) so it
# persists under the docker sandbox; PROJECT_ROOT/auth_state.json is NOT mounted
# there and would be ephemeral. GLOBAL_AUTH stays the shared fallback used when no
# user id is set (local/dev interactive) — and keeps the symbol other modules import.
GLOBAL_AUTH = PROJECT_ROOT / "auth_state.json"
_AUTH_BY_USER_ROOT = WORKSPACES_ROOT / "_auth"


def _user_auth_path() -> Path:
    """auth_state.json for the run's OWNING user. Per-user when
    CRAWLER_EXPLORER_USER_ID is set (the multi-tenant path); the shared GLOBAL_AUTH
    fallback otherwise. The id is reduced to ONE filesystem-safe segment (alnum / _
    / - only), so a malformed/hostile id can never escape the _auth root."""
    uid = os.environ.get("CRAWLER_EXPLORER_USER_ID", "").strip()
    if not uid:
        return GLOBAL_AUTH
    safe = "".join(c if (c.isalnum() or c in "_-") else "_" for c in uid)[:128] or "_"
    return _AUTH_BY_USER_ROOT / safe / "auth_state.json"

_browser = Browser(headless=HEADLESS)
_skills = SkillLibrary(SKILLS_ROOT)
_memory = Memory(MEMORY_ROOT)
_active_site: str | None = None


def _mcp_log(event: str, **data: Any) -> None:
    """Best-effort server-side JSONL log under workspaces/<active_site>/run-logs/.

    The agent-observed tool I/O (every tool_use + tool_result, with input and
    content) is already captured by runtime.run_logger on the SDK side, so this
    deliberately records only what the agent CANNOT see — e.g. lifespan teardown
    failures that would otherwise be swallowed. Never raises.
    """
    try:
        base = (
            (WORKSPACES_ROOT / _active_site / "run-logs")
            if _active_site
            else (PROJECT_ROOT / "run-logs")
        )
        base.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            **data,
        }
        fname = datetime.now(timezone.utc).strftime("%Y%m%d") + "-mcp.jsonl"
        with (base / fname).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


@asynccontextmanager
async def _lifespan(_server):
    """Tear down Chromium inside FastMCP's own event loop.

    See the playwright variant for why - the original cross-loop shutdown
    produced an ugly NoneType traceback on every stdio session.
    """
    try:
        yield
    finally:
        try:
            await _browser.shutdown()
        except Exception as exc:
            _mcp_log(
                "browser_shutdown_error",
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
            )


mcp = FastMCP("crawler-explorer-harness", lifespan=_lifespan)

# Wrap EVERY @mcp.tool() below with a wall-clock timeout so a hung handler
# (e.g. an un-timed page.evaluate on a wedged page) returns a tool error
# instead of freezing the whole run forever. MUST run before the tool defs.
# Intentionally-blocking tools opt out via @mcp.tool(timeout_s=...) — see
# browser_request_user_login (human takeover).
install_tool_timeout(mcp)


def _workspace() -> Workspace:
    if _active_site is None:
        raise RuntimeError(
            "no active workspace; call browser_attach(site_id=...) first "
            "(api workflows: workspace_attach — browser tools are disabled there)"
        )
    return Workspace(WORKSPACES_ROOT, _active_site)


# ---------- Browser ----------


@mcp.tool()
async def browser_attach(site_id: str) -> dict[str, Any]:
    """Bind a workspace and start Chromium. Call once at run start."""
    global _active_site
    _active_site = site_id
    ws = _workspace()
    ws.init()
    _browser.bind_samples_dir(ws.samples_dir)
    auth_path = _user_auth_path()
    _browser.bind_auth_state(auth_path)
    await _browser.ensure_started()
    _mcp_log("session_bind", site_id=site_id)
    return {
        "site_id": site_id,
        "workspace": str(ws.dir),
        "samples_dir": str(ws.samples_dir),
        "authenticated": auth_path.exists(),
    }


@mcp.tool()
async def workspace_attach(site_id: str) -> dict[str, Any]:
    """Bind a workspace WITHOUT starting a browser. Call once at run start
    of an API workflow (browser tools are disabled there); the browser-free
    sibling of browser_attach — every workspace_*/hypothesis_*/manifest tool
    needs one of the two to have bound the active site first."""
    global _active_site
    _active_site = site_id
    ws = _workspace()
    ws.init()
    _browser.bind_samples_dir(ws.samples_dir)
    _mcp_log("session_bind", site_id=site_id, mode="workspace_only")
    return {
        "site_id": site_id,
        "workspace": str(ws.dir),
        "samples_dir": str(ws.samples_dir),
        "browser": "not started (api workflow — probe over HTTP instead)",
    }


@mcp.tool()
async def browser_goto(url: str, wait_until: str = "domcontentloaded") -> dict[str, Any]:
    """Navigate the active page."""
    return await _browser.goto(url, wait_until=wait_until)


@mcp.tool()
async def browser_evaluate(expression: str) -> dict[str, Any]:
    """Run a JS expression in the page context."""
    return {"result": await _browser.evaluate(expression)}


@mcp.tool()
async def browser_content(selector: str | None = None) -> dict[str, Any]:
    """Return page HTML or a CSS-selected sub-tree.

    With ``selector=None`` returns the full document HTML. Pass a selector
    like ``'main'`` or ``'.search-result-link'`` to grab a sub-tree only —
    that's how you keep the result small. There is NO server-side char
    cap here; the SDK's own 25K-token tool-result cap is the backstop
    (it auto-saves the oversized result to a file and surfaces the path
    so you can Read with offset/limit or Grep). Hitting that cap costs
    an extra round-trip, so refining ``selector`` is the cheaper path.

    Returns ``{html, chars_total, selector}``. Empty ``html`` with
    ``chars_total=0`` and a non-None selector means the selector matched
    nothing — that itself is a useful probe signal (page not loaded yet,
    or you guessed the wrong class).
    """
    return await _browser.content_of(selector=selector)


@mcp.tool()
async def browser_cdp_send(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send a raw Chrome DevTools Protocol command. First-class, not an escape hatch."""
    return {"result": await _browser.cdp_send(method, params or {})}


@mcp.tool()
async def browser_player(script: str) -> dict[str, Any]:
    """Execute an async Python snippet against the live page.

    The snippet has ``page``, ``context``, ``cdp`` in scope. Assign your
    output to ``result``. Use this for one-shot probes - don't commit a
    helper until the technique has earned it.
    """
    return await _browser.run_player_script(script)


@mcp.tool()
async def browser_snapshot(name: str) -> dict[str, str]:
    """Save the current page as HTML + PNG into samples/."""
    return await _browser.snapshot(name)


@mcp.tool()
async def browser_screenshot(full_page: bool = False, selector: str | None = None) -> Any:
    """Capture the live page as an IMAGE you can SEE — the multimodal base
    model's extra information source, beside the DOM/HTML you read with
    browser_content / browser_evaluate / browser_player.

    Returns the current VIEWPORT as a PNG. ``full_page=True`` captures the whole
    scroll height (heavier — more image tokens); ``selector="..."`` returns just
    that element's box. Reach for it when the rendered PIXELS carry signal the
    HTML doesn't: canvas / SVG / chart content, text baked into images, a visual
    layout you must reason about, or confirming an overlay / captcha / anti-bot
    state vs what the DOM claims. For plain text, links, or structured data,
    browser_content is the cheaper read.

    Only present when this run's base model is MULTIMODAL — a text-model run
    doesn't get this tool (it's withheld from the surface), and as a backstop the
    server refuses the call when vision is disabled.
    """
    if not VISION_ENABLED:
        return {
            "ok": False,
            "vision_disabled": True,
            "reason": (
                "this run's base model is text-only — browser_screenshot is "
                "unavailable. Read the page with browser_content / "
                "browser_evaluate / browser_player instead."
            ),
        }
    data, fmt = await _browser.screenshot_image(full_page=full_page, selector=selector)
    return Image(data=data, format=fmt)


@mcp.tool()
async def browser_vision_probe(question: str, full_page: bool = False, selector: str | None = None) -> Any:
    """Ask a multimodal SUBMODEL about the current page's PIXELS and get a short
    TEXT answer back — your eyes when YOUR base model is text-only and can't see
    images itself.

    Pass a SPECIFIC question (this is one paid VLM round-trip per call, so ask for
    what you need, don't sweep every page): e.g. "what numbers are on the bars of
    this chart?", "is there a login/captcha overlay covering the content?", "what
    does the text baked into this banner image say?", "is the product list
    rendered as real DOM or as a single canvas?". ``full_page=True`` captures the
    whole scroll height (heavier); ``selector="..."`` scopes to one element's box.

    Returns ``{ok, answer, submodel}`` (or ``{ok: false, reason}`` if no
    multimodal backend is configured). The submodel is separate from your base
    model — set CRAWLER_EXPLORER_VISION_MODEL / ANTHROPIC_BASE_URL to choose it.

    EXPLORE-TIME AID, NOT a data channel: like a screenshot, what you learn here
    cannot ride a `deterministic` workflow.py (it reruns standalone with no LLM).
    Turn the finding into a deterministic mechanism (a selector / JS read / OCR)
    or carry the collection on the `agentic` route. Only present when your base
    model is text-only — a multimodal run uses browser_screenshot instead.
    """
    if VISION_ENABLED:
        return {
            "ok": False,
            "reason": (
                "your base model is multimodal — call browser_screenshot to see the "
                "page directly; browser_vision_probe is only for a text base model."
            ),
        }
    data, fmt = await _browser.screenshot_image(full_page=full_page, selector=selector)
    return await vision.describe(data, fmt, question)


# ---------- Auth / human login ----------


@mcp.tool()
async def browser_check_login_wall() -> dict[str, Any]:
    """Detect whether an ACCESS WALL gates the data: login, login modal,
    captcha, or a JS / Cloudflare challenge.

    Call this whenever you can't get the data you came for — a goto returns
    empty / blocked content, redirects to /login, OR the page looks normal but
    the records are missing / blurred behind a sign-in modal, OR a captcha /
    "verify you are human" / Cloudflare "just a moment" challenge is in the way.

    Returns {is_access_wall, wall_type, is_login_wall, current_url, signals}.
    wall_type is one of login / login_modal / captcha / challenge / none. A
    content-rich page that merely has a 'Sign in' link is NOT flagged, but a
    modal / captcha / challenge that actually gates the data IS. If
    is_access_wall, hand over via browser_request_user_login (it covers login
    AND verification).
    """
    return await _browser.check_login_wall()


# timeout_s=660: this tool BLOCKS on a human (request_user_takeover waits up to
# 600s for the user to finish logging in). The universal 300s timeout would cut
# the takeover short — exempt it with a backstop just above its own 600s bound.
@mcp.tool(timeout_s=660)
async def browser_request_user_login(
    login_url: str, reason: str = "", message: str = "", wall_type: str = ""
) -> dict[str, Any]:
    """HUMAN TAKEOVER: let the user log in OR solve a verification (captcha /
    Cloudflare / "are you human") challenge in a real browser.

    THIS IS THE ONLY SUPPORTED WAY TO PASS A LOGIN / AUTH WALL. When
    browser_check_login_wall reports is_access_wall (any wall_type), call THIS
    tool. Do NOT write your own login scripts, call the site's login / QR APIs
    (e.g. getQrCode), decode QR images, or launch your own headed browser
    (chromium headless=False) — those bypass the managed X display and the
    streamed canvas, trip anti-bot, and the user never sees a window. If a PRIOR
    iteration recorded the login as "blocked" / "no X server", do NOT treat it
    as permanent — the streamed takeover works; call this tool again instead of
    giving up and shipping public-only data.

    Pass `reason` (short), and optionally `message` (markdown shown to the user
    explaining why takeover is needed + how to do it) and `wall_type`.

    Behaviour depends on the run context:
    - Web UI (orchestrator + takeover channel): the headed login browser is
      STREAMED into the user's data-source sub-session; this call BLOCKS until
      the user finishes, then persists auth automatically (you need NOT call
      browser_save_auth). Covers login / captcha / challenge alike. On a launch
      failure it returns {ok: false, reason}: report that reason to the user and
      retry — do NOT fall back to a custom login script.
    - Local interactive: opens a SEPARATE headed Chromium window the operator
      sees; STOP your turn, tell them what to do, then call browser_save_auth
      next turn.
    - Headless autonomous (no takeover channel): returns {autonomous: true};
      record a `blocked` hypothesis (name the wall_type) and continue public data.
    """
    # Prefer the module-global _active_site (set by browser_attach / init_crawl)
    # and fall back to the env var — mirrors send_user_message at line ~331. The
    # env var alone is wrong for the agentic-crawl route: explore_loop pops
    # CRAWLER_EXPLORER_ACTIVE_SITE before dispatching the crawl agent, so an
    # env-only gate fell through to a LOCAL window (invisible to a remote user)
    # instead of the streamed takeover.
    active = (_active_site or os.environ.get("CRAWLER_EXPLORER_ACTIVE_SITE", "")).strip()
    auth_path = _user_auth_path()
    seed = str(auth_path) if auth_path.exists() else None
    # Web-UI / orchestrator WITH a takeover channel: stream the headed login
    # browser into the user's sub-session (this blocks until they finish).
    if active and os.environ.get("CRAWLER_EXPLORER_TAKEOVER", "").strip():
        return await _browser.request_user_takeover(
            login_url,
            request_path=WORKSPACES_ROOT / active / "_takeover_request.json",
            auth_path=auth_path,
            reason=reason,
            message=message,
            wall_type=wall_type,
            seed_auth=seed,
        )
    # Orchestrator-driven but NO takeover channel: no human present -> blocked.
    if active:
        return {
            "opened": False,
            "autonomous": True,
            "reason": (
                "orchestrator-driven run has no takeover channel. Record a "
                "`blocked` hypothesis (needs interactive login) and continue "
                "with public data; re-run interactive /explore to authenticate."
            ),
        }
    # Local interactive: open a headed window the operator sees directly.
    return await _browser.request_user_login(login_url, seed_auth=seed)


@mcp.tool()
async def browser_save_auth() -> dict[str, Any]:
    """Persist the logged-in session to THIS user's auth_state.json
    (one file per end-user; merged so multiple sites accumulate) and
    re-authenticate the main browser from it.

    Call AFTER the user finished logging in (in the window opened by
    browser_request_user_login). The saved storage_state is reused by: the main
    browser (keeps exploring authenticated), the generated workflow.py, and the
    validator's re-fetch. NOTE: this file holds live session cookies — it is
    gitignored; treat it as a secret.
    """
    return await _browser.save_auth(_user_auth_path())


# ---------- User communication ----------


@mcp.tool()
async def send_user_message(message: str) -> dict[str, Any]:
    """Send a short message directly to the USER watching this run.

    In a web-UI run the backend relays it and shows it as a chat bubble in the
    conversation (left side), beside the user's own messages. Use it to TALK to
    the person: explain what you're doing or why, flag something that needs
    their attention or a decision, report a notable finding, or acknowledge
    guidance they sent. Write in the user's language.

    One-way (the user replies by editing the Task plan / steering) and it does
    NOT pause your run. Keep it brief and human — this is not a log and not for
    dumping data. Outside a web-UI run it's a harmless no-op (recorded to the
    workspace but with no live reader).
    """
    msg = (message or "").strip()
    if not msg:
        return {"sent": False, "reason": "empty message"}
    site = _active_site or os.environ.get("CRAWLER_EXPLORER_ACTIVE_SITE", "").strip()
    if not site:
        return {"sent": False, "reason": "no active site; call browser_attach first"}
    # Append-only reverse channel the backend tails (mirrors _takeover_request.json):
    # workspaces/<site>/_agent_messages.jsonl, one {message, created_at} per line.
    path = WORKSPACES_ROOT / site / "_agent_messages.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "message": msg,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # (b) Write-time iter stamp: the orchestrator exports CRAWLER_EXPLORER_ITER_N
    # per iter; record it so the SessionStart conversation-history injection can
    # label this agent→user message's iteration exactly (the hook falls back to
    # ledger time-window derivation for entries that lack it).
    _iter_env = os.environ.get("CRAWLER_EXPLORER_ITER_N", "").strip()
    if _iter_env.isdigit():
        rec["iter"] = int(_iter_env)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _mcp_log("send_user_message", site_id=site, chars=len(msg))
    return {"sent": True, "chars": len(msg)}


# ---------- Workspace ----------


@mcp.tool()
async def workspace_init(site_id: str) -> dict[str, str]:
    """Create the workspace directory for a site (idempotent)."""
    ws = Workspace(WORKSPACES_ROOT, site_id)
    ws.init()
    return {"workspace": str(ws.dir)}


@mcp.tool()
async def workspace_read(path: str) -> dict[str, str]:
    """Read a file inside the active workspace."""
    return {"content": _workspace().read_file(path)}


@mcp.tool()
async def workspace_write(path: str, content: str) -> dict[str, str]:
    """Write a workspace file.

    Blocked paths (use the dedicated tool instead):
      - exploration_log.md  -> use workspace_append_log
      - helpers.py          -> use workspace_helper_append
      - hypotheses.yaml     -> use hypothesis_append / hypothesis_set_status
      - selectors.yaml      -> use selectors_write

    Append-only and schema-managed files are protected at the server boundary;
    the protection cannot be bypassed by trying the Claude Code built-in Write
    tool either (PreToolUse hook intercepts that path)."""
    _workspace().write_file(path, content)
    return {"status": "ok", "path": path}


@mcp.tool()
async def workspace_append_log(section: str, body: str) -> dict[str, str]:
    """Append a timestamped section to exploration_log.md (append-only)."""
    _workspace().append_log(section, body)
    return {"status": "ok"}


@mcp.tool()
async def workspace_append_facts(body: str) -> dict[str, str]:
    """Append a confirmed-fact block to verified_facts.md."""
    _workspace().append_facts(body)
    return {"status": "ok"}


@mcp.tool()
async def workspace_helper_append(name: str, code: str) -> dict[str, str]:
    """Append a function/constant to helpers.py (append-only).

    To evolve a helper, add a new version with a different name. The old
    version stays in the file as audit trail.
    """
    _workspace().helper_append(name, code)
    return {"status": "ok"}


@mcp.tool()
async def workspace_list_samples() -> dict[str, list[str]]:
    """List filenames captured under samples/."""
    return {"samples": _workspace().list_samples()}


# ---------- Schema-managed workspace files (hypotheses.yaml, selectors.yaml) ----------


@mcp.tool()
async def hypothesis_append(
    claim: str,
    source: str,
    priority: str = "high",
    notes: str | None = None,
    status: str = "unverified",
    result: str | None = None,
) -> dict[str, Any]:
    """Append a new hypothesis to workspaces/<site>/hypotheses.yaml.

    Schema-validated via mcp_server.schemas.HypothesesFile. The id is
    auto-assigned (next H### in sequence). The file is rewritten as a
    valid YAML list — no string-append, no risk of corrupting structure.

    If hypotheses.yaml exists but has the wrong shape (mapping instead
    of list, or unparseable YAML), this tool RAISES with a clear message
    rather than silently corrupting further. That's the whole point of
    the schema layer — fail loudly, not silently.

    Args:
        claim: One-sentence claim being tested.
        source: Where this came from, e.g. '/explore 2026-05-22'
            or 'validation-agent run val-XXXX on <ISO>'.
        priority: 'high' | 'medium' | 'low'. Defaults to 'high'.
        notes: Free-form context (validation report refs, evidence pointers).
        status: 'unverified' | 'confirmed' | 'refuted' | 'partial' | 'blocked'.
            Defaults to 'unverified' — set to confirmed only after evidence.
        result: Brief outcome description; null until evidence is in.

    Returns: {id, status, claim} on success.
    """
    new_h = _workspace().append_hypothesis(
        claim=claim,
        source=source,
        status=status,  # type: ignore[arg-type]
        priority=priority,  # type: ignore[arg-type]
        notes=notes,
        result=result,
    )
    return {
        "status": "ok",
        "id": new_h.id,
        "hypothesis_status": new_h.status,
        "claim_preview": new_h.claim[:80],
    }


@mcp.tool()
async def hypothesis_set_status(
    hypothesis_id: str,
    status: str,
    result: str | None = None,
    notes: str | None = None,
    wall_type: str | None = None,
) -> dict[str, Any]:
    """Update an existing hypothesis's status (and optionally result/notes/wall_type).

    Use this when you've gathered evidence that confirms/refutes/blocks a
    previously-unverified hypothesis. Schema-validated.

    Args:
        hypothesis_id: e.g. 'H006'.
        status: 'unverified' | 'confirmed' | 'refuted' | 'partial' | 'blocked'.
        result: Brief outcome description. Optional.
        notes: Free-form context. Optional; if set, replaces existing notes.
        wall_type: 'login' | 'login_modal' | 'captcha' | 'challenge' | 'none'.
            Optional; valid only with status='blocked'. Mirrors
            browser_check_login_wall's wall_type.

    Returns: {id, status, result_preview} or {error} if id not found / invalid.
    """
    try:
        updated = _workspace().set_hypothesis_status(
            hypothesis_id,
            status,  # type: ignore[arg-type]
            result=result,
            notes=notes,
            wall_type=wall_type,  # type: ignore[arg-type]
        )
    except (KeyError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}
    return {
        "status": "ok",
        "id": updated.id,
        "hypothesis_status": updated.status,
        "result_preview": (updated.result or "")[:80],
    }


@mcp.tool()
async def hypothesis_list() -> dict[str, Any]:
    """List all hypotheses in workspaces/<site>/hypotheses.yaml.

    Returns: {hypotheses: [{id, status, priority, claim_preview}, ...],
              total: int, by_status: {unverified, confirmed, ...}}.

    Use this for end-of-run review or to check what's outstanding before
    declaring DONE. Schema-validated read — fails loudly if file shape
    is wrong.
    """
    file = _workspace().read_hypotheses()
    by_status: dict[str, int] = {}
    for h in file.root:
        by_status[h.status] = by_status.get(h.status, 0) + 1
    return {
        "status": "ok",
        "total": len(file.root),
        "by_status": by_status,
        "next_id": file.next_id(),
        "hypotheses": [
            {
                "id": h.id,
                "status": h.status,
                "priority": h.priority,
                "claim_preview": h.claim[:100],
                "source": h.source,
            }
            for h in file.root
        ],
    }


@mcp.tool()
async def iter_summary_append(
    tried: list[str] | None = None,
    worked: list[str] | None = None,
    do_not_retry: list[str] | None = None,
    open_hypotheses: list[str] | None = None,
    next_strategy: str = "",
    cost_usd: float | None = None,
    record_count: int | None = None,
    free_text: str = "",
) -> dict[str, Any]:
    """Append a new `## Iter N` section to workspaces/<site>/iter_summary.md.

    Call this ONCE at the end of your /explore run (right before
    declare_crawl_route — the run's closing act). The SessionStart hook
    will inject this section's content into the next /explore run, so
    your job here is: write down what you tried this iter, what worked,
    what specifically NOT to retry, and what strategy to pivot to.

    The `do_not_retry` list is the most important — it's the explicit
    ban list the next iter agent must consult before picking probes.
    Be SPECIFIC ("MAX_DETAILS=8 expansion didn't yield self-posts"),
    not vague ("sampling didn't work").

    Args:
        tried: bullet list of "what I tried this iter" (one line each).
        worked: bullet list of breakthroughs / successes.
        do_not_retry: bullet list — specific patterns next iter MUST avoid.
        open_hypotheses: bullet list of H### still outstanding.
        next_strategy: 2-3 sentence direction for next iter; can include
            DO NOT retry block expansion + structurally different ideas.
        cost_usd / record_count: optional metadata for the section header.
        free_text: catch-all for any non-templated reflection.

    Returns: {iter_n, status} on success.

    Iter number is auto-assigned — current scheme: max existing iter_n + 1,
    or 1 if file doesn't exist.
    """
    iter_n = _workspace().append_iter_summary(
        cost_usd=cost_usd,
        record_count=record_count,
        tried=tried,
        worked=worked,
        do_not_retry=do_not_retry,
        open_hypotheses=open_hypotheses,
        next_strategy=next_strategy,
        free_text=free_text,
    )
    return {
        "status": "ok",
        "iter_n": iter_n,
        "appended_to": "iter_summary.md",
    }


@mcp.tool()
async def selectors_write(selectors_yaml: str) -> dict[str, Any]:
    """Write workspaces/<site>/selectors.yaml from a YAML text payload.

    The payload is parsed + validated against SelectorsFile schema before
    write. If validation fails, the existing file is left untouched and
    the error is returned with the specific field path that failed.

    Args:
        selectors_yaml: Full YAML text of the file. Must contain top-level
            `selectors:` and `field_stability:` keys. See
            mcp_server/schemas/selectors.py for the canonical shape.

    Returns: {status: ok, field_count, extract_js_chars} on success;
             {status: error, error} on schema mismatch.
    """
    import yaml as _yaml

    try:
        data = _yaml.safe_load(selectors_yaml)
    except _yaml.YAMLError as exc:
        return {"status": "error", "error": f"unparseable YAML: {exc}"}
    try:
        from mcp_server.schemas import SelectorsFile as _SelectorsFile

        file = _SelectorsFile.model_validate(data)
    except Exception as exc:
        return {"status": "error", "error": f"schema validation failed: {exc}"}
    _workspace().write_selectors(file)
    return {
        "status": "ok",
        "field_count": len(file.selectors.fields),
        "extract_js_chars": len(file.selectors.extract_js),
        "field_stability_count": len(file.field_stability.fields),
    }


@mcp.tool()
async def download_manifest_write(manifest_yaml: str) -> dict[str, Any]:
    """Write workspaces/<site>/download_manifest.yaml from a YAML text payload.

    The DOWNLOAD analog of selectors_write: a download workflow (file data
    source) declares the file(s) it fetches here so the validator can
    re-download + verify them. The payload is parsed + validated against the
    DownloadManifestFile schema before write; on failure the existing file is
    left untouched and the error is returned.

    Args:
        manifest_yaml: Full YAML text. Top-level `source_url:` + `files:` (a
            list of {url, filename, file_format, ...}). See
            mcp_server/schemas/download_manifest.py for the canonical shape.

    Returns: {status: ok, file_count, formats} on success;
             {status: error, error} on schema mismatch.
    """
    import yaml as _yaml

    try:
        data = _yaml.safe_load(manifest_yaml)
    except _yaml.YAMLError as exc:
        return {"status": "error", "error": f"unparseable YAML: {exc}"}
    try:
        from mcp_server.schemas import DownloadManifestFile as _DownloadManifestFile

        file = _DownloadManifestFile.model_validate(data)
    except Exception as exc:
        return {"status": "error", "error": f"schema validation failed: {exc}"}
    _workspace().write_download_manifest(file)
    return {
        "status": "ok",
        "file_count": len(file.files),
        "formats": sorted({f.file_format for f in file.files}),
    }


@mcp.tool()
async def api_manifest_write(manifest_yaml: str) -> dict[str, Any]:
    """Write workspaces/<site>/api_manifest.yaml from a YAML text payload.

    The API analog of selectors_write / download_manifest_write: an API
    workflow (HTTP data source) declares the endpoint(s) it calls + the
    output fields here so the validator can re-call the live API and verify
    the output. The payload is parsed + validated against the
    APIManifestFile schema before write; on failure the existing file is
    left untouched and the error is returned.

    Args:
        manifest_yaml: Full YAML text. Top-level `source_url:` + `endpoints:`
            (a list of {url_template, probe_url, method, auth, ...}) +
            `fields:` (declared output field names). `credentials_source`
            describes WHERE the secret is read from — NEVER a secret value.
            See mcp_server/schemas/api_manifest.py for the canonical shape.

    Returns: {status: ok, endpoint_count, field_count, identifier_field} on
             success; {status: error, error} on schema mismatch.
    """
    import yaml as _yaml

    try:
        data = _yaml.safe_load(manifest_yaml)
    except _yaml.YAMLError as exc:
        return {"status": "error", "error": f"unparseable YAML: {exc}"}
    try:
        from mcp_server.schemas import APIManifestFile as _APIManifestFile

        file = _APIManifestFile.model_validate(data)
    except Exception as exc:
        return {"status": "error", "error": f"schema validation failed: {exc}"}
    _workspace().write_api_manifest(file)
    return {
        "status": "ok",
        "endpoint_count": len(file.endpoints),
        "field_count": len(file.fields),
        "identifier_field": file.identifier_field,
    }


@mcp.tool()
async def report_discovered_source(
    url: str, types: list[str], note: str = "", found_on_url: str = ""
) -> dict[str, Any]:
    """Report a GENUINELY NEW data product you found MID-RUN that needs its OWN run.

    Use this ONLY for a product NOT already covered by your run or a sibling run —
    e.g. while extracting a listing you stumble on a separate "Download full dataset
    (CSV)" that the discovery step never saw. Do NOT report your own assigned data,
    and do NOT handle the new product inline (it's a different workflow type). The
    orchestrator harvests these after your run and stages a NEW site per product.

    Args:
        url: URL of the newly-found product.
        types: its category/categories — any of "embedded" / "file" / "api".
        note: what it is / why it deserves its own run.
        found_on_url: the page you were on when you found it.

    Returns: {status: ok, url, types, total_reported} or {status: error, error}.
    """
    try:
        from mcp_server.schemas import DiscoveredSource

        item = DiscoveredSource(url=url, types=types, note=note, found_on_url=found_on_url)
    except Exception as exc:
        return {"status": "error", "error": f"validation failed: {exc}"}
    total = _workspace().append_discovered_source(item)
    return {"status": "ok", "url": url, "types": item.types, "total_reported": total}


# ---------- Memory (cross-site, prose) ----------


@mcp.tool()
async def memory_index() -> dict[str, list[str]]:
    """List markdown files in memory/."""
    return {"entries": _memory.index()}


@mcp.tool()
async def memory_read(name: str) -> dict[str, str]:
    """Read memory/<name>."""
    return {"content": _memory.read(name)}


@mcp.tool()
async def memory_append(name: str, body: str) -> dict[str, str]:
    """Append a timestamped section to memory/<name>."""
    _memory.append(name, body)
    return {"status": "ok"}


# ---------- Skill library (cross-site, structured) ----------


@mcp.tool()
async def skill_list() -> dict[str, list[dict[str, str]]]:
    """List domain skills (id, title, when_to_use, success_count)."""
    return {"skills": _skills.index()}


@mcp.tool()
async def skill_read(skill_id: str) -> dict[str, Any]:
    """Read one skill's full record (SKILL.md content + recipe code)."""
    entry = _skills.load(skill_id)
    if entry is None:
        return {"error": f"skill not found: {skill_id}"}
    return entry.model_dump(mode="json")


@mcp.tool()
async def skill_propose(
    skill_id: str,
    title: str,
    when_to_use: str,
    description: str,
    evidence: str | None = None,
    recipe: str | None = None,
) -> dict[str, Any]:
    """Create or update a cross-site skill.

    Use this only after the technique has demonstrably worked on the
    current site. Pick a descriptive, stable skill_id like
    'dismiss-cookie-banner-eu' or 'paginate-infinite-scroll-twitter-style'.
    success_count starts at 1; subsequent ``skill_record_use`` calls
    increment it.
    """
    site_for_log = _active_site or "unknown"
    existing = _skills.load(skill_id)
    if existing is None:
        entry = SkillEntry(
            skill_id=skill_id,
            title=title,
            when_to_use=when_to_use,
            description=description,
            evidence=evidence,
            recipe=recipe,
            sites_seen=[site_for_log],
            success_count=1,
        )
    else:
        entry = existing.model_copy(
            update={
                "title": title,
                "when_to_use": when_to_use,
                "description": description,
                "evidence": evidence or existing.evidence,
                "recipe": recipe or existing.recipe,
                "sites_seen": sorted(set(existing.sites_seen + [site_for_log])),
                "success_count": existing.success_count + 1,
            }
        )
    _skills.upsert(entry)
    return {"status": "ok", "skill_id": skill_id, "success_count": entry.success_count}


@mcp.tool()
async def skill_record_use(skill_id: str, success: bool) -> dict[str, str]:
    """Record that a skill was applied on the active site.

    success=True only when the technique actually produced the expected
    outcome here. Used skill effectiveness over many sites is the signal
    for pruning the library.
    """
    if _active_site is None:
        return {"status": "no-active-site"}
    _skills.record_use(skill_id, _active_site, success)
    return {"status": "ok"}


@mcp.tool()
async def report_off_goal(
    reason: str,
    types: list[str],
    page_type: str = "",
    data_type: str = "",
    site_type: str = "",
    fields: list[str] | None = None,
    caveats: str = "",
    notes: str = "",
) -> dict[str, str]:
    """Report that the source you explored does NOT serve inputs/<site>/goal.md's
    User need, and describe what it ACTUALLY is. Call this ONLY when you judge the
    source off-goal — never when it is on-goal. Best done EARLY, before building a
    full workflow for an off-topic source. Writes off_goal_report.yaml; an
    upstream reviewer compares your first-hand description against a prior
    classification, so accuracy and the field semantics below matter.

    Fill each field from what you OBSERVED (not from expectation); '' / [] when
    unknown:

    reason: why this source does not serve goal.md's User need. Be concrete —
      what the page is actually about vs what the goal asked for. e.g. "Goal
      wants hotel nightly prices; this page is an airport shuttle-booking form —
      no hotel/price data present."
    types: data-source type(s) it actually is — any of "embedded" (the page IS
      the data: tables/cards/inline JSON), "file" (is/hosts a downloadable
      csv/json/xlsx/…), "api" (JSON/REST endpoint or API ref). [] = not a usable
      data source (login wall / empty / boilerplate).
    page_type: structural role — one of list / detail / search / download / doc /
      other.
    data_type: what KIND of data, in DOMAIN terms (concrete subject matter), e.g.
      "hotel-listing", "nlp-text-dataset", "weather-station-readings". Not the
      generic word "data".
    site_type: the publisher's nature, e.g. "travel-OTA", "open-data-portal",
      "code-repository", "gov-statistics-bureau".
    fields: the concrete fields you ACTUALLY saw extractable on the page (not what
      you'd expect), e.g. ["hotel_name", "star_rating", "price"]. [] if none.
    caveats: extraction gotchas you observed first-hand, e.g. "SPA — cards are
      JS-rendered", "price requires login", "captcha on first load". '' if none.
    """
    if _active_site is None:
        return {"status": "no-active-site"}
    import yaml

    report = {
        "reason": reason,
        "fresh_description": {
            "types": types,
            "page_type": page_type,
            "data_type": data_type,
            "site_type": site_type,
            "fields": fields or [],
            "caveats": caveats,
            "notes": notes,
        },
    }
    ws_dir = WORKSPACES_ROOT / _active_site
    ws_dir.mkdir(parents=True, exist_ok=True)
    path = ws_dir / "off_goal_report.yaml"
    path.write_text(yaml.safe_dump(report, allow_unicode=True, sort_keys=False), encoding="utf-8")
    _mcp_log("report_off_goal", reason=reason[:200], types=types)
    return {"status": "ok", "path": str(path)}


# ---------- Crawl-route selection (explore → terminal route) ----------

_ROUTE_NEXT = {
    "deterministic": "Finalize workflow.py (selectors/api/download manifest); validate→run follows.",
    "agentic": "Now call write_crawl_brief with the completeness anchor — the agentic crawl agent reads it.",
    "inline": "Now call commit_inline_dataset with the records you scraped — it produces the dataset directly.",
    "infeasible": "Call report_off_goal describing what the source actually is.",
}


@mcp.tool()
async def declare_crawl_route(route: str, rationale: str) -> dict[str, Any]:
    """Declare which TERMINAL route this source takes — **this is how an /explore
    run ENDS**. There is no separate DONE marker: a session that ends with NO
    declared route is treated as unfinished by the orchestrator and bounced back
    for another iteration. Writes crawl_route.json; the orchestrator routes on it
    (deterministic → validate→run; agentic → the crawl agent; inline/infeasible →
    terminal).

    Pick based on what you LEARNED exploring (the four are peers — the order
    below is not a ranking; `rationale` is required whichever you pick):
    - **deterministic**: control flow is static enough for code (stable pagination,
      stable selectors, homogeneous pages). Declare AFTER workflow.py + the
      manifest are written — your last act.
    - **agentic**: control flow is too DYNAMIC for static code (heterogeneous page
      templates, state-dependent navigation, a session wall needing periodic human
      takeover, anti-bot needing human-like variation). Declare AFTER
      write_crawl_brief.
    - **inline**: the FULL SET is small enough to harvest in-session right now —
      typically a few dozen records, a single page type, no need for a persistent
      run. commit_inline_dataset DECLARES this route for you (calling this tool
      again afterwards is harmless — same-route re-declares keep its run_id).
    - **infeasible**: NEITHER route can crawl it (e.g. per-request hard captcha that
      defeats both code and an agent). Declare AFTER report_off_goal.

    rationale: one or two sentences on WHY this route, grounded in what you saw.
    """
    if _active_site is None:
        return {"status": "no-active-site"}
    r = (route or "").strip().lower()
    if r not in _ROUTE_NEXT:
        return {"status": "error", "error": f"route must be one of {sorted(_ROUTE_NEXT)}"}
    import json

    path = WORKSPACES_ROOT / _active_site / "crawl_route.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same-route re-declare MERGES over the existing file (preserving extra keys
    # like inline's run_id, written by commit_inline_dataset); a route CHANGE
    # replaces it (stale extras would describe the old route).
    payload: dict[str, Any] = {}
    if path.exists():
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(prev, dict) and prev.get("route") == r:
                payload = prev
        except (OSError, json.JSONDecodeError):
            pass
    payload.update({"route": r, "rationale": rationale})
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _mcp_log("declare_crawl_route", route=r, rationale=(rationale or "")[:200])
    return {"status": "ok", "route": r, "path": str(path), "next": _ROUTE_NEXT[r]}


@mcp.tool()
async def write_crawl_brief(
    goal_summary: str,
    why_agentic: str,
    anchor_hardness: str,
    anchor_full_set: str,
    anchor_enumeration: str,
    anchor_termination: str,
    field_schema: dict[str, str] | None = None,
    identifier_field: str = "",
    anchor_estimated_total: int = 0,
    anchor_progress_metric: str = "",
    proven_knowledge: str = "",
    hazards: str = "",
    recommended_strategy: str = "",
) -> dict[str, Any]:
    """Write the AGENTIC-route handoff (workspaces/<site>/crawl_brief.md). The
    crawl agent reads this to harvest the data. Validated on write.

    The COMPLETENESS ANCHOR is mandatory — it's how the agent judges "done" (and,
    since completion is the agent's own call with no token cap, it's the only lever
    governing an open-ended loop). Label its hardness HONESTLY:
    - anchor_hardness="hard": an enumerable index (a total count, a category tree,
      a date window) → set anchor_estimated_total so progress is a precise ratio.
    - anchor_hardness="soft": only a stop heuristic (infinite scroll, no total) →
      completeness is subjective; the agent must justify the stop.

    goal_summary: what data, one record per what. field_schema: {field: semantic}.
    why_agentic: the dynamics that defeat a static workflow.py. anchor_full_set /
    _enumeration / _termination: what the full set is, how to walk it, the signal
    you've covered it. proven_knowledge / hazards / recommended_strategy: pointers
    to helpers/selectors/endpoints/auth, walls + safe cadence, how to batch.
    """
    if _active_site is None:
        return {"status": "no-active-site"}
    from mcp_server.schemas import CrawlBriefFile, CompletenessAnchor

    try:
        brief = CrawlBriefFile(
            site_id=_active_site,
            goal_summary=goal_summary,
            field_schema=field_schema or {},
            identifier_field=identifier_field or None,
            completeness_anchor=CompletenessAnchor(
                hardness=(anchor_hardness or "").strip().lower(),  # type: ignore[arg-type]
                full_set=anchor_full_set,
                enumeration=anchor_enumeration,
                termination=anchor_termination,
                estimated_total=anchor_estimated_total or None,
                progress_metric=anchor_progress_metric or None,
            ),
            why_agentic=why_agentic,
            proven_knowledge=proven_knowledge,
            hazards=hazards,
            recommended_strategy=recommended_strategy,
        )
    except Exception as exc:  # noqa: BLE001 — surface validation error to the agent
        return {"status": "error", "error": f"validation failed: {exc}"}

    path = WORKSPACES_ROOT / _active_site / "crawl_brief.md"
    brief.save_markdown(path)
    brief.save_yaml(path.with_suffix(".yaml"))
    warning = None
    if (
        brief.completeness_anchor.hardness == "hard"
        and brief.completeness_anchor.estimated_total is None
    ):
        warning = "hard anchor without estimated_total — progress can't be a precise ratio; set it if you can."
    elif brief.completeness_anchor.hardness == "soft":
        warning = (
            "soft anchor — completeness will be the agent's subjective call; higher spend risk."
        )
    _mcp_log("write_crawl_brief", hardness=brief.completeness_anchor.hardness)
    return {
        "status": "ok",
        "path": str(path),
        "hardness": brief.completeness_anchor.hardness,
        "warning": warning,
    }


@mcp.tool()
async def commit_inline_dataset(
    records: list,
    identifier_field: str = "",
    note: str = "",
    data_definition: dict[str, str] | None = None,
) -> dict[str, Any]:
    """INLINE route: hand off a SMALL full set you scraped in-session as the final
    dataset — no downstream run. Use when the full set was small enough to harvest
    during exploration (the 'inline' route). Produces the SAME artifact the agentic
    crawl agent would (runs/<run_id>/output.json + manifest), so the Data Agent
    consumes it identically. The anchor is inherently hard (full set = these records).

    records: the list of record objects (each a flat dict of fields). identifier_field:
    the dedup key, if any. note: one line on what this is. data_definition: field name
    -> meaning — REQUIRED when records are weak-schema data units (documents / media /
    heterogeneous pages) rather than table rows; lands in the run manifest so downstream
    consumers know what each field IS. Returns the run_id + record_count + outcome
    (COMPLETE on success).
    """
    if _active_site is None:
        return {"status": "no-active-site"}
    if not isinstance(records, list) or not records:
        return {"status": "error", "error": "records must be a non-empty list of objects"}
    import json
    import uuid

    from runtime import crawl_emit

    site, run_id = _active_site, f"inline-{uuid.uuid4().hex[:8]}"
    crawl_emit.init_crawl(
        site,
        run_id,
        identifier_field=identifier_field or None,
        anchor={
            "hardness": "hard",
            "estimated_total": len(records),
            "full_set": f"{len(records)} records harvested inline during exploration",
            "enumeration": "explore scraped the full set in-session",
            "termination": "full set committed",
        },
        data_definition=data_definition or None,
    )
    cr = crawl_emit.commit_records(site, run_id, records)
    if cr.get("error"):
        return {"status": "error", "error": cr["error"]}
    fin = crawl_emit.finalize_crawl(
        site,
        run_id,
        "pass",
        f"inline harvest: explore scraped the full set ({cr['total_committed']} records) "
        f"during exploration. {note}".strip(),
    )
    rp = WORKSPACES_ROOT / site / "crawl_route.json"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(
        json.dumps(
            {
                "route": "inline",
                "rationale": note or "small full set harvested inline",
                "run_id": run_id,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _mcp_log("commit_inline_dataset", run_id=run_id, records=cr["total_committed"])
    return {
        "status": "ok",
        "run_id": run_id,
        "record_count": cr["total_committed"],
        "duplicates": cr["duplicates"],
        "output_path": fin.get("output_path"),
        "outcome": fin.get("outcome"),
    }


def main() -> None:
    try:
        mcp.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main() or 0)
