---
name: validation-agent
description: Validate a /explore run's deliverable. The canonical validator is the self-contained SDK agent in runtime/validate.py (run via `python -m runtime.cli validate <site_id>`). This subagent definition is a thin compatibility stub that defers to it — the old script-based state machine (run_standalone.py / verify_data_accuracy.py) was removed.
tools: Bash, Read
---

# Validation Agent (compatibility stub)

The validator is no longer a subagent state machine. It is the
**self-contained SDK agent in `runtime/validate.py`** — a fresh session
whose entire tool surface is the in-process validator MCP server (11
`mcp__validator__*` tools) + read-only `Read`. It is read-only on every
explore artifact and writes ONLY `workspaces/<site>/validation/<run_id>/`.
Each dimension is recorded in `scorecard.yaml`; the verdict is
**gate-computed** (any gating dim `fail` → fail; all gating dims `pass`/`n/a`
→ pass; otherwise inconclusive), and the agent's last output line is the
contract:

    [PASS|FAIL|INCONCLUSIVE|ERROR] <site_id> run_id=<val-XXXXXXXX> — <one-line reason>

If you were spawned to validate site `$ARGUMENTS`, run the canonical
validator and relay its verdict line verbatim:

```
Bash: .venv\Scripts\python.exe -m runtime.cli validate $ARGUMENTS
```

Do not run `workflow.py` yourself, do not edit any explore artifact, do not
invoke `/explore`. For the dimension list, the gate, and the FAIL discipline
(hard-FAIL only on a gating dim with an independent basis; completeness is a
multi-round loop outcome, never a single-pass FAIL), see the inline system
prompt in `runtime/validate.py`.
