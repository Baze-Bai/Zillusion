"""Cross-site skill library.

A skill is a durable, site-agnostic technique: dismiss a cookie banner of
a particular shape, detect a Cloudflare challenge, paginate a Twitter-
style infinite scroll, etc.

Storage:

    domain_skills/
        <skill_id>/
            SKILL.md        title, when-to-use, description, evidence
            recipe.py       OPTIONAL Python snippet the agent may adapt
            metadata.yaml   sites_seen, success_count, timestamps

The MCP server only exposes structured read/list/upsert/record-use tools;
the agent edits content through Claude Code's own Read/Write/Edit on the
files themselves. That dual surface is deliberate: structured tools keep
the metadata consistent, while free-form editing keeps the content human-
readable and human-editable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class SkillEntry(BaseModel):
    skill_id: str
    title: str
    description: str
    when_to_use: str
    evidence: str | None = None
    recipe: str | None = None
    sites_seen: list[str] = Field(default_factory=list)
    success_count: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


class SkillLibrary:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def index(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for sid in self.list():
            entry = self.load(sid)
            if entry is None:
                continue
            out.append(
                {
                    "skill_id": entry.skill_id,
                    "title": entry.title,
                    "when_to_use": entry.when_to_use,
                    "success_count": str(entry.success_count),
                }
            )
        return out

    def load(self, skill_id: str) -> SkillEntry | None:
        d = self.root / skill_id
        if not d.exists():
            return None
        meta_path = d / "metadata.yaml"
        skill_path = d / "SKILL.md"
        if not meta_path.exists() or not skill_path.exists():
            return None
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        skill_md = skill_path.read_text(encoding="utf-8")
        recipe_path = d / "recipe.py"
        recipe = recipe_path.read_text(encoding="utf-8") if recipe_path.exists() else None
        title = next((ln[2:].strip() for ln in skill_md.splitlines() if ln.startswith("# ")), skill_id)
        return SkillEntry(
            skill_id=skill_id,
            title=meta.get("title", title),
            description=meta.get("description", ""),
            when_to_use=meta.get("when_to_use", ""),
            evidence=meta.get("evidence"),
            recipe=recipe,
            sites_seen=meta.get("sites_seen", []),
            success_count=meta.get("success_count", 0),
            created_at=meta.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=meta.get("updated_at", datetime.now(timezone.utc).isoformat()),
        )

    def upsert(self, entry: SkillEntry) -> Path:
        d = self.root / entry.skill_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(self._render_skill_md(entry), encoding="utf-8")
        if entry.recipe is not None:
            (d / "recipe.py").write_text(entry.recipe, encoding="utf-8")
        meta = {
            "title": entry.title,
            "description": entry.description,
            "when_to_use": entry.when_to_use,
            "evidence": entry.evidence,
            "sites_seen": sorted(set(entry.sites_seen)),
            "success_count": entry.success_count,
            "created_at": entry.created_at,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (d / "metadata.yaml").write_text(
            yaml.safe_dump(meta, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        return d

    def record_use(self, skill_id: str, site_id: str, success: bool) -> None:
        entry = self.load(skill_id)
        if entry is None:
            return
        if site_id not in entry.sites_seen:
            entry.sites_seen.append(site_id)
        if success:
            entry.success_count += 1
        self.upsert(entry)

    @staticmethod
    def _render_skill_md(entry: SkillEntry) -> str:
        parts = [
            f"# {entry.title}",
            "",
            f"**ID**: `{entry.skill_id}`",
            f"**When to use**: {entry.when_to_use}",
            "",
            "## Description",
            "",
            entry.description.strip() or "(no description)",
            "",
        ]
        if entry.evidence:
            parts += ["## Evidence", "", entry.evidence.strip(), ""]
        if entry.recipe:
            parts += ["## Recipe (see `recipe.py`)", "", "```python", entry.recipe.strip(), "```", ""]
        return "\n".join(parts)
