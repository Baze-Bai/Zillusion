"""Browser facade, harness style.

Identical shape to the Playwright variant *plus* one extra primitive:
``run_player_script`` (exposed over MCP as ``browser_player``). The player
executes an async Python snippet against the live page with ``page``,
``context``, and ``cdp`` in scope. The snippet sets ``result`` and the
value comes back JSON-serialised.

This is deliberately permissive. The previous design conversation argued
that helper functions are a ceiling - so instead of growing helpers, we
let the agent express *anything* CDP and Playwright can do as a one-shot
Python expression. Useful for probing before committing a helper.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import traceback
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser as PWBrowser,
    BrowserContext,
    CDPSession,
    Page,
    Playwright,
    async_playwright,
)


class BrowserNotStartedError(RuntimeError):
    pass


# Max chars for a single tool payload string. The harness reads the SDK's
# stream-json with a ~1MB (1048576-byte) per-message buffer; a tool result larger
# than that crashes the WHOLE explore loop (observed live: browser_content on an
# SPA with thousands of cards returned >1MB → "JSON message exceeded maximum
# buffer size"). Cap well under 1MB to survive worst-case JSON escaping (~2x).
_TOOL_PAYLOAD_CHAR_CAP = 350_000

# Max bytes for a screenshot before base64. The image rides the SAME ~1MB
# stream-message buffer as text tool results (base64 inflates by ~1/3), so cap
# the raw bytes well under it; screenshot_image() steps PNG → JPEG → viewport to
# fit, and raises (small error result) rather than overflow the buffer.
_SCREENSHOT_BYTE_CAP = 600_000


def _cap_text(s: str) -> tuple[str, int]:
    """Return ``(possibly-truncated string, original length)``."""
    n = len(s)
    return (s[:_TOOL_PAYLOAD_CHAR_CAP], n) if n > _TOOL_PAYLOAD_CHAR_CAP else (s, n)


# JS for check_login_wall: gather ACCESS-wall signals from the live page —
# login (password field / login URL / keywords), a login MODAL sitting over
# otherwise-rich content (soft wall), a visible CAPTCHA widget, and a full-page
# JS / Cloudflare CHALLENGE interstitial. Ported + extended from the Data Agent
# MVP's _LOGIN_PAGE_JS. The Python side (_access_wall_verdict) combines these
# with a "no rich content" guard, so a normal page that merely has a header
# "Sign in" link is NOT misjudged — but a modal/captcha/challenge that gates the
# data IS, even on a content-rich page.
_ACCESS_WALL_JS = r"""
() => {
  const url = location.href;
  const lurl = url.toLowerCase();
  const hasPassword = !!document.querySelector('input[type="password"]');
  const body = document.body ? document.body.innerText : '';
  const bodyLc = body.toLowerCase();
  const title = (document.title || '').toLowerCase();
  const kw = ['sign in','log in','login','signin','register','create account','sign up','log in to'];
  const kwHits = kw.filter(k => bodyLc.includes(k));
  const urlPat = ['/login','/signin','/sign-in','/auth','/sso','/session/new','accounts.google','oauth','passport.'];
  const urlHit = urlPat.some(p => lurl.includes(p));
  const textLen = bodyLc.replace(/\s+/g, ' ').trim().length;
  const linkCount = document.querySelectorAll('a[href]').length;
  const richContent = textLen > 2000 || linkCount > 30;

  // CAPTCHA widget — visible + sizable, so an invisible reCAPTCHA v3 badge or a
  // hidden/preloaded widget does not count.
  const capSel = 'iframe[src*="recaptcha"],iframe[src*="hcaptcha"],iframe[src*="turnstile"],'
    + 'iframe[title*="captcha" i],.g-recaptcha,.h-captcha,.cf-turnstile,#h-captcha';
  let captcha = false;
  for (const el of document.querySelectorAll(capSel)) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width * r.height >= 8000) { captcha = true; break; }
  }

  // Full-page JS / Cloudflare challenge interstitial.
  const challengeEl = !!document.querySelector(
    '#challenge-form,#challenge-running,#cf-challenge-running,.cf-browser-verification,#cf-please-wait,#turnstile-wrapper');
  const challengePat = ['just a moment','checking your browser','verifying you are human',
    'attention required','请稍候','正在验证','安全验证','人机验证',"i'm not a robot",'are you human'];
  const challengeText = challengePat.some(p => bodyLc.includes(p) || title.includes(p));
  const challenge = challengeEl || (challengeText && !richContent);

  // A sizable VISIBLE login/auth modal/overlay sitting over (possibly rich)
  // content — the "soft wall" a header Sign-in link must NOT be confused with.
  const modalSel = '[role="dialog"],[aria-modal="true"],.modal,.ReactModal__Content,'
    + '[class*="login" i],[class*="signin" i],[class*="sign-in" i],[class*="signup" i],[class*="auth-modal" i]';
  let modal = false;
  for (const el of document.querySelectorAll(modalSel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 200 || r.height < 150) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') < 0.1) continue;
    const t = (el.innerText || '').toLowerCase();
    const hasPw = !!el.querySelector('input[type="password"]');
    const kwIn = kw.filter(k => t.includes(k)).length;
    if (hasPw || kwIn >= 2) { modal = true; break; }
  }

  return { url, hasPassword, kwHits, urlHit, textLen, linkCount, richContent, captcha, challenge, modal };
}
"""


def _login_wall_verdict(sig: dict[str, Any]) -> bool:
    """Pure verdict over the raw signals gathered by ``_LOGIN_WALL_JS``.

    A password field *together with* a login URL is conclusive — that pairing
    only shows up on an actual login page — so it flags a wall REGARDLESS of how
    much other content the page carries. This is what catches link-heavy portal
    login pages (e.g. Ctrip's, with 500+ nav/footer links) that would otherwise
    trip the rich-content guard and be missed.

    Otherwise fall back to a weaker login signal (password OR login URL OR >=3
    login keywords) AND no rich content. The rich-content guard (``textLen>2000``
    or ``linkCount>30``) is what stops a normal content page that merely carries
    a 'Sign in' link — or an inline login widget — from being misjudged as a
    wall.
    """
    strong = bool(sig.get("hasPassword") and sig.get("urlHit"))
    weak_signal = bool(
        sig.get("hasPassword")
        or sig.get("urlHit")
        or len(sig.get("kwHits") or []) >= 3
    )
    return strong or (weak_signal and not sig.get("richContent", False))


def _access_wall_verdict(sig: dict[str, Any]) -> tuple[bool, str]:
    """Classify what (if anything) gates the data, returning (is_wall, type).

    ``type`` is one of ``login`` / ``login_modal`` / ``captcha`` / ``challenge``
    / ``none``. All of them route to the SAME human takeover
    (``browser_request_user_login``) — the human either logs in OR solves the
    verification in the opened window, and ``browser_save_auth`` persists the
    resulting cookies (session and/or ``cf_clearance``).

    Priority:
      1. login — a real login page (``_login_wall_verdict``); ranks first so a
         login page that also shows a captcha is labelled ``login``.
      2. challenge — a full-page JS / Cloudflare interstitial.
      3. captcha — a visible captcha, but ONLY when the page is NOT content-rich
         (an incidental captcha on an otherwise-readable page is not a wall).
      4. login_modal — a login dialog/overlay over (possibly rich) content; the
         soft wall a mere header "Sign in" link must not be confused with.
    """
    if _login_wall_verdict(sig):
        return True, "login"
    if sig.get("challenge"):
        return True, "challenge"
    if sig.get("captcha") and not sig.get("richContent", False):
        return True, "captcha"
    if sig.get("modal"):
        return True, "login_modal"
    return False, "none"


# ---------- Human-takeover handshake helpers ----------


def _free_port() -> int:
    """Grab a free localhost TCP port for chromium's --remote-debugging-port."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


async def _discover_page_ws(cdp_http: str, want_url: str | None = None) -> str | None:
    """Find a page target's CDP webSocketDebuggerUrl from chromium's /json/list.
    Prefers a target whose url matches want_url, else the first non-blank page,
    else any page. Returns None if none found (the backend can re-discover)."""
    import httpx

    for _ in range(20):
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{cdp_http}/json/list", timeout=2)
                pages = [t for t in r.json() if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if pages:
                if want_url:
                    for t in pages:
                        if t.get("url") == want_url:
                            return t["webSocketDebuggerUrl"]
                for t in pages:
                    if t.get("url") not in (None, "", "about:blank"):
                        return t["webSocketDebuggerUrl"]
                return pages[0]["webSocketDebuggerUrl"]
        except Exception:  # noqa: BLE001 — endpoint may not be up yet
            pass
        await asyncio.sleep(0.25)
    return None


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class Browser:
    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self._pw: Playwright | None = None
        self._browser: PWBrowser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.samples_dir: Path | None = None
        # Auth: when set + file exists, every main context is created with this
        # storage_state, so the headless browser stays logged in across restarts.
        self.auth_state_path: Path | None = None
        # A SEPARATE headed window opened only for human login (request_user_login),
        # kept apart from the main headless browser.
        self._login_browser: PWBrowser | None = None
        self._login_context: BrowserContext | None = None
        self._login_page: Page | None = None
        # Managed Xvfb for the HEADED login/takeover browser on a headless
        # Linux sandbox (the main browser is headless and needs none).
        self._xvfb: subprocess.Popen | None = None
        # TCP relay (0.0.0.0:<relay_port> -> 127.0.0.1:<chrome cdp port>) for the
        # streamed takeover: Playwright injects --remote-debugging-pipe, so
        # Chromium binds the debug PORT to 127.0.0.1 regardless of
        # --remote-debugging-address — unreachable from the backend, which can
        # only reach the run container by its network IP. The relay bridges that.
        self._cdp_relay: asyncio.AbstractServer | None = None

    def bind_samples_dir(self, samples_dir: Path) -> None:
        self.samples_dir = samples_dir

    def bind_auth_state(self, path: Path) -> None:
        """Point the browser at workspaces/<id>/auth_state.json. If the file
        exists, the next main context is created authenticated; if not, this is
        a harmless no-op until save_auth writes it."""
        self.auth_state_path = path

    async def ensure_started(self, user_agent: str | None = None) -> None:
        # Backstop for API workflow runs: browser tools are removed from the
        # session via disallowed_tools, but if any path still reaches the
        # browser, refuse to launch one rather than silently leaving the lane.
        if os.environ.get("CRAWLER_EXPLORER_WORKFLOW") == "api":
            raise RuntimeError(
                "browser disabled: this is an API workflow run — probe over HTTP "
                "(Bash + httpx) instead; bind the workspace via workspace_attach."
            )
        # Crash-safe: if Chromium died, self.page still points at a dead object.
        # Detect and restart so the MCP server keeps working across the failure.
        if self.page is not None:
            try:
                if self._browser is not None and self._browser.is_connected():
                    return
            except Exception:
                pass
            self._pw = None
            self._browser = None
            self.context = None
            self.page = None

        self._pw = await async_playwright().start()
        # A headed main browser (CRAWLER_EXPLORER_HEADLESS=0) needs an X display.
        # The sandbox entrypoint starts a process-wide Xvfb, but ensure one here
        # too so headed explore/run is robust (and works in none-mode / if the
        # entrypoint's best-effort Xvfb didn't come up). No-op when headless or a
        # live display already exists.
        if not self.headless:
            await self._ensure_display()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        await self._new_main_context(user_agent)

    async def _new_main_context(self, user_agent: str | None = None) -> None:
        """(Re)create the main context + page. Loads auth_state.json as
        storage_state when bound + present, so the headless browser is
        authenticated after a human login (and across crash-restarts)."""
        if self._browser is None:
            raise BrowserNotStartedError("browser not launched")
        kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 800}}
        if user_agent:
            kwargs["user_agent"] = user_agent
        if self.auth_state_path is not None and self.auth_state_path.exists():
            kwargs["storage_state"] = str(self.auth_state_path)
        self.context = await self._browser.new_context(**kwargs)
        self.page = await self.context.new_page()

    async def shutdown(self) -> None:
        await self._close_login_session()
        if self.context:
            await self.context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        if self._xvfb is not None:
            try:
                self._xvfb.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._xvfb = None
        self.context = None
        self.page = None
        self._browser = None
        self._pw = None

    def _require_page(self) -> Page:
        if self.page is None:
            raise BrowserNotStartedError("call ensure_started before use")
        return self.page

    async def goto(self, url: str, wait_until: str = "domcontentloaded") -> dict[str, Any]:
        await self.ensure_started()
        page = self._require_page()
        resp = await page.goto(url, wait_until=wait_until)  # type: ignore[arg-type]
        return {
            "url": page.url,
            "status": resp.status if resp else None,
            "headers": dict(resp.headers) if resp else {},
            "title": await page.title(),
        }

    async def evaluate(self, expression: str) -> Any:
        await self.ensure_started()
        result = await self._require_page().evaluate(expression)
        # Transport guard (see _TOOL_PAYLOAD_CHAR_CAP): a giant DOM/JSON dump would
        # crash the explore loop's 1MB SDK message buffer. Only acts on huge results.
        try:
            blob = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 — non-serialisable; let FastMCP handle it
            return result
        if len(blob) > _TOOL_PAYLOAD_CHAR_CAP:
            return {
                "_truncated": True,
                "_chars_total": len(blob),
                "_preview": blob[:_TOOL_PAYLOAD_CHAR_CAP],
                "_note": "evaluate result truncated to stay under the SDK's 1MB message limit; return less / narrow the query.",
            }
        return result

    async def content(self) -> str:
        """Raw page content — used by snapshot(). Prefer content_of() at the
        tool surface so callers always opt into a budget."""
        await self.ensure_started()
        return await self._require_page().content()

    async def content_of(self, selector: str | None = None) -> dict[str, Any]:
        """Return page HTML or a CSS-selected sub-tree. No char budget here —
        the SDK's own 25 K-token tool-result cap is the backstop (it auto-
        saves the oversized result to ``tool-results/*.txt`` and tells the
        agent the path; the agent can then Read with limit or Grep for what
        it actually needs). Doubling that up at the server is redundant and
        forces a single arbitrary cut-point on every caller.

        Returns ``{"html": str, "chars_total": int, "selector": str | None}``.

        ``selector=None`` falls back to full page (``page.content()``).
        Otherwise the first matching element's ``outerHTML`` is returned;
        if the selector matches nothing the ``html`` is empty and
        ``chars_total`` is 0 (not an error — callers may want to detect
        absence as a probe signal).

        Agents should narrow with ``selector`` rather than rely on the SDK
        cap kicking in — landing on the cap costs one extra round-trip
        (auto-save + follow-up Read/Grep) and is rarely the cheapest path.
        """
        await self.ensure_started()
        page = self._require_page()
        if selector:
            html = await page.evaluate(
                "(sel) => { const el = document.querySelector(sel); "
                "return el ? el.outerHTML : ''; }",
                selector,
            )
        else:
            html = await page.content()
        capped, total = _cap_text(html or "")
        out: dict[str, Any] = {
            "html": capped,
            "chars_total": total,
            "selector": selector,
        }
        if total > len(capped):
            out["truncated"] = True
            out["note"] = (
                f"html truncated to {len(capped)} of {total} chars (server cap to "
                "stay under the SDK's 1MB stream-message limit) — narrow with a more "
                "specific `selector` to fetch just the part you need."
            )
        return out

    async def screenshot_image(
        self, *, full_page: bool = False, selector: str | None = None
    ) -> tuple[bytes, str]:
        """Bytes + format ("png"/"jpeg") of the live page — the multimodal base
        model's VISION information source.

        Captures the current VIEWPORT by default (what the page looks like right
        now); ``full_page=True`` grabs the whole scroll height; ``selector``
        screenshots just that element's bounding box. Unlike :meth:`snapshot`
        (which PERSISTS html+png to samples/ and returns filenames), this returns
        the bytes so the tool layer can hand the model an image it can SEE.

        Size is BOUNDED to stay under the SDK's ~1MB stream-message buffer AFTER
        base64 (+1/3) — the same buffer that caps ``browser_content`` and that an
        oversized tool result would overflow, crashing the explore loop. A
        ``full_page`` PNG of a long page easily blows past it, so: try PNG, then
        step down to JPEG at falling quality, then fall back to the viewport; if
        nothing fits, RAISE with guidance (FastMCP turns that into a small error
        result — far better than crashing the loop with a multi-MB payload).
        """
        await self.ensure_started()
        page = self._require_page()

        async def _shot(**kw: Any) -> bytes:
            if selector:
                el = await page.query_selector(selector)
                if el is None:
                    raise RuntimeError(f"selector matched no element: {selector!r}")
                return await el.screenshot(**kw)
            return await page.screenshot(full_page=full_page, **kw)

        png = await _shot(type="png")
        if len(png) <= _SCREENSHOT_BYTE_CAP:
            return png, "png"
        # Too big for the buffer — re-encode as JPEG at falling quality.
        jpg = png
        for q in (75, 55, 35):
            jpg = await _shot(type="jpeg", quality=q)
            if len(jpg) <= _SCREENSHOT_BYTE_CAP:
                return jpg, "jpeg"
        # Still too big (huge full_page): fall back to just the viewport.
        vp_bytes: int | None = None
        if full_page and not selector:
            vp = await page.screenshot(type="jpeg", quality=55, full_page=False)
            if len(vp) <= _SCREENSHOT_BYTE_CAP:
                return vp, "jpeg"
            vp_bytes = len(vp)
        # Report the size of every candidate that overflowed so the agent sees that
        # the viewport itself was already too big (when it was tried) rather than
        # misreading it as "just shrink to viewport".
        sizes = f"full_page={len(jpg)}B" if (full_page and not selector) else f"{len(jpg)}B"
        if vp_bytes is not None:
            sizes += f", viewport={vp_bytes}B"
        raise RuntimeError(
            f"screenshot too large to return ({sizes}) even at min quality — over the "
            f"{_SCREENSHOT_BYTE_CAP}-byte cap that keeps the tool result under the SDK "
            "buffer. Pass a `selector` to scope to one element."
        )

    @asynccontextmanager
    async def cdp_session(self):
        await self.ensure_started()
        page = self._require_page()
        if self.context is None:
            raise BrowserNotStartedError("context not initialised")
        session: CDPSession = await self.context.new_cdp_session(page)
        try:
            yield session
        finally:
            await session.detach()

    async def cdp_send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        async with self.cdp_session() as session:
            return await session.send(method, params or {})

    async def run_player_script(self, script: str) -> dict[str, Any]:
        """Execute an async Python snippet against the live page.

        Scope provided to the snippet:
            page    - the active Playwright Page
            context - the BrowserContext
            cdp     - a fresh CDPSession (auto-detached afterwards)

        The snippet must assign its output to ``result``. The returned
        value is JSON-serialised; non-serialisable values fall back to
        their repr(). Errors are caught and returned as ``ok: false``.
        """
        await self.ensure_started()
        page = self._require_page()
        if self.context is None:
            raise BrowserNotStartedError("context not initialised")
        cdp = await self.context.new_cdp_session(page)
        try:
            wrapper = (
                "import json\n"
                "async def __player(page, context, cdp):\n"
                "    result = None\n"
            )
            indented = "\n".join("    " + line for line in script.splitlines())
            wrapper += indented + "\n    return result\n"
            ns: dict[str, Any] = {}
            exec(wrapper, ns)  # noqa: S102 - by design, agent-supplied
            value = await ns["__player"](page, self.context, cdp)
            safe = _safe_json(value)
            # Transport guard: a giant serialized result would blow the SDK's ~1MB
            # message buffer and crash the explore loop. Truncate to a JSON string.
            blob = json.dumps(safe, ensure_ascii=False, default=str)
            if len(blob) > _TOOL_PAYLOAD_CHAR_CAP:
                return {
                    "ok": True,
                    "result": blob[:_TOOL_PAYLOAD_CHAR_CAP],
                    "result_truncated": True,
                    "result_chars_total": len(blob),
                    "note": "result truncated to stay under the SDK's 1MB message limit; return less data from the player script.",
                }
            return {"ok": True, "result": safe}
        except Exception as exc:  # noqa: BLE001
            # Include the traceback so a failing agent-supplied player snippet
            # is debuggable (surfaced to the agent as the tool result AND
            # captured in the SDK-side run log). Player failures are exactly
            # the kind of thing the update pipeline needs to localise.
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        finally:
            # Bounded: if the player script hangs, the tool-level timeout cancels
            # us and THIS cleanup runs DURING cancellation — an un-bounded detach
            # against a wedged CDP socket would re-hang and defeat the timeout.
            with suppress(Exception):
                await asyncio.wait_for(cdp.detach(), timeout=5)

    # ---------- Auth / human login ----------

    async def check_login_wall(self) -> dict[str, Any]:
        """Access-wall detector: login, login modal, visible captcha, or a
        full-page JS / Cloudflare challenge. Combines login SIGNALS (password
        field / login URL / repeated keywords) with a 'no rich content' guard —
        a password field AT a login URL is conclusive and bypasses the guard (so
        link-heavy portal login pages, Ctrip-style 500+ links, are caught), and
        a login modal / captcha / challenge is flagged even on a content-rich
        page. See ``_access_wall_verdict``.

        Returns ``{is_access_wall, wall_type, is_login_wall, current_url,
        signals}``. ``wall_type`` is one of login / login_modal / captcha /
        challenge / none. ``is_login_wall`` is kept for back-compat (real-login
        only); prefer ``is_access_wall`` + ``wall_type``."""
        await self.ensure_started()
        sig = await self._require_page().evaluate(_ACCESS_WALL_JS)
        is_wall, wall_type = _access_wall_verdict(sig)
        return {
            "is_access_wall": is_wall,
            "wall_type": wall_type,
            "is_login_wall": _login_wall_verdict(sig),
            "current_url": sig.get("url"),
            "signals": sig,
        }

    def _x_display_ready(self) -> bool:
        """True iff an X server is ACTUALLY listening at $DISPLAY. A set DISPLAY
        env or a since-died Xvfb is not enough — both yield 'no XServer' at launch.
        Checks the server's unix socket exists (``/tmp/.X11-unix/X<n>``)."""
        m = re.match(r"[^:]*:(\d+)", os.environ.get("DISPLAY", ""))
        return bool(m) and os.path.exists(f"/tmp/.X11-unix/X{m.group(1)}")

    async def _ensure_display(self) -> None:
        """Headed Chromium (request_user_login / takeover) needs an X display. On
        a headless Linux sandbox there is none, so start a managed Xvfb and point
        DISPLAY at it. No-op on a natively-windowed OS (Windows/macOS) or when an X
        server is already live. Without this, headless=False fails at launch with
        'Missing X server or $DISPLAY'."""
        if os.name == "nt" or sys.platform == "darwin":
            return
        if self._x_display_ready():
            return
        # (Re)start Xvfb if ours never started or has since died.
        if self._xvfb is not None and self._xvfb.poll() is not None:
            self._xvfb = None
        if self._xvfb is None:
            try:
                self._xvfb = subprocess.Popen(
                    ["Xvfb", ":99", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError as exc:
                # Xvfb not installed — fail LOUDLY so the caller can surface a
                # clear reason (caught by request_user_takeover/login) instead of
                # an opaque chromium 'Missing X server' raised later.
                raise RuntimeError(
                    "Xvfb binary not found — cannot provide an X display for the "
                    "headed login/takeover browser"
                ) from exc
            os.environ["DISPLAY"] = ":99"
        # WAIT for the server to actually be ready (socket appears). A fixed sleep
        # races under load — Chromium launching first yields 'no XServer'.
        for _ in range(60):  # up to ~6s
            if os.path.exists("/tmp/.X11-unix/X99"):
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(
            "Xvfb started but its X11 socket /tmp/.X11-unix/X99 never appeared "
            "within ~6s — the headed browser launch would fail with 'no X server'"
        )

    async def request_user_login(self, login_url: str, *, seed_auth: str | None = None) -> dict[str, Any]:
        """Open a SEPARATE visible (headed) Chromium window at login_url for a
        human to log in. The main headless browser is untouched. Returns once
        the window is open; the agent then asks the user to log in and, next
        turn, calls save_auth. seed_auth (an existing auth_state.json) pre-seeds
        the window so a partially valid session is reused."""
        await self.ensure_started()
        if self._pw is None:
            return {"opened": False, "reason": "playwright not started"}
        await self._close_login_session()
        try:
            await self._ensure_display()
            self._login_browser = await self._pw.chromium.launch(headless=False)
        except Exception as exc:  # noqa: BLE001 — no X display / headed launch failed
            await self._close_login_session()
            return {
                "opened": False,
                "reason": f"could not open the headed login browser: {type(exc).__name__}: {exc}",
            }
        ctx_kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 900}}
        if seed_auth and Path(seed_auth).exists():
            ctx_kwargs["storage_state"] = seed_auth
        self._login_context = await self._login_browser.new_context(**ctx_kwargs)
        self._login_page = await self._login_context.new_page()
        try:
            await self._login_page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001 — window is open; the user can retry nav
            return {"opened": True, "url": login_url, "warning": f"{type(exc).__name__}: {exc}"}
        return {"opened": True, "url": login_url}

    async def _start_cdp_relay(self, listen_port: int, target_port: int) -> None:
        """Expose Chromium's CDP debug port (bound to 127.0.0.1 because Playwright
        forces --remote-debugging-pipe, which makes --remote-debugging-address a
        no-op) on ``0.0.0.0:<listen_port>`` so the backend can reach it over the
        run container's network IP (the sandbox-takeover bridge). A transparent
        TCP proxy — forwards BOTH the ``/json`` HTTP endpoint and the
        ``/devtools/page`` WebSocket alike."""
        await self._close_cdp_relay()

        async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while True:
                    chunk = await reader.read(65536)
                    if not chunk:
                        break
                    writer.write(chunk)
                    await writer.drain()
            except Exception:  # noqa: BLE001 — peer closed / reset is normal
                pass
            finally:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass

        async def _handle(c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter) -> None:
            try:
                u_reader, u_writer = await asyncio.open_connection("127.0.0.1", target_port)
            except Exception:  # noqa: BLE001 — chromium debug port gone
                try:
                    c_writer.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            t1 = asyncio.create_task(_pipe(c_reader, u_writer))
            t2 = asyncio.create_task(_pipe(u_reader, c_writer))
            try:
                await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in (t1, t2):
                    t.cancel()

        self._cdp_relay = await asyncio.start_server(_handle, "0.0.0.0", listen_port)

    async def _close_cdp_relay(self) -> None:
        if self._cdp_relay is not None:
            try:
                self._cdp_relay.close()
                await self._cdp_relay.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._cdp_relay = None

    async def request_user_takeover(
        self,
        login_url: str,
        *,
        request_path: Path,
        auth_path: Path,
        reason: str = "",
        message: str = "",
        wall_type: str = "",
        seed_auth: str | None = None,
        timeout_s: float = 600.0,
        poll_s: float = 1.0,
    ) -> dict[str, Any]:
        """Streamed human takeover for orchestrator/web-UI runs (no operator at
        the server). Opens the headed ``_login_browser`` with a CDP debug port,
        writes a handshake file (``request_path``) the backend reads to bridge a
        live screencast + input to the remote user, BLOCKS until the user marks
        it done (or ``timeout_s``), then ``save_auth`` (persist cookies to
        ``auth_path`` + re-auth the main browser) and clean up. Covers login,
        captcha, and Cloudflare/JS challenges alike — whatever the user resolves
        in the streamed window. Returns the agent-facing result."""
        await self.ensure_started()
        if self._pw is None:
            return {"ok": False, "reason": "playwright not started"}
        await self._close_login_session()
        port = _free_port()
        viewport = {"width": 1280, "height": 900}
        nav_warning = None
        # An X display + a successful HEADED launch + CDP discovery are ALL
        # prerequisites for the streamed canvas. If any fails (e.g. no X server),
        # do NOT dead-end: write a `failed` handshake so the backend can tell the
        # user, and return a clear result to the agent instead of throwing (an
        # uncaught throw here wrote no handshake -> the canvas never appeared and
        # the agent misread the error as a permanent "no X server" block).
        try:
            await self._ensure_display()
            # Bind the debug port on 0.0.0.0 + allow any WS origin so the backend
            # can attach to CDP from OUTSIDE this container (under the sandbox it
            # bridges over a dedicated docker network — see services/sandbox.py).
            # Chrome >=111 rejects cross-origin CDP WebSockets without
            # --remote-allow-origins. In none-mode (same netns) it's harmless.
            self._login_browser = await self._pw.chromium.launch(
                headless=False,
                args=[
                    f"--remote-debugging-port={port}",
                    "--remote-debugging-address=0.0.0.0",
                    "--remote-allow-origins=*",
                ],
            )
            ctx_kwargs: dict[str, Any] = {"viewport": viewport}
            if seed_auth and Path(seed_auth).exists():
                ctx_kwargs["storage_state"] = seed_auth
            self._login_context = await self._login_browser.new_context(**ctx_kwargs)
            self._login_page = await self._login_context.new_page()
            try:
                await self._login_page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:  # noqa: BLE001 — window is open; the user can retry nav
                nav_warning = f"{type(exc).__name__}: {exc}"
            cdp_http_local = f"http://127.0.0.1:{port}"
            cdp_ws = await _discover_page_ws(cdp_http_local, self._login_page.url)
            # Chromium bound the debug port to 127.0.0.1 (Playwright's pipe makes
            # --remote-debugging-address a no-op), so relay it on 0.0.0.0 — the
            # backend reaches the run container only by its network IP.
            relay_port = _free_port()
            await self._start_cdp_relay(relay_port, port)
        except Exception as exc:  # noqa: BLE001 — display / headed launch / CDP setup failed
            await self._close_login_session()
            err = f"{type(exc).__name__}: {exc}"
            _atomic_write_json(request_path, {
                "status": "failed",
                "error": err,
                "reason": reason,
                "message": message,
                "wall_type": wall_type,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            return {
                "ok": False,
                "reason": f"could not open the headed login browser for takeover: {err}",
                "wall_type": wall_type,
            }

        payload: dict[str, Any] = {
            "status": "pending",
            "reason": reason,
            "message": message,
            "wall_type": wall_type,
            "page_url": self._login_page.url,
            "cdp_http": f"http://127.0.0.1:{relay_port}",
            "cdp_ws": cdp_ws,
            # Raw RELAY port so the orchestrator re-advertises cdp_http at the run
            # container's takeover-network IP (sandbox bridge) without parsing.
            "cdp_port": relay_port,
            "viewport": viewport,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if nav_warning:
            payload["nav_warning"] = nav_warning
        _atomic_write_json(request_path, payload)

        # Block until the backend flips status -> "done" (user finished) or timeout.
        waited, done = 0.0, False
        while waited < timeout_s:
            await asyncio.sleep(poll_s)
            waited += poll_s
            try:
                cur = json.loads(request_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if cur.get("status") == "done":
                done = True
                break

        if not done:
            await self._close_login_session()
            try:
                request_path.unlink(missing_ok=True)
            except OSError:
                pass
            return {"ok": False, "reason": f"takeover timed out after {timeout_s:.0f}s (no user action)",
                    "wall_type": wall_type}

        # User finished -> save_auth merges cookies, re-auths main browser, closes login session.
        saved = await self.save_auth(auth_path)
        try:
            request_path.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": True, "completed": True, "saved": saved,
                "wall_type": wall_type, "page_url": payload["page_url"]}

    async def save_auth(self, path: str | Path) -> dict[str, Any]:
        """Export the login window's storage_state and MERGE it into the global
        `path` — cookies/localStorage ACCUMULATE across sites, so logging into a
        second site does NOT wipe the first (same domain re-login refreshes).
        Then close the window and rebuild the MAIN context so it's
        authenticated. The merged file is reused by workflow.py + the validator."""
        if self._login_context is None:
            return {"saved": False, "reason": "no active login window; call request_user_login first"}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        new_state = await self._login_context.storage_state()
        merged = _merge_storage_state(_load_json(path), new_state)
        path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        await self._close_login_session()
        self.auth_state_path = path
        if self._browser is not None:
            try:
                if self.context:
                    await self.context.close()
            except Exception:  # noqa: BLE001
                pass
            await self._new_main_context()
        return {"saved": True, "path": str(path), "total_cookies": len(merged.get("cookies") or [])}

    async def _close_login_session(self) -> None:
        await self._close_cdp_relay()
        for obj in (self._login_context, self._login_browser):
            try:
                if obj is not None:
                    await obj.close()
            except Exception:  # noqa: BLE001
                pass
        self._login_context = self._login_page = self._login_browser = None

    async def snapshot(self, name: str) -> dict[str, str]:
        await self.ensure_started()
        if self.samples_dir is None:
            raise RuntimeError("samples_dir not bound; call browser_attach first")
        page = self._require_page()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe = name.replace("/", "_").replace("\\", "_")
        html_path = self.samples_dir / f"{ts}_{safe}.html"
        png_path = self.samples_dir / f"{ts}_{safe}.png"
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(png_path), full_page=True)
        return {"html": html_path.name, "screenshot": png_path.name, "url": page.url}


def _safe_json(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:  # noqa: BLE001
        return repr(value)


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/corrupt → start fresh
        return {}


def _merge_storage_state(old: dict, new: dict) -> dict:
    """Merge two Playwright storage_states so the global auth file ACCUMULATES
    logins across sites. Cookies keyed by (name, domain, path); localStorage by
    origin; the new login wins on conflict (same-domain re-login refreshes)."""
    cookies: dict = {}
    for c in (old or {}).get("cookies") or []:
        cookies[(c.get("name"), c.get("domain"), c.get("path"))] = c
    for c in (new or {}).get("cookies") or []:
        cookies[(c.get("name"), c.get("domain"), c.get("path"))] = c
    origins: dict = {}
    for o in (old or {}).get("origins") or []:
        origins[o.get("origin")] = o
    for o in (new or {}).get("origins") or []:
        origins[o.get("origin")] = o
    return {"cookies": list(cookies.values()), "origins": list(origins.values())}
