# Domain skills

Cross-site, structured techniques the agent has accumulated. Read with
`skill_list` / `skill_read`; grow with `skill_propose`.

## Layout

```
domain_skills/
  <skill_id>/
    SKILL.md         title, when-to-use, description, evidence
    recipe.py        OPTIONAL Python snippet to adapt
    metadata.yaml    sites_seen, success_count, timestamps
```

## Bundled seed

One starter skill ships in this directory so the library is non-empty on
first use: `dismiss-cookie-banner-eu`. It demonstrates the file layout
and gives the agent a concrete pattern to imitate when proposing new
skills.

## Pruning

`python -c "from mcp_server.skill_library import SkillLibrary; ..."` or
just open the directory and inspect `metadata.yaml`. Skills with low
`success_count` after several runs are likely false generalisations.
