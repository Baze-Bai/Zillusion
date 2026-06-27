"""Workflow version archive — every loop's final workflow.py, kept with full docs.

Operator decision (2026-06-11): every explore→validate loop end persists the
CURRENT workflow.py as an immutable version under
``workspaces/<site>/workflow_versions/vNNN_<ts>_<verdict>/``, bundled with a
complete human-readable document and machine-readable metadata. Server-side
archive only — nothing is registered as a UI artifact.

Each version directory contains:
  - ``workflow.py``         — the snapshot (the mutable workspace copy keeps evolving)
  - ``WORKFLOW_DOC.md``     — assembled documentation: the agent-written purpose
                              note (``workflow_purpose.md``; falls back to a goal
                              digest when absent — the section never goes missing),
                              the goal, the task_plan field table, how to run it,
                              an output-sample preview, and the loop's verdict line
  - ``meta.json``           — version, verdict, exit reason, iterations, cost,
                              model, workflow sha256, source hints, file list
  - ``output_sample.json``  — snapshot of the validated sample (when present)

Dedup: identical workflow bytes (same sha256 as the latest version) do NOT mint
a new version; instead the latest version's ``meta.json`` gains a
``revalidations`` entry — "same code, judged again" leaves a trace without
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


def _workflow_type_hint(site_id: str, ws: Path, root: Path) -> str:
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


def _build_doc(site_id: str, ws: Path, root: Path, meta: dict[str, Any]) -> str:
    goal = _read_text(root / "inputs" / site_id / "goal.md")
    task_plan = _read_text(ws / "task_plan.md")
    sample = _read_text(ws / "output_sample.json")

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
    parts.append(f"- **workflow.py sha256**: `{meta['workflow_sha256']}`")
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
    parts.append(f"python -m runtime.cli run {site_id} --crawl-mode full")
    parts.append("```")
    parts.append("")
    parts.append(
        "- 依赖 (deps): `pip install .` + `python -m playwright install chromium`,"
        " 或直接使用沙箱镜像 `zillusion-harness:latest`。"
    )
    parts.append(
        "- 输出 (output): `workspaces/<site>/runs/<run_id>/output.json` + `manifest.yaml`。"
    )
    if meta.get("workflow_type_hint") == "api":
        parts.append(
            "- 凭据 (credentials): api 类型站点需先提供 `inputs/"
            f"{site_id}/credentials.json`(shape: `{{\"api_key\": ..., \"extra\": {{}}}}`)。"
        )
    parts.append(
        "- 本快照是不可变版本;工作区根部的 `workflow.py` 是持续演化的最新版。"
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
) -> Path | None:
    """Archive the site's current workflow.py as a documented immutable version.

    Returns the version directory (new, or the latest one when the workflow
    bytes are unchanged and only a revalidation was recorded), or None when
    there is no workflow.py / archiving failed. Never raises."""
    try:
        root = root or PROJECT_ROOT
        ws = root / "workspaces" / site_id
        wf = ws / "workflow.py"
        if not wf.is_file():
            return None
        content = wf.read_bytes()
        sha = hashlib.sha256(content).hexdigest()

        versions_dir = ws / "workflow_versions"
        versions = _existing_versions(versions_dir)
        latest = _latest_meta(versions)

        # Same bytes as the latest version → record a revalidation, no new copy.
        if latest is not None and latest[1].get("workflow_sha256") == sha:
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

        (vdir / "workflow.py").write_bytes(content)
        sample = ws / "output_sample.json"
        if sample.is_file():
            shutil.copy2(sample, vdir / "output_sample.json")

        meta: dict[str, Any] = {
            "version": n,
            "site_id": site_id,
            "created_at": _utcnow_iso(),
            "verdict": verdict,
            "exit_reason": exit_reason,
            "iterations_run": iterations_run,
            "total_cost_usd": round(total_cost_usd, 4),
            "model": model,
            "workflow_sha256": sha,
            "workflow_type_hint": _workflow_type_hint(site_id, ws, root),
            "source_url": _source_url_hint(site_id, root),
            "files": sorted(p.name for p in vdir.iterdir()) + ["meta.json", "WORKFLOW_DOC.md"],
            "revalidations": [],
        }
        (vdir / "WORKFLOW_DOC.md").write_text(
            _build_doc(site_id, ws, root, meta), encoding="utf-8"
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
