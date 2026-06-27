---
name: in-workspace
description: Reminder of workspace conventions whenever a file inside workspaces/ is touched.
when_to_use: any tool call that reads or writes inside workspaces/<site_id>/
paths:
  - workspaces/**
disable-model-invocation: true
---

# Workspace conventions reminder

You are operating inside a per-site workspace. Conventions:

- `exploration_log.md` is **append-only**. Use
  `workspace_append_log(section=..., body=...)`.
- `helpers.py` is **append-only**. Use
  `workspace_helper_append(name=..., code=...)`. To evolve a helper, add
  a new function with a different name; do not rewrite.
- `verified_facts.md` is append-only by convention. Use
  `workspace_append_facts`.
- `samples/` contains DOM + screenshot pairs produced by `browser_snapshot`.
  Each call writes `<ISO-timestamp>_<label>.html` (full `page.content()`) and
  `<ISO-timestamp>_<label>.png` (full-page `page.screenshot()`).

A PreToolUse hook will reject direct Edit/Write/MultiEdit on
`exploration_log.md` and `helpers.py`. Edit / Read / Bash work for the
other workspace files (`hypotheses.yaml`, `workflow.py`,
`output_sample.json`).
