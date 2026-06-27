---
description: Validate a /explore run's deliverable by running the canonical self-contained validator (runtime.validate) on the site. Independent trust-but-verify pass over workflow.py's output against goal.md.
argument-hint: <site_id>
---

Run the canonical validator on site `$ARGUMENTS`.

The validator is the self-contained SDK agent in `runtime/validate.py` — a
fresh session whose entire tool surface is the in-process validator MCP
server (11 `mcp__validator__*` tools) + read-only `Read`. It is read-only on
every explore artifact and writes ONLY
`workspaces/$ARGUMENTS/validation/<run_id>/`. This is the SAME validator the
explore-loop runs between iterations, so a manual run and a loop-internal run
reach the same gate-computed verdict.

Run it from the project root with the venv python:

```cmd
.venv\Scripts\python.exe -m runtime.cli validate $ARGUMENTS
```

The last line of its output is the verdict contract (the orchestrator parses
it with a regex):

    [PASS|FAIL|INCONCLUSIVE|ERROR] $ARGUMENTS run_id=<val-XXXXXXXX> — <one-line reason>

After it returns, report that verdict line to the user verbatim and point
them at `workspaces/$ARGUMENTS/validation/<run_id>/` — `scorecard.yaml`
(gate-computed verdict), `report.md` (narrative), `feedback.yaml`
(hypothesis feedback). The next `/explore $ARGUMENTS` reads `feedback.yaml`
via the SessionStart hook and folds it into its own hypotheses.

Do not invoke `/explore` here — let the user decide whether to iterate.
