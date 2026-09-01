"""robots.txt facts + per-host pacing for harness-owned navigation/fetch tools.

Scope: the MCP-managed browser (``browser_goto``) and the harness URL tools
(``url_harvest`` / ``url_probe_range``). The standalone ``workflow.py``
subprocess dials its own Playwright and is NOT covered here — unifying that
is the Policy Gate's job (plan item D12); this module is deliberately the
smaller explore/agentic-facing increment.

Modes — ``CRAWLER_ROBOTS_MODE`` (read per call; forwarded into the sandbox).
Per the user ruling of 2026-07-21 (§10) robots ENFORCEMENT is stopped: the
default is now ``off`` and the code/design is retained so it can be cheaply
restored later (set ``warn``/``enforce`` to re-enable). Sitemap discovery is a
SEPARATE, retained path — ``url_harvest`` still reads robots.txt but ONLY to
extract ``Sitemap:`` directives (§10.6 ruling one, via
``url_harvest.parse_robots_sitemaps``); it never parses Disallow / Allow /
Crawl-delay. This module is the ENFORCEMENT side only and is inert by default.

* ``off`` (default): no robots fetch in the nav/split paths, no rule
  application. ``robots_check`` still answers on demand (explicit facts
  requests always fetch). PACING STILL APPLIES (see below).
* ``warn``: FACTS, not fences — results carry disallowed counts / warnings,
  nothing is blocked. Matches the harness prompt philosophy: the explore
  agent is told the truth and decides.
* ``enforce``: ``browser_goto`` refuses a disallowed URL; the url tools drop
  disallowed URLs before fetching/probing. The unattended monitor path
  (scheduled agentic re-crawls) can opt into this.

Per-host pacing (``browser_goto`` only): consecutive same-host navigations
stay >= ``CRAWLER_MIN_HOST_INTERVAL_S`` (default 1s) apart. This is DECOUPLED
from the robots mode — it runs even when robots is ``off`` — because it
protects US from being rate-limited / IP-blocked, NOT the site's robots
courtesy. Crawl-delay was removed from the gap per §10 (set
``CRAWLER_MIN_HOST_INTERVAL_S=0`` to disable pacing entirely).
``url_probe_range`` is deliberately NOT paced — enumeration probing is burst-y
by design and bounded by its own concurrency + tool timeout.

Rule cache (``_parsers``) is keyed by fetch-time MODE (§10 mode isolation): a
parser fetched under one mode is never reused under another. So a robots.txt
pulled while ``off`` (e.g. by an explicit ``robots_check``) can NEVER be revived
as an enforced rule after an operator flips the mode back to ``warn`` /
``enforce`` — the mismatched cache entry is treated as a miss and refetched.

Fetch semantics: robots.txt is fetched once per origin+mode (TTL 24h, in-process
only — one MCP server == one run session, so a disk cache would only add
staleness). Any non-200 / network error is treated as "no restrictions"
(permissive, ``fetched=False`` in the verdict): the 2019 REP treats 4xx as
unrestricted, and we extend that to 5xx rather than wedge a run on a flaky
robots endpoint. Fetching robots.txt / sitemaps is itself exempt from robots
rules by convention.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

# KNOWN LIMITS OF THE PARSER THIS USES, measured rather than assumed. Both make
# it MISS a rule (report allowed where a modern parser would disallow); neither
# invents a restriction. Read this before treating a verdict as authoritative:
#
#   1. A blank line between `User-agent:` and its `Disallow:` lines ends the
#      record, so the rules that follow are dropped. GitHub's robots.txt is
#      written that way, and stdlib therefore reports its whole `User-agent: *`
#      section as empty — every path "allowed".
#   2. `*` and `$` in a path are not wildcards here, they are literal
#      characters. `Disallow: /*/private` does not block `/a/private`.
#
# Consequence, stated plainly so nobody has to discover it the hard way: an
# `allowed` verdict from this module means "no rule I could parse forbids it",
# NOT "the site permits it". Anyone who needs true conformance should put a
# spec-complete parser behind `RobotsPolicy(fetch=...)`; the fetch/verdict/pace
# structure here does not change.

def origin_of(url: str) -> str:
    """scheme://host for a URL (defaults to https when no scheme).

    Inlined rather than imported: upstream this lives in ``mcp_server.url_harvest``,
    a module this build does not ship, and it is four lines of stdlib.
    """
    p = urlparse(url if "://" in url else "https://" + url)
    return f"{p.scheme or 'https'}://{p.netloc}"

_MODE_ENV = "CRAWLER_ROBOTS_MODE"
_MIN_INTERVAL_ENV = "CRAWLER_MIN_HOST_INTERVAL_S"
_VALID_MODES = ("warn", "enforce", "off")

# Crawl-delay cap: a hostile "Crawl-delay: 3600" must slow us down, not wedge
# the run for an hour per navigation.
ROBOTS_MAX_DELAY_S = 10.0
# REP requires parsers to handle at least 500 KiB; bound hostile payloads.
_ROBOTS_MAX_BYTES = 512 * 1024
_FETCH_TIMEOUT_S = 8.0


def robots_mode(env: dict[str, str] | None = None) -> str:
    """warn | enforce | off — unset/unrecognized values resolve to ``warn``.

    ``warn`` is the default in this build: robots.txt is fetched and consulted
    on every navigation, a disallowed URL is recorded and surfaced on the tool
    result, and the crawl proceeds. Per-host pacing applies in every mode
    including ``off``.

    Why warn and not enforce: the operator, not this library, is the one who
    knows whether a given crawl is authorised — a site's robots.txt speaks to
    anonymous crawlers, and does not describe, say, a logged-in export of your
    own data. Warn keeps the fact in front of you without silently deciding it
    for you. Set ``CRAWLER_ROBOTS_MODE=enforce`` and a disallowed URL is
    refused instead; ``off`` skips the check entirely.

    Whichever mode you pick, the obligation is yours: honour robots.txt and the
    site's terms, and obey the law that applies to you.
    """
    e: Any = env if env is not None else os.environ
    raw = (e.get(_MODE_ENV) or "warn").strip().lower()
    return raw if raw in _VALID_MODES else "warn"


def min_host_interval_s(env: dict[str, str] | None = None) -> float:
    e: Any = env if env is not None else os.environ
    try:
        v = float(e.get(_MIN_INTERVAL_ENV) or 1.0)
    except (TypeError, ValueError):
        v = 1.0
    return max(0.0, v)


async def _default_fetch(origin: str) -> str | None:
    """GET <origin>/robots.txt; None on any non-200 / error (→ permissive)."""
    import httpx

    # Optional: upstream routes this through the egress-proxy layer, which this
    # build does not ship. Absent it, fetch robots.txt directly — the same way
    # every other request in this build already goes out.
    try:
        from runtime.proxy import httpx_client_kwargs

        client_kwargs = httpx_client_kwargs(httpx)
    except ImportError:
        client_kwargs = {}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=_FETCH_TIMEOUT_S, **client_kwargs
        ) as c:
            r = await c.get(origin + "/robots.txt")
        if r.status_code != 200:
            return None
        return r.text[:_ROBOTS_MAX_BYTES]
    except Exception:  # noqa: BLE001 — unreachable robots == no restrictions
        return None


@dataclass
class RobotsVerdict:
    """One URL's robots facts under the current mode."""

    allowed: bool
    crawl_delay: float | None
    origin: str
    fetched: bool  # robots.txt was fetched + parsed (False == permissive default)
    mode: str

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "crawl_delay": self.crawl_delay,
                "origin": self.origin, "fetched": self.fetched, "mode": self.mode}


class RobotsPolicy:
    """Per-origin robots cache + per-host navigation pacer (one per MCP server).

    ``fetch`` / ``clock`` / ``sleep`` are injectable for tests; production uses
    httpx / time.monotonic / asyncio.sleep.
    """

    def __init__(
        self,
        *,
        user_agent: str = "*",
        ttl_s: float = 24 * 3600.0,
        fetch: Callable[[str], Awaitable[str | None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._ua = user_agent
        self._ttl_s = ttl_s
        self._fetch = fetch or _default_fetch
        self._clock = clock
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        # origin -> (fetched_at_monotonic, parser | None-when-unfetchable,
        # fetched_mode). fetched_mode keys the cache per §10 mode isolation: a
        # parser pulled under one mode is never reused under another (a robots.txt
        # fetched while off by robots_check can't revive as an enforced rule).
        self._parsers: dict[str, tuple[float, RobotFileParser | None, str]] = {}
        self._fetch_locks: dict[str, asyncio.Lock] = {}
        self._last_nav: dict[str, float] = {}
        self._pace_locks: dict[str, asyncio.Lock] = {}
        self._violations: list[str] = []

    @property
    def mode(self) -> str:
        # Read per call (not cached at init) so the forwarded sandbox env and
        # tests see changes without rebuilding the singleton.
        return robots_mode()

    def _cache_hit(self, origin: str, mode: str) -> tuple[float, RobotFileParser | None, str] | None:
        """A live cache entry for ``origin`` fetched under the SAME ``mode`` and
        within TTL — else None (a different-mode entry is a miss, not a hit, so
        §10 mode isolation holds)."""
        cached = self._parsers.get(origin)
        if cached is not None and cached[2] == mode and self._clock() - cached[0] < self._ttl_s:
            return cached
        return None

    async def _parser_for(self, origin: str, *, mode: str) -> RobotFileParser | None:
        hit = self._cache_hit(origin, mode)
        if hit is not None:
            return hit[1]
        lock = self._fetch_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            hit = self._cache_hit(origin, mode)
            if hit is not None:
                return hit[1]
            text = await self._fetch(origin)
            parser: RobotFileParser | None = None
            if text:
                parser = RobotFileParser()
                parser.parse(text.splitlines())
            self._parsers[origin] = (self._clock(), parser, mode)
            return parser

    async def check(self, url: str, *, facts: bool = False) -> RobotsVerdict:
        """Robots verdict for one URL. ``facts=True`` (the robots_check tool)
        fetches even in off mode — an explicit facts request always answers."""
        mode = self.mode
        origin = origin_of(url) if url else ""
        if not origin or (mode == "off" and not facts):
            return RobotsVerdict(True, None, origin, False, mode)
        parser = await self._parser_for(origin, mode=mode)
        if parser is None:
            return RobotsVerdict(True, None, origin, False, mode)
        allowed = parser.can_fetch(self._ua, url)
        delay_raw = parser.crawl_delay(self._ua)
        delay = min(float(delay_raw), ROBOTS_MAX_DELAY_S) if delay_raw is not None else None
        return RobotsVerdict(allowed, delay, origin, True, mode)

    async def pace(self, url: str, *, crawl_delay: float | None = None) -> float:
        """Sleep so consecutive same-host navigations stay >= the min host
        interval apart. Returns the seconds actually waited; no-op for hostless
        URLs.

        DECOUPLED from the robots mode (§10): pacing runs even when robots is
        ``off`` because it is self-protection (avoid tripping rate-limit / IP
        block), NOT a robots courtesy. ``crawl_delay`` is accepted for call-site
        back-compat but IGNORED — Crawl-delay was removed from the gap per §10;
        the gap is ``min_host_interval_s()`` only (set it to 0 to disable
        pacing entirely)."""
        host = urlparse(url if "://" in url else "https://" + url).netloc
        if not host:
            return 0.0
        gap = min_host_interval_s()
        lock = self._pace_locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._last_nav.get(host)
            wait = 0.0 if last is None else max(0.0, last + gap - self._clock())
            if wait > 0:
                await self._sleep(wait)
            self._last_nav[host] = self._clock()
            return wait

    async def split(self, urls: list[str]) -> tuple[list[str], list[str]]:
        """(allowed, disallowed) for a URL list — robots fetched once per unique
        origin (the per-URL check is a local rule match). Off mode: all allowed."""
        if not urls or self.mode == "off":
            return list(urls), []
        allowed: list[str] = []
        disallowed: list[str] = []
        for u in urls:
            v = await self.check(u)
            (allowed if v.allowed else disallowed).append(u)
        return allowed, disallowed

    def note_violation(self, url: str) -> None:
        """Record a warn-mode navigation to a disallowed URL (post-hoc audit)."""
        self._violations.append(url)

    def stats(self) -> dict[str, Any]:
        return {"mode": self.mode, "violations": len(self._violations),
                "violation_sample": self._violations[:10]}
