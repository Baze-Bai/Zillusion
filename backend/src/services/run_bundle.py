"""Bundle a multi-file run deliverable into one zip for browser download.

A run's deliverable is a DIRECTORY when the workflow is download-type (the
fetched files ARE the dataset) or when output.json is accompanied by on-disk
payloads (media/, weak-schema file_ref targets). The browser needs a single
file, so the download endpoint zips the run dir on request (scratch tmp file,
deleted after send) while the orchestrator only decides WHETHER a run needs
bundling and registers the directory as the artifact.

What ships: the data files (output.json, downloaded files, media/, file_ref
targets) plus the explainer files (report.md, manifest.yaml — carries
data_definition for weak-schema runs — and the task_plan.md field-doc
snapshot). Run-internal machinery (underscore-prefixed files, logs, the
records.jsonl truth file, the output_sample.json duplicate, feedback.yaml)
never ships.

Scratch zips go to $ZILLUSION_SCRATCH_DIR (fall back to the system temp dir)
— NEVER next to the data: the workspace drive is chronically near-full and a
transient double-copy there can wedge the whole pipeline.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import uuid
import zipfile
from pathlib import Path

# Top-level run-dir files that are machinery, not deliverable. Includes the
# inputs run_exec STAGES into the run dir to execute the crawl (workflow
# source + manifests, copied by _stage_inputs and left behind) — they are
# code, not data, and without this set every run looks multi-file and the
# single-output.json direct-link path never triggers.
_EXCLUDE_TOP_NAMES = {
    "crawl_stdout.log",
    "feedback.yaml",  # hypothesis feedback for the next /explore, not user data
    "workflow.py",
    "helpers.py",
    "selectors.yaml",
    "api_manifest.yaml",
    "download_manifest.yaml",
}

# Secrets must NEVER ship, at any depth: api runs stage the user's
# credentials.json next to workflow.py, and --output-root runs copy the
# project-global auth_state.json into the run dir. A same-named legitimate
# data file is theoretically conceivable but safety wins over completeness.
_EXCLUDE_SECRET_NAMES = {"credentials.json", "auth_state.json"}

# Top-level files that duplicate output.json — excluded ONLY when the
# canonical copy actually exists. Without that guard, a run whose only data
# survives as records.jsonl / output_sample.json (e.g. a salvage fallback or
# a crawl that died before materialization) would bundle WITHOUT its data.
_EXCLUDE_TOP_IF_CANONICAL = {
    "records.jsonl",  # agentic truth file — redundant with output.json
    "output_sample.json",  # sample-mode duplicate of output.json
}

# Top-level explainer files: they SHIP in a bundle but never TRIGGER one
# (every run writes report/manifest — only extra DATA makes a run multi-file).
_EXPLAINER_NAMES = {"report.md", "manifest.yaml", "task_plan.md"}

# Text formats worth deflating; everything else (media, already-compressed
# formats) is stored as-is.
_DEFLATE_SUFFIXES = {
    ".json",
    ".jsonl",
    ".csv",
    ".tsv",
    ".txt",
    ".md",
    ".markdown",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".htm",
    ".py",
    ".js",
    ".log",
    ".svg",
}


def _scratch_dir() -> Path:
    d = os.environ.get("ZILLUSION_SCRATCH_DIR")
    p = Path(d) if d else Path(tempfile.gettempdir())
    p.mkdir(parents=True, exist_ok=True)
    return p


def iter_bundle_files(run_dir: Path) -> list[Path]:
    """Every file under ``run_dir`` that belongs in the user's bundle, sorted.

    Excluded at any depth: underscore-prefixed files/dirs (control surface:
    _run_progress.json, _crawl_meta.json, _agent_messages.jsonl, …) and
    ``*.tmp`` partials. Excluded at the top level: the machinery names above,
    plus output.json duplicates — only while output.json itself is present.
    """
    has_canonical = (run_dir / "output.json").is_file()
    out: list[Path] = []
    for p in sorted(run_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(run_dir)
        if any(part.startswith("_") for part in rel.parts):
            continue
        if p.suffix.lower() == ".tmp":
            continue
        if p.name in _EXCLUDE_SECRET_NAMES:  # any depth — secrets never ship
            continue
        if len(rel.parts) == 1 and rel.parts[0] in _EXCLUDE_TOP_NAMES:
            continue
        if len(rel.parts) == 1 and rel.parts[0] in _EXCLUDE_TOP_IF_CANONICAL and has_canonical:
            continue
        out.append(p)
    return out


def data_files(run_dir: Path) -> list[Path]:
    """Bundle files that are DATA (not top-level explainers)."""
    return [
        p
        for p in iter_bundle_files(run_dir)
        if not (len(p.relative_to(run_dir).parts) == 1 and p.name in _EXPLAINER_NAMES)
    ]


def should_bundle(run_dir: Path, output_file: Path | None) -> bool:
    """True when the deliverable is multi-file: any DATA file beyond the single
    canonical output_file (download dirs pass ``None`` — any data file at all).
    Explainer files alone never trigger a bundle (single output.json stays a
    direct-download chip)."""
    try:
        resolved = output_file.resolve() if output_file is not None else None
        return any(p.resolve() != resolved for p in data_files(run_dir))
    except OSError:
        return False


def total_bytes(files: list[Path]) -> int:
    n = 0
    for p in files:
        with contextlib.suppress(OSError):
            n += p.stat().st_size
    return n


def build_zip(run_dir: Path, dest: Path, arc_root: str) -> int:
    """Zip the bundle files into ``dest`` under a single top-level folder
    ``arc_root/`` (so extraction yields one folder, not loose files). Returns
    the number of members written. Text formats are deflated; media/binary
    stored as-is."""
    files = iter_bundle_files(run_dir)
    with zipfile.ZipFile(dest, "w", allowZip64=True) as zf:
        for p in files:
            compress = (
                zipfile.ZIP_DEFLATED
                if p.suffix.lower() in _DEFLATE_SUFFIXES
                else zipfile.ZIP_STORED
            )
            arcname = f"{arc_root}/{p.relative_to(run_dir).as_posix()}"
            zf.write(p, arcname=arcname, compress_type=compress)
    return len(files)


def create_bundle(run_dir: Path, arc_root: str) -> Path:
    """Build the zip in the scratch dir and return its path. Caller owns
    deletion (the download endpoint unlinks it after the response)."""
    dest = _scratch_dir() / f"zillusion_bundle_{uuid.uuid4().hex}.zip"
    try:
        n = build_zip(run_dir, dest, arc_root)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    if n == 0:
        dest.unlink(missing_ok=True)
        raise FileNotFoundError(f"no bundleable files under {run_dir}")
    return dest
