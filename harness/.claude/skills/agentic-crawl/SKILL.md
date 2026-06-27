---
name: agentic-crawl
description: Patterns for the Agentic Crawl agent — harvest a site's data by driving the browser and writing batch-extraction code, committing as you go until complete against the crawl_brief's anchor.
when_to_use: running an agentic-route crawl (runtime.cli crawl); a site Explore marked too dynamic for a deterministic workflow.py
---

# Agentic Crawl (harness variant)

You ARE the crawl. There is no workflow.py — Explore decided this site's
control flow is too dynamic for static code, so you harvest the data by
driving the browser and writing extraction code, reacting to what each
page actually shows, until the data is complete against the
`crawl_brief.md` completeness anchor.

The economic rule that makes this affordable: **cost scales with the
number of DISTINCT page shapes you handle, not the record count.** Write
one extraction routine per shape and run it over a whole batch; never
read rows one-by-one with the LLM except for small high-value exceptions.

## Spine

1. `workspace_read "crawl_brief.md"` — goal + field schema, the
   completeness **anchor** (your done-judgment basis, marked hard|soft),
   why this is agentic, proven knowledge (helpers/selectors/endpoints/
   auth), hazards, recommended strategy.
2. `init_crawl(site, run, identifier_field=<brief>, anchor=<brief>)` —
   declare the dedup key + anchor. Resumes if the run already has records.
3. **Harvest loop** (below) — batch by batch, commit as you go.
4. `finalize_crawl(completeness_status, completeness_basis)` when you
   judge it done against the anchor → emit the outcome line.

## Harvest loop

```
get_crawl_progress            # where am I vs the anchor? cursor?
→ pick the next BATCH (a page of the enumeration, a category, a shape)
→ browser_player: write a routine that extracts the WHOLE batch
→ commit_records(batch)       # truth file; survives a kill
→ mark_cursor(position)       # resume point
→ page shape changed? rewrite the routine (that's the whole point)
→ a wall? browser_check_login_wall + browser_request_user_login
→ a unit you must give up on? record_skip(id, reason)
→ repeat until the anchor is reached
```

### Batch extraction (the main path)

`browser_player` runs async Python with `page`/`context`/`cdp` in scope.
Write a routine that loops over a batch IN the browser and returns
structured rows — then commit them. One model turn covers a whole page of
records.

```python
# browser_player script — extract a whole listing page, return rows
rows = []
for card in await page.query_selector_all(".result-card"):
    rows.append({
        "id":    await (await card.get_attribute("data-id")),
        "name":  (await (await card.query_selector(".title")).inner_text()).strip(),
        "price": (await (await card.query_selector(".price")).inner_text()).strip(),
        "url":   await (await card.query_selector("a")).get_attribute("href"),
    })
return rows                       # ← the tool result; then commit_records(rows)
```

Pure-HTTP enumeration (a list API behind the page) is fine too — `import
httpx` inside the `browser_player` script and page through it, returning
rows to commit. Use the cadence the brief's hazards section declares.

### Weak-schema units (documents / media / heterogeneous pages)

When units don't decompose into repeating same-shaped rows, YOU decide the
record organization — one record per DATA UNIT (one document, one media
item) is fine. Non-negotiable minimum per record: `source_url` (always)
plus a content carrier — `content` inline, or `file_ref` pointing at a
file you saved under the run dir (media bodies go on disk, never inline).
Carry the brief's `field_schema` into `init_crawl(data_definition=...)`
and refine it there when the real shape differs — that manifest block is
where downstream consumers read what each field MEANS.

### Resume / dedup

- `check_committed(ids)` before scraping a batch — skip ids already done
  (cheap resume, avoids spending fetches on records you have).
- `commit_records` is idempotent (deduped by `identifier_field`), so a
  retried batch never doubles. Re-running after a kill is safe.

## Judging done against the anchor

- **Hard anchor** (an enumerable index — a total count, a category tree,
  a date window): done when the cursor has covered the full set and every
  unit is `commit`ted or `record_skip`ped. `get_crawl_progress` gives the
  precise ratio. `finalize_crawl(completeness_status="pass", basis=
  "cursor reached 25104; 23k committed + 2.1k skipped-with-reason")`.
- **Soft anchor** (a stop heuristic — infinite scroll with no total,
  "until no new results"): done when the heuristic is met. You must
  JUSTIFY it: `basis="3 consecutive empty scroll batches; no pagination
  cursor exists on this site"`. Soft completeness is your judgment — own
  it honestly; if you stopped early, say `completeness_status="fail"`
  (the gate then yields PARTIAL with the data you kept).

## Walls (you hold the browser)

Unlike the Run agent, the live browser is YOURS. On a login/captcha wall:
`browser_check_login_wall` to confirm, then `browser_request_user_login`
to stream it to the watching user; after they clear it the session is
authenticated and you continue. `browser_save_auth` persists it for next
time. If the wall is unpassable and unmanned, `record_skip` the blocked
units with a reason and move on — don't spin.

## Progress-stall kill (keep moving)

The driver kills a crawl that stops advancing — no new commits or cursor
movement for a long stretch (a soft warning arrives first as an operator
message). This is the primary brake in place of a token cap. Keep
progress visible: commit small batches, `mark_cursor` often. If you're
genuinely stuck on a unit, `record_skip` it and advance, or
`finalize_crawl` with what you have — never loop in place hoping a wall
clears itself.

## Anti-patterns

- Holding scraped rows in memory across many batches before committing —
  a kill loses them. Commit each batch.
- One LLM read per record. That's the deterministic route's job done
  badly; here it's ruinously expensive. Write a routine, run the batch.
- Re-scraping from the top after a restart — `init_crawl` resumes; use
  `check_committed` / the cursor.
- Declaring done on a hard anchor without covering the cursor, or on a
  soft anchor without justifying the stop heuristic.
- Tomb-stoning transient failures you should retry — `record_skip` is for
  FINAL skips (unclearable wall, 404, off-goal), not a flaky timeout.
