---
description: Dump the current workspace state for a site (hypotheses, facts, log tail, sample count)
argument-hint: <site_id>
allowed-tools:
  - Bash
---

# Workspace state for $ARGUMENTS

!`ls workspaces/$ARGUMENTS 2>/dev/null && echo "---" && echo "## hypotheses.yaml" && cat workspaces/$ARGUMENTS/hypotheses.yaml 2>/dev/null | head -80 && echo "---" && echo "## verified_facts.md" && cat workspaces/$ARGUMENTS/verified_facts.md 2>/dev/null && echo "---" && echo "## exploration_log.md (tail)" && tail -40 workspaces/$ARGUMENTS/exploration_log.md 2>/dev/null && echo "---" && echo "## samples/" && ls workspaces/$ARGUMENTS/samples 2>/dev/null | head -20`

Summarise what you see for the user: what's confirmed, what's still
unverified, what the audit trail shows, what evidence exists.
