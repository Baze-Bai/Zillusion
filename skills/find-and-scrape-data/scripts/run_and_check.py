#!/usr/bin/env python3
"""run_and_check — execute a candidate scraper and sanity-check its output.

Distilled from the Zillusion harness validator. It:
  1. runs the scraper in a subprocess (the SAME python),
  2. loads the records it produced,
  3. checks record count and that required fields are present & non-empty,
  4. spot-checks a few sampled records' values against a fresh fetch of each
     record's source_url (substring match against the live page).
It prints findings; the calling agent decides PASS / FAIL. Mechanical checks
only — no LLM, no judgement.

Usage:
    python run_and_check.py <scraper.py> [--output output.json]
                            [--expect-fields a,b,c] [--sample N]
                            [--no-page-check] [--timeout S]

Output (stdout): a JSON report with a "checks" list of {check, ok, detail}.
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path


def load_records(path: str):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for k in ("records", "data", "results", "items", "rows"):
            if isinstance(data.get(k), list):
                return data[k]
        return [data]
    return data if isinstance(data, list) else []


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="run_and_check", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("scraper", help="path to the scraper / workflow.py to run")
    p.add_argument("--output", default="output.json", help="expected output file")
    p.add_argument("--expect-fields", default="", help="comma-separated required fields")
    p.add_argument("--sample", type=int, default=3, help="records to spot-check vs the page")
    p.add_argument("--no-page-check", action="store_true", help="skip live page comparison")
    p.add_argument("--timeout", type=float, default=300.0)
    args = p.parse_args(argv)

    report: dict = {"scraper": args.scraper, "checks": []}

    def check(name, ok, detail=""):
        report["checks"].append({"check": name, "ok": bool(ok), "detail": detail})

    # 1. run it
    try:
        proc = subprocess.run([sys.executable, args.scraper],
                              capture_output=True, text=True, timeout=args.timeout)
        report["ran"] = True
        report["exit_code"] = proc.returncode
        report["stderr_tail"] = proc.stderr[-1000:]
        check("ran_clean", proc.returncode == 0, f"exit={proc.returncode}")
    except Exception as e:  # noqa: BLE001
        report["ran"] = False
        check("ran_clean", False, str(e))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    # 2. load output
    try:
        records = load_records(args.output)
    except Exception as e:  # noqa: BLE001
        check("output_loadable", False, f"{args.output}: {e}")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    check("output_loadable", True, args.output)
    report["record_count"] = len(records)
    check("non_trivial", len(records) > 0, f"{len(records)} records")

    # 3. required fields present & non-empty across all records
    fields = [f.strip() for f in args.expect_fields.split(",") if f.strip()]
    for f in fields:
        if not records:
            check(f"field:{f}", False, "no records")
            continue
        present = sum(1 for r in records
                      if isinstance(r, dict) and r.get(f) not in (None, "", [], {}))
        check(f"field:{f}", present == len(records), f"{present}/{len(records)} non-empty")

    # 4. spot-check sampled values against the live page
    if not args.no_page_check and records:
        try:
            import httpx
        except ImportError:
            check("page_match", False, "httpx not installed (use --no-page-check)")
        else:
            sample = random.sample(records, min(args.sample, len(records)))
            with httpx.Client(timeout=20, follow_redirects=True,
                              headers={"User-Agent": "zillusion-check/0.2"}) as c:
                for rec in sample:
                    if not isinstance(rec, dict):
                        continue
                    url = rec.get("source_url") or rec.get("url")
                    if not url:
                        check("page_match", False, "record has no source_url/url")
                        continue
                    try:
                        page = c.get(url).text
                    except Exception as e:  # noqa: BLE001
                        check("page_match", False, f"fetch {url[:60]}: {e}")
                        continue
                    vals = [str(v) for v in rec.values()
                            if isinstance(v, (str, int, float)) and len(str(v)) >= 4][:6]
                    hit = sum(1 for v in vals if v in page)
                    check("page_match", hit > 0,
                          f"{hit}/{len(vals)} sampled values on {url[:60]}")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
