---
description: Run the hypothesis-driven browser exploration loop (harness variant) for one site
argument-hint: <site_id>
---

You are starting an exploration for site `$ARGUMENTS`.

Follow this sequence:

1. Read `inputs/$ARGUMENTS/seed.json` and `inputs/$ARGUMENTS/goal.md`.
   The seed is a *hypothesis*, not a fact.
2. Call `skill_list` and `memory_index`. For any skill whose `when_to_use`
   looks like it might apply, call `skill_read` to fetch the full record.

   **API variant** — if `inputs/$ARGUMENTS/api_spec.json` exists, this is an
   **API workflow** run (browser tools are disabled in this session; the
   `hypothesis-loop` skill's api type + pivot rules apply). Deviations from
   the numbered steps:
   - Step 1: read `inputs/$ARGUMENTS/api_spec.json` (+ `goal.md`) instead of
     seed.json — same rule: it is a *hypothesis*, not a fact.
   - Step 3: call `workspace_attach(site_id="$ARGUMENTS")` instead of
     `browser_attach`.
   - Step 6: probe over HTTP per the `api-probe` skill (Bash + the venv
     python + httpx). Save every response WHOLE under `samples/`, then Read
     selectively. NEVER print, hardcode, or write a credential value into
     any artifact, log, or command string — load keys inside python from the
     credentials file; a 401/403 wall → hypothesis `blocked`,
     `wall_type="login"`, tell the user which key is missing.
   - Step 7's auth_state walk-up does not apply; use the
     `_find_credentials()` snippet from the `api-probe` skill instead.
   - Step 8: write `api_manifest.yaml` via `api_manifest_write` (NOT
     selectors.yaml) — endpoints with a concrete `probe_url`, declared
     `fields[]` with stability classes (re-call the endpoint 2–3 times),
     `identifier_field`, `pagination`, `rate_limit`, `credentials_source`
     (a pointer, never a value).
   - Steps 4, 5, 9–12 unchanged — in step 9 reconcile `task_plan.md`
     against `api_manifest.yaml`'s `fields[]` instead of selectors.yaml.
3. Call `browser_attach(site_id="$ARGUMENTS")`.
4. **Write the run's task plan** to `workspaces/$ARGUMENTS/task_plan.md`
   via `workspace_write(path="task_plan.md", content=...)` — a descriptive
   summary of what THIS run intends to do, synthesised from the seed +
   goal + any applicable skills. **Write the prose in the SAME language as
   the user's request in `goal.md`** (e.g. a Chinese goal → a Chinese
   task_plan); keep technical tokens — field names, URLs, selectors, code,
   the ```fields``` block — in their canonical form. (This applies ONLY to
   `task_plan.md`, which the user reads. Your other working artifacts —
   `exploration_log.md`, `hypotheses.yaml`, `iter_summary.md`, probes — can
   be in whatever language you find natural.) This is your own
   **freely-mutable living doc** (NOT append-only): `workspace_read` it to stay on target and
   `workspace_write` it again to revise as the plan changes. If a prior
   iter already left one (the SessionStart hook surfaces it), revise that
   rather than overwrite blindly. Cover:
   - **Data source**: seed URL; site kind (SPA / static / API-backed — a
     guess at this stage); one-line summary of the seed's `page_tree`.
   - **User requirement**: restate in your own words what `goal.md` asks
     for — this is the acceptance bar.
   - **Target data & fields**: per page/node, which records and which
     fields you intend to extract (start from seed `fields_available` +
     goal Required fields; every field is unverified until probed).
     Present them as a **markdown table, one row per field, with two
     columns: the canonical field name and a 1–2 sentence description**.
     Every field MUST get a real 1–2 sentence note in the description
     column — what the field is, its unit, and where on the page it comes
     from — never a bare one-word gloss. Write the description prose (and
     the column headers) in the user's goal language per the rule above;
     keep the field name itself canonical. Example (English-goal site):

     | Field | Description |
     |---|---|
     | `post_title` | The post's headline, taken from the visible text of the card's main link. The primary human-readable identifier for each record. |
     | `post_url` | The canonical permalink to the post, from the card link's href. Used to dedupe records and to re-fetch the detail page. |
     | `vote_count` | Net upvotes as an integer, read from the card's vote control. Can be negative on heavily-downvoted posts. |

     **After the table, also emit a machine-readable field list** — a
     fenced code block tagged `fields`, one canonical field name per line
     (the names goal.md / your output schema use), listing exactly the
     same fields as the table's rows. The SessionStart hook diffs this
     block against the confirmed `selectors.fields` keys and flags any
     drift to the next iter, so keep the table AND this block in sync when
     you reconcile (step 9):
     ```fields
     post_title
     post_url
     vote_count
     ```
   - **Intended crawl method**: navigation (browser / CDP / API),
     pagination style, anti-bot / media handling, concurrency — an INTENT
     at run start that WILL change as probes reveal the real mechanism.
   - **Success criteria**: target record count, completeness bar, what
     "done" looks like.
   - **Open questions / risks**: the seed assumptions you doubt most.
5. Build `workspaces/$ARGUMENTS/hypotheses.yaml` from the seed and the plan
   (all entries start `unverified`). Append an `[INIT]` section to the log
   via `workspace_append_log`.
6. Drive the loop in `.claude/skills/hypothesis-loop/SKILL.md`:
   - Probe with `browser_player`, `browser_cdp_send`, or `browser_goto +
     browser_evaluate` as appropriate.
   - Always `browser_snapshot` before flipping to `confirmed`.
   - **If a wall blocks the goal data** — page empty/blocked, redirect to login,
     a login **modal/blur** over content, or a **captcha / "verify you're human"
     / Cloudflare** challenge — call `browser_check_login_wall` (returns
     `wall_type`); if `is_access_wall`, `browser_request_user_login(url,
     reason=...)`, STOP and ask the user to log in **or solve the verification**
     in the window, then `browser_save_auth` and re-fetch. See "Access walls" in
     the skill. (Autonomous runs can't — mark the hypothesis `blocked` with the
     `wall_type` and continue with public data.)
   - **NEVER DIY a login.** `browser_request_user_login` is the ONLY supported
     way past an auth wall. Do NOT write login scripts, call the site's login /
     QR APIs (e.g. `getQrCode`), decode QR images, or launch your own headed
     browser — they bypass the streamed canvas, trip anti-bot, and the user sees
     nothing. If the tool returns `{ok: false, reason}`, relay the reason and
     retry it; if a PRIOR iteration marked login `blocked` / `no X server`, do
     NOT treat it as permanent — the streamed takeover works, call the tool
     again instead of shipping public-only data.
   - If you find a transferable technique, propose it as a skill via
     `skill_propose` (only after it worked here at least once).
   - Call `skill_record_use` when you apply an existing skill.
   - When a probe materially changes the plan, `workspace_write` an
     updated `task_plan.md` so it stays current.
7. When no high-priority unverified hypotheses remain, **declare your terminal
   route** (`hypothesis-loop` ROUTE SELECTION — the four routes are peers, not a
   default plus exceptions) and produce that route's artifact. **Steps 7–9 are
   the `deterministic` route's finish**; the other routes finish in one call,
   then skip to step 10:
   - **agentic** → `write_crawl_brief(...)` with the mandatory completeness
     anchor + `field_schema` (the Agentic Crawl agent then harvests it — its
     steps are the `agentic-crawl` skill).
   - **inline** → `commit_inline_dataset(records)` with the WHOLE set you scraped
     this session.
   - **infeasible** → `report_off_goal(...)`.

   For **deterministic**: write
   `workspaces/$ARGUMENTS/workflow.py`, run it with Bash, validate the
   output against `goal.md`. **If the user logged in via
   `browser_request_user_login` (takeover), you MUST rebuild workflow.py to USE
   that session and extract the FULL post-login dataset — never ship the
   public-only subset** (placeholder prices, the first page, a tiny fraction of
   the claimed total). Re-probe the authenticated paths (gated fields, the
   post-login pagination/API) during explore and encode them in workflow.py. The
   saved cookies are staged as an `auth_state.json` on the script's walk-up path
   — it may be a PER-USER store, NOT the project root, so always resolve by
   walking up from `__file__`; never hardcode a path. Pass it as `storage_state`
   (browser) or send its cookies (API) so the standalone scraper AND its
   validator rerun authenticate too:
   ```python
   def _find_auth_state():
       from pathlib import Path
       for parent in Path(__file__).resolve().parents:
           cand = parent / "auth_state.json"
           if cand.exists():
               return str(cand)
       return None
   ```

   **Alongside workflow.py, write `workspaces/$ARGUMENTS/workflow_purpose.md`**
   via `workspace_write` — 3–8 sentences, in the same language as `goal.md`
   (same rule as task_plan), covering: what this workflow does (which site,
   which records), why this approach (browser / API / download — the mechanism
   your probes confirmed), and what its output is for. Revise it whenever
   workflow.py's approach changes. It is bundled verbatim into the archived
   version's `WORKFLOW_DOC.md` (`workflow_versions/`), so write it for a
   reader who has not seen this conversation.
8. **Selectors + field stability pass** (`deterministic` route; before [DONE]): see
   "Selector tracking" + "Field stability tracking" sections in
   `hypothesis-loop/SKILL.md`. Use the `selectors_write` MCP tool to
   persist BOTH `selectors:` and `field_stability:` blocks to
   `workspaces/$ARGUMENTS/selectors.yaml` (NOT hypotheses.yaml — that
   sidecar was migrated 2026-05-22). This is the CANONICAL source
   the validator reads — skipping leaves it unable to run ground-truth
   re-fetch (the resample_match / field_semantics dimensions).

9. **Check `task_plan.md` against reality, fix only mismatches** (before
   [DONE]): the "Target data & fields" you wrote in step 4 was an
   *intent*, recorded before probing. Compare it to `selectors.yaml`, the
   now-confirmed field set — does task_plan's Target data & fields match
   the `selectors.fields` keys? **If they already agree, leave task_plan
   unchanged.** Only for the parts that DON'T match, `workspace_write` a
   correction: drop fields that proved unextractable, add fields you
   discovered while probing, fix any field whose real meaning differed
   from the intent (use each selector's `semantic`) — updating BOTH the
   annotated table's rows (rewrite a field's 1–2 sentence description when
   its real meaning differs) AND the `fields` block so the two stay
   identical. Apply the same check
   to "Intended crawl method" and "Success criteria", correcting only
   what the run actually contradicted. This is a consistency check, not a
   mandatory rewrite — the carried-forward plan just must not misdescribe
   what the crawler does.

10. **Write iter_summary section** (before [DONE], REQUIRED in
   orchestrator-driven loops): call `iter_summary_append` MCP tool with
   the structured fields (`tried`, `worked`, `do_not_retry`,
   `open_hypotheses`, `next_strategy`). Be SPECIFIC in `do_not_retry`
   — name actual patterns that failed, not generalities. The next iter's
   SessionStart hook injects this into the actor's context as their
   primary cross-iter memory.

11. Append `[DONE]` to `exploration_log.md` and summarise briefly.

12. Before exiting, ask yourself: did I learn something that would help
    the *next* site (not the next iter on this site)? If yes,
    `memory_append` it.

**Orchestrator-driven note**: if this `/explore` was invoked by the
`explore-loop` orchestrator (env `CRAWLER_EXPLORER_ACTIVE_SITE` was set
at session start), do NOT invoke validation-agent yourself, do NOT
re-enter `/explore` after [DONE]. The orchestrator handles validation
and decides whether to start the next iter. Just exit cleanly after
step 11.

Do not stop after a single probe. Keep going until the workflow is
end-to-end validated or you've logged blockers with explanations.
