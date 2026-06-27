---
description: Scaffold a new inputs/<site_id>/ folder with empty seed.json and goal.md
argument-hint: <site_id>
allowed-tools:
  - Bash
  - Write
---

Scaffold a new site folder for **$ARGUMENTS**.

!`mkdir -p inputs/$ARGUMENTS && echo "scaffolded inputs/$ARGUMENTS"`

Now create these two files (if they don't already exist):

1. `inputs/$ARGUMENTS/seed.json`:

```json
{
  "seed_url": "https://example.com",
  "page_tree": {},
  "notes": "Replace this with the seed URL and any Firecrawl page_tree output. Remember: this is a hypothesis, not a fact."
}
```

2. `inputs/$ARGUMENTS/goal.md`:

```markdown
# Goal

(describe what data the crawler should collect)

## Required fields

- `field_a`
- `field_b`

## Scope

- (how deep, how many pages, what to skip)

## Constraints

- (auth, rate limits, etc.)
```

Then ask the user to fill them in. When they're ready, run `/explore $ARGUMENTS`.
