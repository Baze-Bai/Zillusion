r"""Interactive human-takeover smoke test — config-driven (GitHub + Ctrip).

Drives the REAL login-wall -> human-takeover -> save-auth -> reuse round-trip,
the same sequence the harness agent runs (browser.py + the hypothesis-loop
SKILL "Auth walls" section), as a watchable script. The deterministic parts are
covered by the pytest suite in tests/; this is the one path that needs a human
+ a visible browser.

Two "am I logged in?" strategies, picked per site:
  * cookie model (GitHub): an authoritative boolean cookie (logged_in=yes).
  * redirect model (Ctrip): request a login-walled page; you're authed iff it
    does NOT bounce to the login host. No site cookie scheme to guess — this is
    exactly the login-wall semantics the harness cares about.

Run (project venv):

    .venv\Scripts\python.exe tests\manual\login_takeover_driver.py --site ctrip
    .venv\Scripts\python.exe tests\manual\login_takeover_driver.py --site github

By default the session is written to a SCRATCH file on D: (outside the repo) so
your real project auth_state.json is untouched. Pass --write-project-auth to
authenticate the harness for real (writes <root>/auth_state.json, gitignored).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[2]
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

from mcp_server.browser import Browser  # noqa: E402

# Per-site config. `login_markers` are url substrings meaning "this is the login
# page / you are logged OUT". `auth_cookie` (name, value|None) enables the cookie
# model; when None the redirect model is used.
SITES: dict[str, dict] = {
    "github": {
        "login_url": "https://github.com/login",
        "walled_url": "https://github.com/settings/profile",
        "login_markers": ["/login"],
        "auth_cookie": ("logged_in", "yes"),
    },
    "ctrip": {
        "login_url": "https://passport.ctrip.com/user/login?backurl=https%3A%2F%2Fmy.ctrip.com%2F",
        "walled_url": "https://my.ctrip.com",
        "login_markers": ["passport.ctrip.com", "/user/login"],
        "auth_cookie": None,  # redirect model — Ctrip has no clean boolean cookie
    },
}


def _on_login_page(url: str | None, markers: list[str]) -> bool:
    u = (url or "").lower()
    return any(m.lower() in u for m in markers)


async def _auth_cookie_present(ctx, spec: tuple[str, str | None]) -> bool:
    name, value = spec
    try:
        cookies = await ctx.cookies()
    except Exception:
        return False
    for c in cookies:
        if c.get("name") == name and (c.get("value") == value if value is not None else bool(c.get("value"))):
            return True
    return False


async def _main_is_authed(browser: Browser, site: dict) -> tuple[bool, str]:
    """Navigate the MAIN browser to the walled page; authed iff it does NOT
    bounce to the login host. The authoritative reuse check."""
    await browser.goto(site["walled_url"], wait_until="domcontentloaded")
    await browser.page.wait_for_timeout(2500)
    url = browser.page.url
    return (not _on_login_page(url, site["login_markers"]), url)


async def _login_window_authed(browser: Browser, site: dict) -> bool:
    """Poll signal on the HEADED login window (don't navigate it — the user is
    in it). Cookie model if configured, else: it left the login host."""
    spec = site.get("auth_cookie")
    if spec is not None:
        return await _auth_cookie_present(browser._login_context, spec)
    try:
        return not _on_login_page(browser._login_page.url, site["login_markers"])
    except Exception:
        return False


def _banner(text: str) -> None:
    print("\n" + "=" * 72 + f"\n  {text}\n" + "=" * 72)


async def run(site_name: str, site: dict, auth_path: Path, login_timeout: int = 600) -> int:
    login_url, walled_url = site["login_url"], site["walled_url"]
    results: list[tuple[str, bool]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    browser = Browser(headless=True)
    browser.bind_auth_state(auth_path)
    await browser.ensure_started()
    print(f"[{site_name}] main (headless) browser started. Auth store -> {auth_path}")

    try:
        # --- 1a. DETECTOR PROBE (informational) -----------------------------
        _banner(f"STEP 1  detect the login wall ({login_url})")
        await browser.goto(login_url, wait_until="domcontentloaded")
        await browser.page.wait_for_timeout(2000)
        wall = await browser.check_login_wall()
        sig = wall.get("signals", {})
        print(f"  current_url   = {wall.get('current_url')}")
        print(f"  is_login_wall = {wall.get('is_login_wall')}")
        print(f"  signals: hasPassword={sig.get('hasPassword')} urlHit={sig.get('urlHit')} "
              f"kwHits={len(sig.get('kwHits') or [])} textLen={sig.get('textLen')} "
              f"linkCount={sig.get('linkCount')} richContent={sig.get('richContent')}")
        if not wall.get("is_login_wall") and sig.get("hasPassword") and sig.get("urlHit"):
            print("  >>> DETECTOR FINDING: a real login page (password field + login URL) was "
                  "NOT flagged — the richContent guard "
                  f"(linkCount={sig.get('linkCount')}>30) suppressed it.")
        print(f"  detector verdict recorded (informational): is_login_wall={wall.get('is_login_wall')}")

        # --- 1b. baseline: the walled page bounces us to login --------------
        authed_before, url_before = await _main_is_authed(browser, site)
        print(f"  baseline {walled_url} -> {url_before}")
        check("login wall bounces logged-out user to login", not authed_before,
              "did not bounce — already authed? delete the scratch auth file" if authed_before else "")

        # --- 2. HUMAN TAKEOVER ----------------------------------------------
        _banner("STEP 2  HUMAN TAKEOVER — a visible browser window will open")
        seed = str(auth_path) if auth_path.exists() else None
        opened = await browser.request_user_login(login_url, seed_auth=seed)
        check("headed login window opened", opened.get("opened") is True, str(opened))
        if not opened.get("opened"):
            return 1

        print(f"\n  >>> A separate Chrome window is open at the {site_name} login page.")
        print("  >>> Log in there (password / SMS code / QR scan — whatever it asks).")
        print(f"  >>> Land back on {walled_url} (or your account home).\n")
        if sys.stdin and sys.stdin.isatty():
            input("  Press Enter once you have finished logging in... ")
        else:
            print(f"  (non-interactive stdin — polling the login window until you're logged in, "
                  f"up to {login_timeout}s)")
            waited = 0
            while waited < login_timeout:
                if await _login_window_authed(browser, site):
                    print(f"  detected login after ~{waited}s")
                    break
                if waited and waited % 30 == 0:
                    try:
                        print(f"    ...still waiting; login window at {browser._login_page.url}")
                    except Exception:
                        pass
                await asyncio.sleep(5)
                waited += 5
            else:
                print("  timed out waiting for login; saving whatever state exists so far")

        # --- 3. save + re-auth main -----------------------------------------
        _banner("STEP 3  save_auth — merge session + rebuild main context")
        saved = await browser.save_auth(auth_path)
        print(f"  saved={saved.get('saved')} total_cookies={saved.get('total_cookies')} "
              f"path={saved.get('path')}")
        check("auth saved", saved.get("saved") is True, str(saved.get("reason", "")))
        check("auth_state.json written", auth_path.exists(), str(auth_path))
        check("session cookies captured", (saved.get("total_cookies") or 0) > 0,
              f"{saved.get('total_cookies')} cookies")

        # --- 4. reuse: the headless main browser is now authenticated -------
        _banner("STEP 4  reuse — re-probe the headless main browser")
        authed_after, url_after = await _main_is_authed(browser, site)
        print(f"  {walled_url} -> {url_after}")
        check("main browser reaches walled page after save_auth (no bounce)", authed_after,
              f"landed on {url_after}")

    finally:
        await browser.shutdown()

    _banner("SUMMARY")
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n  {passed}/{len(results)} checks passed")
    print(f"  auth store: {auth_path}  (holds LIVE session cookies — treat as secret)")
    return 0 if passed == len(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Interactive human-takeover login smoke test")
    ap.add_argument("--site", choices=sorted(SITES), default="github")
    ap.add_argument("--login-url", default=None, help="override the site's login URL")
    ap.add_argument("--walled-url", default=None, help="override the site's login-walled URL")
    ap.add_argument("--auth-out", default=None, help="where to write the session (default: D: scratch)")
    ap.add_argument("--write-project-auth", action="store_true",
                    help="write the REAL <root>/auth_state.json instead (authenticates the harness)")
    ap.add_argument("--login-timeout", type=int, default=600,
                    help="seconds to wait for login when run non-interactively")
    args = ap.parse_args()

    site = dict(SITES[args.site])
    if args.login_url:
        site["login_url"] = args.login_url
    if args.walled_url:
        site["walled_url"] = args.walled_url

    if args.write_project_auth:
        auth_path = HARNESS_ROOT / "auth_state.json"
    elif args.auth_out:
        auth_path = Path(args.auth_out)
    else:
        auth_path = Path(r"D:\Zillusion_work\login_takeover_test") / f"{args.site}_auth_state.json"
    auth_path.parent.mkdir(parents=True, exist_ok=True)

    return asyncio.run(run(args.site, site, auth_path, args.login_timeout))


if __name__ == "__main__":
    raise SystemExit(main())
