---
name: validate-workflow
description: Run a workspace's workflow.py end-to-end and validate output_sample.json against goal.md before declaring DONE.
when_to_use: before calling finish on /explore; or when the user asks "is the workflow ready?"
argument-hint: <site_id>
allowed-tools:
  - Bash
  - Read
---

# Validate workflow end-to-end

**Invocation** (the `args` field is required — it becomes `$ARGUMENTS` below):

    Skill(skill="validate-workflow", args="<site_id>")
    # e.g. Skill(skill="validate-workflow", args="reddit-trump")

Validation for site **$ARGUMENTS**:

!`python ${CLAUDE_SKILL_DIR}/scripts/check_output.py $ARGUMENTS`

If the script reported gaps:

1. Open `inputs/$1/goal.md` and compare with the printed field set.
2. Add a new hypothesis to `workspaces/$1/hypotheses.yaml` for each missing
   field, `status: unverified, priority: high`.
3. Re-enter the PROBE loop.

If the script reported OK:

1. Reflect: anything for `memory_append`? Anything for `skill_propose`?
2. End the run by declaring the route: `declare_crawl_route("deterministic",
   rationale)` — the declaration IS the finish (no [DONE] marker exists).
