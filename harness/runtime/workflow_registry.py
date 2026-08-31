"""Route version archive — every loop's final RECIPE, kept with full docs.

Operator decision (2026-06-11): every explore→validate loop end persists the
route's current recipe as an immutable version under
``workspaces/<site>/workflow_versions/vNNN_<ts>_<verdict>/``, bundled with a
complete human-readable document and machine-readable metadata. Server-side
archive only — nothing is registered as a UI artifact.

**A route's recipe is whatever a later run would need to redo the work**, and
that differs by route (2026-08-31 — until then only the first line was archived,
so an agentic site left no version behind at all and a deterministic one left a
version that could not actually be replayed):

  - ``deterministic`` → ``workflow.py`` **plus its sidecars**. The generated
    script is not self-contained: ``selectors.yaml`` carries the field semantics
    the validator checks against the live page, ``api_manifest.yaml`` IS the
    recipe for an api-type site (endpoints, auth, pagination — the script is
    just the shell that reads it), and ``download_manifest.yaml`` likewise.
    Archiving the script alone produced a version directory that documented a
    run nobody could reproduce.
  - ``agentic``       → ``crawl_brief.md``. There is no script; the brief (with
    its mandatory completeness anchor) is what the next crawl agent is handed.
  - ``inline`` / ``infeasible`` → nothing, on purpose. Inline harvested the whole
    set during explore, so its deliverable under ``runs/`` IS the artifact and
    there is no recipe to version; infeasible produced no crawl at all.

Each version directory contains:
  - the recipe          — ``workflow.py`` or ``crawl_brief.md`` (the workspace
                          copy keeps evolving; this snapshot does not)
  - its sidecars        — the manifests / selectors / helpers that existed
  - ``WORKFLOW_DOC.md`` — assembled documentation: the agent-written purpose
                          note (``workflow_purpose.md``; falls back to a goal
                          digest when absent — the section never goes missing),
                          the goal, the task_plan field table, how to run it,
                          an output-sample preview, and the loop's verdict line
  - ``meta.json``       — version, verdict, exit reason, iterations, cost, model,
                          recipe kind + sha256, source hints, file list
  - ``output_sample.json`` — the validated sample (deterministic) or a snapshot
                          of the agentic run's ``output.json`` (when present)

Dedup: an identical recipe (same kind AND same sha256 as the latest version)
does NOT mint a new version; instead the latest version's ``meta.json`` gains a
``revalidations`` entry — "same recipe, judged again" leaves a trace without
cloning the archive. Everything here is stdlib-only and best-effort: a failure
to archive must never affect the loop's verdict.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# WORKFLOW_DOC.md embeds a PREVIEW of output_sample.json; the full file is
# snapshotted alongside, so the preview is bounded to keep the doc readable.
_SAMPLE_PREVIEW_CHARS = 4096
# Goal digest used when the agent didn't leave workflow_purpose.md.
_GOAL_DIGEST_LINES = 40

_VERSION_DIR_RE = re.compile(r"^v(\d{3})_")

# The recipe, by route. Order matters: a site that somehow has both is a
# deterministic site whose earlier agentic brief was never cleaned up, and the
# script is what actually runs.
_RECIPES: tuple[tuple[str, str], ...] = (("workflow", "workflow.py"), ("crawl_brief", "crawl_brief.md"))
# Copied alongside a `workflow` recipe when present. Without these the snapshot
# documents a run it cannot reproduce — see the module docstring.
_SIDECARS: tuple[str, ...] = (
    "helpers.py",
    "selectors.yaml",
    "api_manifest.yaml",
    "download_manifest.yaml",
)


def _resolve_recipe(ws: Path) -> tuple[str, Path] | None:
    """(kind, path) of the artifact this version IS, or None when the route left
    no recipe (inline / infeasible / a loop that never got that far)."""
    for kind, name in _RECIPES:
        p = ws / name
        if p.is_file():
            return kind, p
    return None


def _recipe_id(meta: dict[str, Any]) -> tuple[str, str | None]:
    """Dedup key of an archived version. Versions written before recipes were a
    concept carry only ``workflow_sha256`` and are all deterministic."""
    return (meta.get("recipe_kind") or "workflow", meta.get("recipe_sha256") or meta.get("workflow_sha256"))


def _agentic_sample(ws: Path, run_id: str | None) -> Path | None:
    """The agentic route's sample: this loop's crawl output when the caller knew
    the run id, else the newest run that produced one. None when no run
    materialized output.json (a crawl that died before finalize)."""
    runs = ws / "runs"
    if run_id:
        p = runs / run_id / "output.json"
        return p if p.is_file() else None
    try:
        candidates = [p for p in runs.glob("*/output.json") if p.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utcnow_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_text(path: Path) -> str | None:
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return None


def _existing_versions(versions_dir: Path) -> list[Path]:
    if not versions_dir.is_dir():
        return []
    return sorted(
        (d for d in versions_dir.iterdir() if d.is_dir() and _VERSION_DIR_RE.match(d.name)),
        key=lambda d: d.name,
    )


def _latest_meta(versions: list[Path]) -> tuple[Path, dict[str, Any]] | None:
    for d in reversed(versions):
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                return d, meta
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _workflow_type_hint(site_id: str, ws: Path, root: Path, recipe_kind: str = "workflow") -> str:
    # The agentic route has no workflow type — it has no workflow. Reporting one
    # of the three deterministic types for it would be a lie in every meta.json.
    if recipe_kind == "crawl_brief":
        return "agentic"
    if (root / "inputs" / site_id / "api_spec.json").is_file():
        return "api"
    if (ws / "download_manifest.yaml").is_file():
        return "download"
    return "extraction"


def _source_url_hint(site_id: str, root: Path) -> str | None:
    """Best-effort source URL from the staged seed/spec; None when unknowable."""
    inputs = root / "inputs" / site_id
    for fname, keys in (
        ("seed.json", ("url", "source_url", "seed_url")),
        ("api_spec.json", ("base_url",)),
    ):
        try:
            j = json.loads((inputs / fname).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(j, dict):
            continue
        for k in keys:
            v = j.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        src = j.get("source")
        if isinstance(src, dict) and isinstance(src.get("url"), str) and src["url"].strip():
            return src["url"].strip()
    return None


def _purpose_section(ws: Path, site_id: str, root: Path) -> str:
    """The agent-written purpose note; degrades to a goal digest so the doc
    never ships without this section."""
    purpose = _read_text(ws / "workflow_purpose.md")
    if purpose and purpose.strip():
        return purpose.strip()
    goal = _read_text(root / "inputs" / site_id / "goal.md") or ""
    digest = "\n".join(goal.splitlines()[:_GOAL_DIGEST_LINES]).strip()
    note = "(agent 本轮未提供专门说明,以下为任务目标自动摘要 / no agent-written note this loop — goal digest follows)"
    return f"{note}\n\n{digest}" if digest else note


def _build_doc(
    site_id: str, ws: Path, root: Path, meta: dict[str, Any], sample_path: Path | None = None
) -> str:
    goal = _read_text(root / "inputs" / site_id / "goal.md")
    task_plan = _read_text(ws / "task_plan.md")
    sample = _read_text(sample_path) if sample_path else _read_text(ws / "output_sample.json")
    agentic = meta.get("recipe_kind") == "crawl_brief"

    parts: list[str] = []
    parts.append(f"# Workflow 说明 / Workflow Documentation — `{site_id}` v{meta['version']:03d}")
    parts.append("")
    parts.append(f"- **生成时间 (created)**: {meta['created_at']}")
    parts.append(f"- **验证结论 (verdict)**: {meta['verdict']}  —  {meta.get('exit_reason') or 'n/a'}")
    parts.append(f"- **目标站点 (source)**: {meta.get('source_url') or '(unknown)'}")
    parts.append(f"- **工作流类型 (type)**: {meta.get('workflow_type_hint')}")
    parts.append(
        f"- **迭代/成本 (iterations / cost)**: {meta.get('iterations_run')} iters,"
        f" ${meta.get('total_cost_usd')}  (model: {meta.get('model') or 'n/a'})"
    )
    parts.append(f"- **{meta.get('recipe_file', 'workflow.py')} sha256**: `{meta['recipe_sha256']}`")
    parts.append("")

    parts.append("## 作用与目的 (Purpose)")
    parts.append("")
    parts.append(_purpose_section(ws, site_id, root))
    parts.append("")

    if goal and goal.strip():
        parts.append("## 任务目标 (Goal — inputs/goal.md)")
        parts.append("")
        parts.append(goal.strip())
        parts.append("")

    if task_plan and task_plan.strip():
        parts.append("## 字段与计划 (Fields & plan — task_plan.md)")
        parts.append("")
        parts.append(task_plan.strip())
        parts.append("")

    parts.append("## 运行方法 (How to run)")
    parts.append("")
    parts.append("```bash")
    parts.append("# 在 harness 仓库根目录 / from the harness repo root:")
    if agentic:
        parts.append(f"python -m runtime.cli crawl {site_id}")
        parts.append("")
        parts.append("# 续跑上一次(保留已抓记录)/ resume, keeping records already committed:")
        parts.append(f"python -m runtime.cli crawl {site_id} --run-id {meta.get('run_id') or '<run-id>'}")
    else:
        parts.append(f"python -m runtime.cli run {site_id} --crawl-mode full")
    parts.append("```")
    parts.append("")
    parts.append(
        "- 依赖 (deps): `pip install -c constraints.txt .` +"
        " `python -m playwright install chromium`。"
    )
    parts.append(
        "- 输出 (output): `workspaces/<site>/runs/<run_id>/output.json` + `manifest.yaml`。"
    )
    if agentic:
        # The run-id default is the sharpest edge on this route: omitting it is a
        # full re-crawl of the whole site, which is both the expensive option and
        # the one you get by accident.
        parts.append(
            "- ⚠️ **不带 `--run-id` 会开一个全新 run,从零重抓整站**;带上上次的 run_id 才是续跑"
            "(`init_crawl` 会重载已提交记录的 id 去重)。/ Without `--run-id` this re-crawls the"
            " site from scratch under a fresh run; pass the previous one to resume."
        )
        parts.append(
            "- 本路线没有 `workflow.py`:抓取由 agent 现场驱动,`crawl_brief.md`(含完整性锚点)"
            "是它拿到的全部指令。/ No script on this route — the brief is the instruction."
        )
    if meta.get("workflow_type_hint") == "api":
        parts.append(
            "- 凭据 (credentials): api 类型站点需先提供 `inputs/"
            f"{site_id}/credentials.json`(shape: `{{\"api_key\": ..., \"extra\": {{}}}}`)。"
        )
    parts.append(
        f"- 本快照是不可变版本;工作区根部的 `{meta.get('recipe_file', 'workflow.py')}`"
        " 是持续演化的最新版。"
    )
    parts.append("")

    if sample:
        preview = sample[:_SAMPLE_PREVIEW_CHARS]
        truncated = len(sample) > _SAMPLE_PREVIEW_CHARS
        parts.append("## 输出样例预览 (Output sample preview)")
        parts.append("")
        if truncated:
            parts.append(
                f"(预览前 {_SAMPLE_PREVIEW_CHARS} 字符;完整样例见同目录 `output_sample.json` / "
                f"preview truncated — full file alongside)"
            )
            parts.append("")
        parts.append("```json")
        parts.append(preview)
        parts.append("```")
        parts.append("")

    return "\n".join(parts)


def save_workflow_version(
    site_id: str,
    *,
    verdict: str,
    exit_reason: str = "",
    iterations_run: int = 0,
    total_cost_usd: float = 0.0,
    model: str | None = None,
    root: Path | None = None,
    run_id: str | None = None,
) -> Path | None:
    """Archive the site's current route recipe as a documented immutable version.

    The recipe is ``workflow.py`` (deterministic) or ``crawl_brief.md``
    (agentic); see the module docstring. ``run_id`` names the agentic crawl this
    loop dispatched, so the doc can print a resume command and snapshot that
    run's output rather than guessing at the newest one.

    Returns the version directory (new, or the latest one when the recipe is
    unchanged and only a revalidation was recorded), or None when the route left
    no recipe / archiving failed. Never raises."""
    try:
        root = root or PROJECT_ROOT
        ws = root / "workspaces" / site_id
        resolved = _resolve_recipe(ws)
        if resolved is None:
            return None
        recipe_kind, recipe_path = resolved
        content = recipe_path.read_bytes()
        sha = hashlib.sha256(content).hexdigest()

        versions_dir = ws / "workflow_versions"
        versions = _existing_versions(versions_dir)
        latest = _latest_meta(versions)

        # Same recipe as the latest version → record a revalidation, no new copy.
        # Kind is part of the identity: a site that switched deterministic →
        # agentic has a genuinely different recipe even if a sha collided.
        if latest is not None and _recipe_id(latest[1]) == (recipe_kind, sha):
            latest_dir, meta = latest
            meta.setdefault("revalidations", []).append(
                {
                    "at": _utcnow_iso(),
                    "verdict": verdict,
                    "exit_reason": exit_reason,
                    "iterations_run": iterations_run,
                    "total_cost_usd": round(total_cost_usd, 4),
                }
            )
            (latest_dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return latest_dir

        n = 1
        if versions:
            m = _VERSION_DIR_RE.match(versions[-1].name)
            if m:
                n = int(m.group(1)) + 1
        safe_verdict = re.sub(r"[^A-Za-z]", "", verdict or "UNSET").upper() or "UNSET"
        vdir = versions_dir / f"v{n:03d}_{_utcnow_compact()}_{safe_verdict}"
        vdir.mkdir(parents=True, exist_ok=True)

        (vdir / recipe_path.name).write_bytes(content)
        # Sidecars: the deterministic recipe is not self-contained without them.
        sidecars: list[str] = []
        if recipe_kind == "workflow":
            for name in _SIDECARS:
                src = ws / name
                if src.is_file():
                    shutil.copy2(src, vdir / name)
                    sidecars.append(name)

        if recipe_kind == "crawl_brief":
            sample_src = _agentic_sample(ws, run_id)
        else:
            sample_src = ws / "output_sample.json"
            if not sample_src.is_file():
                sample_src = None
        if sample_src is not None:
            shutil.copy2(sample_src, vdir / "output_sample.json")

        meta: dict[str, Any] = {
            "version": n,
            "site_id": site_id,
            "created_at": _utcnow_iso(),
            "verdict": verdict,
            "exit_reason": exit_reason,
            "iterations_run": iterations_run,
            "total_cost_usd": round(total_cost_usd, 4),
            "model": model,
            "recipe_kind": recipe_kind,
            "recipe_file": recipe_path.name,
            "recipe_sha256": sha,
            "sidecars": sidecars,
            "run_id": run_id,
            "workflow_type_hint": _workflow_type_hint(site_id, ws, root, recipe_kind),
            "source_url": _source_url_hint(site_id, root),
            "files": sorted(p.name for p in vdir.iterdir()) + ["meta.json", "WORKFLOW_DOC.md"],
            "revalidations": [],
        }
        # Kept for readers written against the pre-2026-08-31 shape, which only
        # ever saw deterministic versions; absent on an agentic one on purpose,
        # so nothing can mistake a brief for a script.
        if recipe_kind == "workflow":
            meta["workflow_sha256"] = sha
        (vdir / "WORKFLOW_DOC.md").write_text(
            _build_doc(site_id, ws, root, meta, sample_src), encoding="utf-8"
        )
        meta["files"] = sorted(p.name for p in vdir.iterdir() if p.name != "meta.json") + [
            "meta.json"
        ]
        (vdir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return vdir
    except Exception:  # noqa: BLE001 — archiving must never break the loop
        return None
