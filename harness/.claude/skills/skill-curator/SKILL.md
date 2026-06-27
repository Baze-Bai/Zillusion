---
name: skill-curator
description: Decide whether an observation belongs in memory, in a per-site helper, or in a cross-site domain skill; prune the library over time.
when_to_use: at the end of an exploration run, or when you notice a repeated pattern across sites
---

# Skill curation

The library has three layers. Putting things in the right layer is what
keeps the system from drowning in noise.

| Layer | File | Lifetime | When to put a thing here |
| --- | --- | --- | --- |
| Per-site helper | `workspaces/<id>/helpers.py` | one run | Specific to this site; not transferable. Append-only via `workspace_helper_append`. |
| Cross-site memory | `memory/<topic>.md` | grows over runs | One-off observation that *might* matter later but isn't proven yet. Cheap to write. |
| Cross-site skill | `domain_skills/<id>/SKILL.md` | grows over runs | Proven technique that has worked on 1+ sites and is structurally reusable. |

## Promotion rules

### memory -> skill

Promote a memory note to a skill when:

1. The same pattern shows up on a *second* site.
2. The fix is structurally the same (not just "the right thing to do").
3. You can write a `when_to_use` that is concrete enough another run can
   evaluate it.

Don't promote based on theoretical reusability. The bar is: "I have already
applied this twice and it worked twice."

### helper -> skill

Promote a per-site helper to a skill when:

1. You catch yourself writing the same helper on two different sites.
2. The helper is genuinely site-agnostic (no hard-coded selectors that
   only apply to one site).

Keep the helper in the site's `helpers.py` *and* add the skill. Skills can
reference recipes; helpers reference live selectors.

## When to *demote* (or prune)

A skill with `success_count == 1` after 5+ runs is a false generalisation.
Either:

- Edit `SKILL.md` to narrow `when_to_use` until it matches reality, or
- Remove the skill directory entirely. The memory note that spawned it
  may still be valid.

`skill_record_use(success=False)` calls are signals too - high failure
ratio means the skill's premise is wrong.

## Naming

Skill IDs should be:

- Descriptive: `dismiss-cookie-banner-eu`, not `cookies-1`.
- Stable: don't rename; the file path is the foreign key.
- Specific enough: `paginate-infinite-scroll-twitter-style` beats
  `paginate-infinite-scroll`, because "twitter-style" actually carries
  technical content (cursor with id-anchor, etc.).

## End-of-run reflection

Right before ending the run (declaring the route):

1. Skim the exploration log. Did you do anything twice?
2. If yes, is it site-specific (helper) or site-agnostic (skill)?
3. Is there a *cross-site* observation worth a `memory_append`?
   ("Firecrawl missed the GraphQL endpoint here too.")
4. Should an existing memory note become a skill now?

Spend a couple of minutes here. The library is what makes runs 6+ much
faster than runs 1-5.
