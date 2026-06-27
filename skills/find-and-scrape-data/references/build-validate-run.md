# Build → Validate → Run (deterministic route)

This is the BUILD-WRITE-and-prove phase of the `find-and-scrape-data` skill. The
upstream explore phase (see `explore-and-route.md`) has handed you a confirmed
ROUTE and a `goal.md`. This file covers only the **deterministic** route: you
write a `workflow.py`, you validate it at sample scale, and — if it passes — you
run it at full scale. Inline / agentic / infeasible routes terminate elsewhere;
do not read this for them.

In the original Zillusion harness, these steps were mediated by MCP tools
(`selectors_write`, `api_manifest_write`, `runtime.cli run`, a separate
validator agent) and a `PreToolUse` hook that made some files append-only. **You
have none of that.** Everything below is done with `Write`/`Edit` to plain
files, `Bash` to run python, and `Read`/`Grep` to inspect. The manifests are
just YAML files you author by hand. Honesty about this degradation is baked into
each section.

Per the skill's doctrine: every selector, endpoint, and field shape you write
into a manifest is a **hypothesis carried from explore**, not a proven fact. The
validate step is where hypotheses get ground-truthed against real output.

## Workspace layout (you create these dirs)

```
<workspace>/<site_id>/
  goal.md                 # handoff contract from explore (read-only to you)
  workflow.py             # YOU write — one of the three skeletons below
  selectors.yaml | download_manifest.yaml | api_manifest.yaml   # exactly ONE
  output_sample.json      # workflow.py writes this in CRAWL_MODE=sample
  samples/                # raw response bodies saved during probing (api)
  credentials.json        # only if api + user supplied a key (gitignored)
  runs/<run_id>/          # full-mode output lands here, never overwrites sample
```

Pick the manifest matching the type explore confirmed. They are **mutually
exclusive per run** — produce exactly one. If you ever have more than one on
disk, treat precedence as `api > download > extraction` (the original
validator's detection order).

## workflow.py — the contract baked into every skeleton

Whatever the type, `workflow.py` is a plain script you run with
`python workflow.py`. Four cross-cutting rules apply to ALL three types,
because a production runner will eventually run this same file unattended:

| Rule | Why |
| --- | --- |
| Read `CRAWL_MODE` env (`sample` default, `full` for production) and bound work by it | One code path proves at sample scale and runs at full scale — never a second script |
| Honor `WORKFLOW_CDP_PORT` env if you launch a browser | A production runner can stream a mid-crawl login/captcha wall to a watching human in the SAME browser |
| `print()` a heartbeat at least every ~60s during any long sleep | A production runner kills a process after ~5 min of stdout silence — a rate-limit sleep must keep talking |
| Flush output atomically every ≤50 records or ≤60s | A mid-run kill (esp. on Windows — no signal grace) must never corrupt or lose everything; partial-on-disk salvages to PARTIAL instead of ABORTED |

The flush helper (write once near the top of every workflow.py):

```python
import os, json

CRAWL_MODE = os.environ.get("CRAWL_MODE", "sample")
OUT_PATH   = "output_sample.json"   # full-mode runner redirects this to runs/<id>/output.json

def flush(records, path=OUT_PATH):
    """Atomic rewrite — a kill mid-write never corrupts the file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)   # rename is atomic on the same filesystem
```

The browser-attach shape, when (and only when) the type needs a browser:

```python
_cdp = os.environ.get("WORKFLOW_CDP_PORT")
browser = await pw.chromium.launch(
    headless=os.environ.get("HEADLESS", "1") != "0",
    args=[f"--remote-debugging-port={_cdp}"] if _cdp else [],
)
```

**Output contract (all types):** the top-level JSON in `output_sample.json` is a
LIST of record dicts. Per record, the non-negotiable weak-schema minimum is
`source_url` (where this unit came from) plus a content carrier — `content`
(inline text) OR `file_ref` (a workspace-relative path to a file the workflow
saved, for PDFs / media / oversized text). Real tabular fields go on top of that
minimum. One record total is a legal dataset.

> Degradation note: the original harness enforced append-only on some artifacts
> via a `PreToolUse` hook. You have no such enforcement — just don't rewrite
> history you want to keep. `workflow.py` itself is a deployable product and is
> freely mutable (you fix it across validate iterations); it is NOT append-only.

## Type 1 — extraction (embedded page data)

Records live in the page's HTML. Declare the selectors you confirmed in explore
into `selectors.yaml` (authored by hand with `Write`), then `workflow.py`
re-implements that same extraction.

`selectors.yaml`:

```yaml
selectors:
  observed_at: 2026-06-13T00:00:00Z
  source_url: https://example.com/list?page=1
  records_observed: 25
  record_locator: 'div.card'           # CSS for ONE record container
  fields:
    title:
      selector: 'h2.title'
      extraction: 'textContent.trim()'
      stability: STRICT                 # STRICT | TOLERANT | SKIP
      workflow_field: null              # set only if workflow.py names it differently
      semantic: 'listing title, NOT the category label'
    score:
      selector: 'span.votes'
      extraction: 'parseInt(textContent)'
      stability: TOLERANT               # numeric, real-world drift ±5% ok
      semantic: 'upvote score, NOT comment count'
  # The full JS the validator (re)runs to ground-truth records. Must return
  # a list[dict] with the SAME field names workflow.py emits.
  extract_js: |
    () => Array.from(document.querySelectorAll('div.card')).map(u => ({
      source_url: location.href,
      title: u.querySelector('h2.title')?.textContent.trim() ?? null,
      score: parseInt(u.querySelector('span.votes')?.textContent ?? '') || null,
    }))
```

`stability` legend (carried from explore's resample observations):
`STRICT` = identifier / stable text, any change is a bug · `TOLERANT` = numeric
with real drift, ±5% ok · `SKIP` = time-relative / CDN-cached / detail-only.

> If `workflow.py` parses an **embedded JSON blob** instead of iterating discrete
> DOM nodes (the common httpx-first case), carry the explore discipline into the
> build: slice records by a **stable per-record id**, not a positionally-unstable
> field, and assert each record's id matches the id in its own detail URL before
> keeping it (see "anchor on a STABLE id" in `explore-and-route.md`). A silent
> off-by-one that looked fine in a 2-record sample mis-pairs every record at full scale.

workflow.py skeleton (httpx-first; fall back to playwright only if the records
need JS to render):

```python
import os, json, time, re
# ... CRAWL_MODE + flush() from above ...
MAX_PAGES = None if CRAWL_MODE == "full" else 2

def _to_int(node):                                 # parse digits, None on failure/missing
    if node is None:
        return None
    m = re.search(r"-?\d+", node.text())
    return int(m.group()) if m else None

def parse_page_httpx(html, page_url):
    from selectolax.parser import HTMLParser        # or lxml / bs4 — whatever's importable
    tree = HTMLParser(html)
    out = []
    for card in tree.css("div.card"):
        title_node = card.css_first("h2.title")
        out.append({
            "source_url": page_url,
            "title": title_node.text(strip=True) if title_node else None,
            "score": _to_int(card.css_first("span.votes")),
        })
    return out

def main():
    import httpx
    records, page = [], 1
    seen, last_t = 0, time.time()                  # flush bookkeeping
    with httpx.Client(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as c:
        while MAX_PAGES is None or page <= MAX_PAGES:
            r = c.get(f"https://example.com/list?page={page}")
            rows = parse_page_httpx(r.text, str(r.url))
            if not rows:
                break                              # stop condition = empty page
            records += rows
            # flush every >=50 NEW records OR >=60s — not on an exact modulo
            if len(records) - seen >= 50 or time.time() - last_t >= 60:
                flush(records); seen, last_t = len(records), time.time()
            print(f"[heartbeat] page {page}, {len(records)} records")
            time.sleep(0.5)                    # be polite; on a 429/503 reuse the api skeleton's Retry-After backoff
            page += 1
    flush(records)

if __name__ == "__main__":
    main()
```

If httpx returns a shell with no records (client-rendered), switch the page
fetch to playwright (`page.goto(...)` then `page.evaluate(extract_js)`), reusing
the `_cdp` launch shape above. **playwright may not be installed** — probe with
`python -c "import playwright"` first; if it fails, either
`python -m pip install playwright && python -m playwright install chromium` (if
the environment permits) or fall back to httpx + a heavier HTML parse, and note
the limitation in `goal.md`-adjacent reasoning. extraction ↔ download switches
are free if the "page" turns out to be a CSV link.

## Type 2 — download (a downloadable file)

The data IS a file the site already offers (CSV / JSON / XLSX / parquet / …).
Declare what you fetch in `download_manifest.yaml`:

```yaml
files:
  - url: https://example.com/data/dump.csv
    filename: dump.csv
    format: csv
    min_bytes: 10240        # sanity floor — a 200-byte "file" is an error page
```

workflow.py streams each file, checks size, confirms it parses, and emits one
record per file (weak schema: `source_url` + `file_ref`):

```python
import os, json, httpx, csv

def _assert_parses(dest, fmt):
    """Open with the right reader and touch >=1 row/object so a renamed HTML
    error page (a 200 that's actually a login wall) fails loudly, not silently."""
    if fmt == "csv":
        with open(dest, newline="", encoding="utf-8", errors="replace") as f:
            next(csv.reader(f))                       # first row must exist
    elif fmt == "json":
        with open(dest, encoding="utf-8") as f:
            json.load(f)
    elif fmt in ("xlsx", "xls"):
        import openpyxl
        openpyxl.load_workbook(dest, read_only=True)
    # parquet / xml / zip: add the matching reader; else the size check stands

def main():
    files = [{"url": "https://example.com/data/dump.csv",
              "filename": "dump.csv", "format": "csv", "min_bytes": 10240}]
    os.makedirs("downloads", exist_ok=True)
    records = []
    with httpx.Client(timeout=120, follow_redirects=True) as c:
        for spec in files:
            dest = os.path.join("downloads", spec["filename"])
            with c.stream("GET", spec["url"]) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
            size = os.path.getsize(dest)
            assert size >= spec["min_bytes"], f"{dest} only {size}B < {spec['min_bytes']}"
            _assert_parses(dest, spec["format"])     # csv.reader / json.load / openpyxl
            records.append({"source_url": spec["url"], "file_ref": dest,
                            "format": spec["format"], "bytes": size})
            print(f"[heartbeat] downloaded {dest} ({size}B)")
    flush(records)
```

`_assert_parses` opens the file with the right reader and reads at least one
row/object so a renamed HTML error page fails loudly rather than silently.

## Type 3 — api (an HTTP API)

Browserless end-to-end: httpx only, no playwright. Declare the endpoints you
confirmed in explore into `api_manifest.yaml`. **`credentials_source` is a
POINTER string, never a literal key.**

```yaml
endpoints:
  - probe_url: https://api.example.com/v1/posts?page=1&per_page=5   # concrete, callable NOW
    record_path: data.items          # dot-path to the record list in the JSON
    auth: header:X-Api-Key           # header:<name> | query:<name> | bearer
    pagination:
      style: page                    # page | cursor | offset
      param: page
      stop: empty_list               # empty_list | next_null | http_4xx
    rate_limit:
      min_interval_s: 0.5            # pace between calls
      respect_header: Retry-After
identifier_field: id
credentials_source: "env API_KEY, else credentials.json walk-up"
```

workflow.py loads creds via `_find_credentials` (see Credential safety below),
walks pages to the recorded stop condition, paces per `rate_limit`, and unwraps
`record_path`:

```python
import os, json, time, httpx
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

# These literals mirror api_manifest.yaml — keep them in sync with what explore
# confirmed (record_path, auth header, pagination param/stop, rate_limit).
RECORD_PATH  = "data.items"
MIN_INTERVAL = 0.5

def _dig(obj, dot_path):           # "data.items" -> obj["data"]["items"]
    for k in dot_path.split("."):
        obj = obj[k]
    return obj

def _retry_after_seconds(value):   # Retry-After is int-seconds OR an HTTP-date
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            return None

def main():
    creds = _find_credentials()
    headers = {"X-Api-Key": creds.get("api_key", "")} if creds.get("api_key") else {}
    records, page = [], 1
    with httpx.Client(timeout=30, headers=headers) as c:
        while True:
            r = c.get(f"https://api.example.com/v1/posts?page={page}&per_page=50")
            if r.status_code in (429, 503):              # back off BEFORE raising
                wait = _retry_after_seconds(r.headers.get("Retry-After")) or 5.0
                print(f"[heartbeat] {r.status_code}, backing off {wait:.0f}s")
                time.sleep(wait); continue               # retry the SAME page
            if r.status_code in (401, 403):              # auth wall — plain msg, not a bracket token
                raise SystemExit("AUTH WALL: missing/invalid key — tell the user "
                                 "which key/plan is needed; no records emitted")
            r.raise_for_status()
            rows = _dig(r.json(), RECORD_PATH)
            if not rows:
                break                                    # stop = empty_list
            records += rows
            flush(records)
            print(f"[heartbeat] page {page}, {len(records)} records")
            time.sleep(MIN_INTERVAL)                      # pace between calls
            page += 1
    flush(records)
```

**Cursor / offset pagination** (manifest `style: cursor|offset`) — same loop, different stepping:

```python
cursor = None
while True:
    params = {"cursor": cursor} if cursor else {}        # offset variant: {"offset": off}
    r = c.get(URL, params=params); r.raise_for_status()
    rows = _dig(r.json(), RECORD_PATH)
    if not rows: break                                   # stop = empty_list
    records += rows; flush(records)
    cursor = _dig(r.json(), "meta.next_cursor")          # offset variant: off += len(rows)
    if not cursor: break                                 # stop = next_null
```

Read the stop shape from the manifest: `empty_list` → `if not rows`, `next_null` → `if not cursor`, `http_4xx` → caught by `raise_for_status`.

APIs returning parallel arrays (e.g. `hourly.time[]` + `hourly.values[]`) need
zipping into per-record dicts. A persistent 401/403 with a correct-looking key
is a **wall**: stop, write a clear note that key/plan X is missing, and emit
INCONCLUSIVE — never try to register for keys yourself. `api → download` is a
fine pivot (the API hands you a bulk file URL); `api → extraction` is
discouraged — not because a browser is unavailable (it IS, in vanilla Claude
Code) but because an "API" that's really page-embedded data means you
mis-identified the source upstream; re-discover it as extraction instead (see
the pivot rules in `explore-and-route.md`).

## VALIDATE — the method (you perform this; the skill ships no validator)

In the original harness a separate SDK validator agent ran this. Here YOU do it,
optionally writing a throwaway python check at runtime — but the skill bundles no
script. The logic below mirrors the original `check_output.py`; its exit code was
always **informational only** — the verdict comes from your reasoning, not an
exit status.

Run these checks in order:

1. **Sample run.** `CRAWL_MODE=sample python workflow.py`. On Windows PowerShell:
   `$env:CRAWL_MODE="sample"; python workflow.py`. This must write
   `output_sample.json`.
2. **Parses.** `Read` the file or `json.loads` it. If it doesn't parse → **hard
   FAIL**.
3. **Is a list.** Top-level must be a JSON array. **Auto-unwrap** a dict wrapper
   keyed `rows` / `records` / `data` / `items` / `results` (first present wins)
   before this check — `goal.md` may legitimately pick either shape. If after
   unwrapping it's still not a list → **hard FAIL**.
4. **Record count.** `len(records) >= 3` is the expectation. Fewer is a **WARN**,
   not an automatic fail — the page might genuinely hold <3 records.
5. **Field coverage.** Pull the required fields from `goal.md`: scan ONLY bullet
   lines (`-`/`*`) under a heading matching `Required fields` / `Output fields` /
   `Output schema` (case-insensitive); collect `` `field` `` and `**field**`
   tokens; drop stopwords (`null`, `true`, `int`, `str`, `list`, `dict`, …). The
   section gate matters — bullets under Scope/Constraints/Background mention tool
   names in backticks that are NOT fields. For each required field, confirm it's
   **present and mostly non-null** across records. A missing required field →
   **hard FAIL**. If `goal.md` has no such section → WARN, coverage unchecked.
6. **Format parses** (type-specific): download — the saved file opens with its
   reader; api — `json.loads` succeeds on the response. Won't parse → **hard
   FAIL**.
7. **Reproducibility.** Run the sample crawl TWICE; diff record counts and key
   sets. Gross instability (wildly different counts, keys appearing/vanishing) is
   a smell — surface it. Minor numeric drift on TOLERANT fields is expected, not
   a fail.
8. **Secrets scan.** `Grep` the produced `workflow.py`, the manifest, and
   `output_sample.json` for key shapes: `sk-[A-Za-z0-9]{16,}`,
   `AKIA[0-9A-Z]{16}`, long hex/base64 runs, and the literal prefix of any key
   you actually loaded. Any literal key leaked into a shipped artifact → **hard
   FAIL** (this is `secrets_safe`).
9. **Endpoint match (api only).** Re-call each manifest `probe_url` live; confirm
   it still returns the recorded shape at `record_path`. A dead/changed endpoint
   → FAIL.

**Emit a verdict as the LAST line of your validate output**, regex-parseable —
it must match `\[(PASS|FAIL|INCONCLUSIVE)\]` followed by a short reason:

```
[PASS] 25 records, all 4 goal fields present & non-null, reproduced, no secrets
[FAIL] missing required field 'published_at' in 24/25 records
[INCONCLUSIVE] 0 records but the listing page may simply be empty right now
```

Gate logic — mechanical and honest about what's checkable:

- Any **hard gating check FAILs** (doesn't parse / not a list / missing required
  field / format won't parse / secret leaked / endpoint dead) → `[FAIL] <reason>`.
- All gating checks are **PASS or N/A** → `[PASS] <reason>`.
- Genuinely **undecidable** → `[INCONCLUSIVE] <reason>`: e.g. zero records but the
  page might just be empty, or you can't tell whether the dataset is complete.

> **Completeness is NOT hard-verifiable in one shot.** Whether you captured ALL
> records is determined by the workflow you wrote — there's no independent ground
> truth in a single sample run, so you cannot hard-FAIL on a "feels incomplete"
> hunch. Mark such doubts `[INCONCLUSIVE]` and record a follow-up hypothesis
> ("pagination may stop early; recheck at full scale"). FAIL only against an
> INDEPENDENT basis (the JSON doesn't parse, a named required field is absent,
> a key leaked). Standalone, you also lack the original's resample-derived
> STRICT/TOLERANT/SKIP precision and its 23k-API semantic cross-check — so lean
> toward INCONCLUSIVE over FAIL when the basis is your own implementation.

If FAIL, fix `workflow.py` / the manifest and re-validate. Do not proceed to RUN
on anything but `[PASS]`.

## RUN — production (full scale)

Only after `[PASS]`. This reuses the **exact same `workflow.py`** validated at
sample scope — never a second code path. The only change is the env:

```bash
mkdir -p <workspace>/<site_id>/runs/<run_id>
CRAWL_MODE=full OUT_PATH=<...>/runs/<run_id>/output.json \
  python <workspace>/<site_id>/workflow.py
```

(PowerShell: `$env:CRAWL_MODE="full"; $env:OUT_PATH="...\runs\<run_id>\output.json"; python ...`)

`CRAWL_MODE=full` lifts the sample caps to `goal.md`'s complete scope (all pages
until no `next`, a date window, a category sweep — whatever you defined). The
result is KEPT under `runs/<run_id>/` and **never overwrites `output_sample.json`**.
Write a `runs/<run_id>/manifest.yaml` recording the run.

The full run reuses the same heartbeat + atomic-flush guarantees, so a mid-run
kill salvages partial data. The **outcome is gate-computed and measures
COMPLETION, not quality** — assign one of:

| Outcome | When |
| --- | --- |
| `COMPLETE` | Ran to the defined stop condition; output present and parses |
| `PARTIAL`  | Killed/errored mid-run but flushed records survive on disk |
| `FAILED`   | Ran but produced no usable output (empty / unparseable) |
| `ABORTED`  | Killed before any output was flushed — nothing kept |

A non-COMPLETE outcome is feedback for a NEXT explore iteration, not a silent
failure — note what stopped it (wall-clock cap, a 429 storm, an early-stopping
pagination bug) in the manifest so the next pass has a concrete lead.

**Resuming a PARTIAL run.** Incremental flush leaves real records on disk, so a
killed run can resume instead of re-fetching or double-counting: on restart, load
the existing `runs/<run_id>/output.json` if present, build a `seen` set keyed on
`identifier_field` (api) or the canonicalized `source_url` (extraction), skip
already-captured records, and continue. The same stable id that anchors extraction
and dedups tiles is your resume key.

> Degradation note: the original had a wall-clock + stall killswitch and a sandbox
> enforcing it. Standalone you are the runner — make it concrete: cap the full run
> at a self-chosen wall-clock budget (default ~10–15 min interactive) AND a
> **stall guard** (if N consecutive pages/tiles add 0 *new* records, stop and mark
> `PARTIAL`). Print the chosen cap at start so the user can veto, and stop yourself
> rather than spinning forever on a stuck crawl.

## Credential safety (api type) — recap

Detailed in `explore-and-route.md`; the load-side rules that matter at BUILD/RUN
time:

- `credentials_source` in `api_manifest.yaml` is a POINTER
  (`"env API_KEY, else credentials.json walk-up"`), never the value.
- workflow.py loads the key INSIDE the script, env-first then a walk-up for
  `credentials.json`; never interpolate a key into a command line (logged),
  never `print()` it unmasked (`sk-…3f2`), never write it into `workflow.py` /
  the manifest / any sample / any log.

This is the SAME canonical finder as `explore-and-route.md` — paste it
identically into both your probes and the eventual `workflow.py` (workflow.py
sits next to `credentials.json` in the workspace, so the walk-up finds it):

```python
import json, os
from pathlib import Path

def _find_credentials() -> dict:
    if os.environ.get("API_KEY"):                       # env wins (for deployments)
        return {"api_key": os.environ["API_KEY"], "extra": {}}
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:         # walk up looking for the file
        cand = parent / "credentials.json"
        if cand.exists():
            return json.loads(cand.read_text(encoding="utf-8"))
    return {}
```

The secrets-scan check in VALIDATE step 8 is your backstop: it hard-FAILs if a
literal key ever leaked into a shipped artifact, so a careless `print(api_key)`
or a hard-coded fallback gets caught before RUN.
