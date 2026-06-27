---
name: api-probe
description: Patterns for probing an HTTP API (endpoints, auth, pagination, rate limits) during an api-workflow hypothesis loop — browserless, credential-safe.
when_to_use: inside an API workflow exploration run, between PICK_NEXT and UPDATE; whenever inputs/<site>/api_spec.json exists
---

# API probes (harness variant)

A probe is ONE bounded HTTP call that yes/no's a single hypothesis. Browser
tools are disabled in api sessions — the primitive is Bash + the venv
python + httpx (`.venv\Scripts\python.exe`, httpx is installed). One
request per probe; respect the API between probes (sleep if the docs or a
429 told you to).

## Two iron rules

1. **Responses land on disk, not in context.** Save every response body
   WHOLE to `workspaces/<site>/samples/` (no truncation), then `Read` the
   saved file selectively (head / a record / a grep). Never cat a body into
   the conversation.
2. **Credentials never leave python.** Load the key INSIDE the script from
   the credentials file; never interpolate it into a command line (command
   strings are recorded in run-logs), never print it (mask: `sk-…3f2`),
   never write it into workflow.py / helpers.py / api_manifest.yaml /
   exploration_log.md. `credentials_source` in the manifest is a POINTER
   ("env API_KEY, else credentials.json walk-up"), not a value.

## Credentials — where they live, how workflow.py finds them

The user supplies `inputs/<SITE_ID>/credentials.json`:
`{"api_key": "...", "extra": {...}}`. goal.md's Credentials section says
whether this run has one. The canonical lookup (put this in workflow.py —
the validator/runner copy credentials.json next to workflow.py in their
isolated dirs, and env wins for deployments):

```python
import json, os
from pathlib import Path

SITE_ID = "<your site_id>"

def _find_credentials() -> dict:
    if os.environ.get("API_KEY"):
        return {"api_key": os.environ["API_KEY"], "extra": {}}
    for parent in [Path(__file__).resolve().parent, *Path(__file__).resolve().parents]:
        for cand in (parent / "credentials.json", parent / "inputs" / SITE_ID / "credentials.json"):
            if cand.exists():
                return json.loads(cand.read_text(encoding="utf-8"))
    return {}
```

## Probe ladder (cheapest first)

1. **Docs**: `WebFetch` the documentation_url / quickstart from
   api_spec.json. Extract endpoint paths, auth header name, paging params.
2. **OpenAPI**: if `openapi_spec_url` exists, fetch it to `samples/`
   (httpx, save whole — specs can be MBs) and list `paths` keys + the
   security scheme with a small python snippet.
3. **Unauthenticated probe**: call the most promising endpoint WITHOUT
   auth. A 200 → the API is open (note it); 401/403 → auth confirmed
   required; 404 → wrong path hypothesis.
4. **Authenticated probe**: same call with the credential applied per the
   auth hypothesis (header vs query vs bearer). Still 401/403 with a
   correct-looking key → mark the hypothesis `blocked`,
   `wall_type="login"`, and `send_user_message` which key/plan is missing.
   Do NOT try to sign up for keys yourself.
5. **Shape probe**: where is the record list? Note the dot-path
   (`data.items`) for the manifest's `record_path`; APIs that return
   parallel arrays (e.g. open-meteo's `hourly.time[]` + values[]) need
   zipping into per-record dicts in workflow.py.
6. **Pagination boundary**: walk one page past the expected end. Empty
   list / explicit `next: null` / 4xx — record which, it becomes the
   workflow's stop condition (non-terminating pagination is the #1
   post-run regression).
7. **Rate-limit observation**: read `X-RateLimit-*` / `Retry-After`
   headers off a real response; on a 429, note the backoff that worked.
   Record findings in the manifest's `rate_limit` and pace workflow.py
   accordingly (sleep between calls; print a heartbeat line ≥ every 60s
   during long sleeps — the production runner kills 5-minute silences).

## Probe template

```python
# Bash: .venv\Scripts\python.exe probe_ep.py   (write the script, run it, Read the sample)
import json, httpx
from pathlib import Path

creds = _find_credentials()  # see snippet above
headers = {"X-Api-Key": creds.get("api_key", "")} if creds else {}
r = httpx.get("https://api.example.com/v1/posts?page=1&per_page=5",
              headers=headers, timeout=30)
out = Path("workspaces/<site>/samples/ep_posts_p1.json")
out.write_bytes(r.content)  # WHOLE body to disk
print({"status": r.status_code, "content_type": r.headers.get("content-type"),
       "bytes": len(r.content),
       "rate": {k: v for k, v in r.headers.items() if "ratelimit" in k.lower()}})
```

## When to commit a helper

You wrote the same request/unwrap shape twice (signed requests, cursor
paging, parallel-array zipping). Append it via `workspace_helper_append`.
One-shot probes belong in the exploration log, not helpers.

## When to promote to a skill

The probe used a *provider-agnostic* pattern (a class of auth dance, a
class of cursor pagination, a class of quota header). Propose via
`skill_propose`; the Recipe should be a starting point the next API adapts.
