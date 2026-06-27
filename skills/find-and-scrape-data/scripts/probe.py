#!/usr/bin/env python3
"""probe — inspect a page or endpoint before writing a scraper (credential-safe).

httpx GET (or --render via Playwright for JS pages), then report the signals a
scraper author needs:
  - status, final URL, content-type
  - kind: json vs html
  - for JSON: the top-level keys / array length
  - for HTML: counts of <table>, JSON-LD blocks, __NEXT_DATA__,
    <script type="application/json">, <form>, <a>; likely pagination params
  - a short body preview
Never prints Authorization / Cookie / X-API-Key / Set-Cookie headers.

Usage:
    python probe.py <url> [--render] [--max-bytes N] [--timeout S]

Output (stdout): a JSON report.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

_SECRET_HDRS = {"authorization", "cookie", "set-cookie", "x-api-key", "proxy-authorization"}
_PAGINATION_RE = re.compile(r'[?&](page|offset|cursor|start|page_?no|page_?num|per_page|limit)=')


def analyze_html(text: str) -> dict:
    low = text.lower()
    return {
        "tables": low.count("<table"),
        "json_ld": low.count("application/ld+json"),
        "next_data": "__next_data__" in low,
        "script_json": len(re.findall(r'<script[^>]+type=["\']application/json["\']', low)),
        "forms": low.count("<form"),
        "links": low.count("<a "),
        "pagination_params": sorted({m for m in _PAGINATION_RE.findall(low)}),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="probe", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url")
    p.add_argument("--render", action="store_true", help="render JS via Playwright")
    p.add_argument("--max-bytes", type=int, default=4000, help="body preview length")
    p.add_argument("--timeout", type=float, default=20.0)
    args = p.parse_args(argv)

    report: dict = {"url": args.url}
    body = ""

    if args.render:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            sys.stderr.write("probe --render needs Playwright:  pip install playwright && playwright install chromium\n")
            return 2
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page()
                page.goto(args.url, timeout=int(args.timeout * 1000))
                body = page.content()
                browser.close()
            report.update(status=200, rendered=True, content_type="text/html (rendered)")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"probe: render failed: {e}\n")
            print(json.dumps({"url": args.url, "status": None, "error": str(e)}))
            return 1
    else:
        try:
            import httpx
        except ImportError:
            sys.stderr.write("probe needs httpx:  pip install httpx\n")
            return 2
        try:
            with httpx.Client(timeout=args.timeout, follow_redirects=True,
                              headers={"User-Agent": "zillusion-probe/0.2"}) as c:
                r = c.get(args.url)
            report["status"] = r.status_code
            report["final_url"] = str(r.url)
            report["content_type"] = r.headers.get("content-type", "")
            report["headers_safe"] = {k: v for k, v in r.headers.items()
                                      if k.lower() not in _SECRET_HDRS}
            body = r.text
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"probe: request failed: {e}\n")
            print(json.dumps({"url": args.url, "status": None, "error": str(e)}))
            return 1

    ct = report.get("content_type", "")
    stripped = body.lstrip()
    if "json" in ct or stripped[:1] in "{[":
        report["kind"] = "json"
        try:
            j = json.loads(body)
            report["json_shape"] = (list(j.keys())[:25] if isinstance(j, dict)
                                    else f"array[{len(j)}]")
        except Exception:  # noqa: BLE001
            report["json_shape"] = "parse_failed"
    else:
        report["kind"] = "html"
        report["signals"] = analyze_html(body)

    report["body_preview"] = body[:args.max_bytes]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
