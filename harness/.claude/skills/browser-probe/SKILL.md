---
name: browser-probe
description: Patterns for writing minimal CDP-first probes against the active page during a hypothesis loop.
when_to_use: inside an exploration run, between PICK_NEXT and UPDATE
---

# Browser probes (harness variant)

A probe is a minimal action that yes/no's a single hypothesis. Reach for
the *lowest-level* primitive that gives a clean expression - the previous
design conversation argued that helper functions are a ceiling, so we
prefer raw CDP + Python over thin Playwright sugar when the question
warrants it.

## Primitive selection

| Question | Use | Why |
| --- | --- | --- |
| "What does this URL look like?" | `browser_goto` + `browser_snapshot` | high-level is fine |
| "What does the DOM say about field X?" | `browser_evaluate` | one-liner JS |
| "Is there a hidden XHR/GraphQL endpoint?" | `browser_player` with `cdp.send('Network.enable')` | needs streaming |
| "Does Ctrl-click open a new tab?" | `browser_cdp_send('Input.dispatchMouseEvent', {modifiers:2,...})` | Playwright doesn't expose modifiers |
| "What does the framework state look like?" | `browser_player` | needs multi-step JS + Python decoding |

## Probe templates

### "Find the internal API behind the list"

```python
# browser_player script
import asyncio
seen = []
def on_response(event):
    url = event["response"]["url"]
    mt = event["response"].get("mimeType", "")
    if "/api/" in url or mt.startswith("application/json"):
        seen.append({"url": url, "status": event["response"]["status"]})
cdp.on("Network.responseReceived", on_response)
await cdp.send("Network.enable")
await page.goto("https://target.com/search?q=tokyo")
await asyncio.sleep(2)
result = seen[:20]
```

Snapshot afterward, then inspect `seen` for the lowest-noise endpoint.

### "Check that the GraphQL persisted query works on its own"

```python
# browser_player script
import json
resp = await page.request.post(
    "https://target.com/graphql",
    data=json.dumps({"query": "...", "variables": {...}}),
    headers={"Content-Type": "application/json"},
)
result = {"status": resp.status, "body": (await resp.text())[:2000]}
```

### "Right-click context menu reachable?"

```python
# browser_cdp_send script (or wrap in browser_player)
await cdp.send("Input.dispatchMouseEvent", {
    "type": "mousePressed",
    "button": "right",
    "x": 200, "y": 300,
    "buttons": 2,
    "clickCount": 1,
})
await cdp.send("Input.dispatchMouseEvent", {
    "type": "mouseReleased",
    "button": "right",
    "x": 200, "y": 300,
    "buttons": 0,
    "clickCount": 1,
})
```

Then `browser_snapshot` to see whether the context menu appears in the DOM.

### "What does pagination look like at the boundary?"

Walk to the last expected page, snapshot, then walk one past. Three cases:

- Empty result page -> termination condition is "no items"
- 404 / 5xx -> termination condition is HTTP status
- "No more results" string -> termination condition is a text match

Workflows that don't terminate cleanly are the #1 source of regressions
after the initial run. Verify this before VALIDATE_E2E.

## When to commit a helper

You wrote the same probe shape twice. Append it via
`workspace_helper_append`. Don't over-extract; one-shot probes belong in
the exploration log, not the helpers file.

## When to promote to a skill

The probe used a *site-agnostic* pattern (a class of cookie banner, a
class of anti-bot, a class of pagination). Propose it as a skill via
`skill_propose`. The Description should generalise; the Recipe should be
a starting point that the next site adapts.
