"""Workspace and memory layers (harness variant).

Distinction from the Playwright variant: ``helpers.py`` is enforced
append-only here too, mirroring the harness philosophy of "grow your
toolbox, don't rewrite history". When a helper needs to evolve, the
agent adds ``_v2`` / ``_v3`` and leaves the old version visible.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from mcp_server.schemas import (
    Hypothesis,
    HypothesesFile,
    SelectorsFile,
    DownloadManifestFile,
    APIManifestFile,
    DiscoveredSourcesFile,
    HypothesisPriority,
    HypothesisStatus,
)
from mcp_server.schemas.hypotheses import append_hypothesis_to_file, WallType
from mcp_server.schemas.iter_summary import (
    append_section as _iter_summary_append_section,
    load_latest_section as _iter_summary_load_latest,
    next_iter_n as _iter_summary_next_n,
)

LOG_FILE = "exploration_log.md"
HYPOTHESES_FILE = "hypotheses.yaml"
SELECTORS_FILE = "selectors.yaml"
DOWNLOAD_MANIFEST_FILE = "download_manifest.yaml"
API_MANIFEST_FILE = "api_manifest.yaml"
DISCOVERED_SOURCES_FILE = "discovered_sources.yaml"
ITER_SUMMARY_FILE = "iter_summary.md"
FACTS_FILE = "verified_facts.md"
HELPERS_FILE = "helpers.py"
WORKFLOW_FILE = "workflow.py"
WORKFLOW_HISTORY_DIR = "workflow_history"
OUTPUT_SAMPLE_FILE = "output_sample.json"
LAST_RUN_STATUS_FILE = "_last_run_status.json"
SAMPLES_DIR = "samples"

# Files that must NOT be written via the generic write_file() path —
# they have schemas validated through dedicated methods. Trying to
# workspace_write these raises WorkspacePathError redirecting to the
# correct entry point. Same defense-in-depth pattern as the existing
# append-only files (exploration_log.md, helpers.py).
SCHEMA_MANAGED_FILES = {
    HYPOTHESES_FILE: "Use hypothesis_append / hypothesis_set_status MCP tools (schema-validated via mcp_server.schemas.HypothesesFile).",
    SELECTORS_FILE: "Use selectors_write MCP tool (schema-validated via mcp_server.schemas.SelectorsFile).",
    DOWNLOAD_MANIFEST_FILE: "Use download_manifest_write MCP tool (schema-validated via mcp_server.schemas.DownloadManifestFile).",
    API_MANIFEST_FILE: "Use api_manifest_write MCP tool (schema-validated via mcp_server.schemas.APIManifestFile).",
    DISCOVERED_SOURCES_FILE: "Use report_discovered_source MCP tool (schema-validated via mcp_server.schemas.DiscoveredSourcesFile).",
}
HELPERS_HEADER = (
    '"""Site-specific helpers grown by the agent during exploration.\n'
    "\n"
    "Append-only via the `workspace_helper_append` tool. Old definitions\n"
    "stay in place; evolve by adding a new function with a different name.\n"
    '"""\n\n'
)


class WorkspacePathError(ValueError):
    pass


class Workspace:
    def __init__(self, root: Path, site_id: str) -> None:
        if not site_id or "/" in site_id or "\\" in site_id or site_id.startswith("."):
            raise ValueError(f"invalid site_id: {site_id!r}")
        self.site_id = site_id
        self.root = root.resolve()
        self.dir = (self.root / site_id).resolve()
        self.samples_dir = self.dir / SAMPLES_DIR

    def init(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.samples_dir.mkdir(parents=True, exist_ok=True)
        log = self.dir / LOG_FILE
        if not log.exists():
            log.write_text(f"# Exploration log for {self.site_id}\n\n", encoding="utf-8")
        helpers = self.dir / HELPERS_FILE
        if not helpers.exists():
            helpers.write_text(HELPERS_HEADER, encoding="utf-8")

    def path(self, relative: str) -> Path:
        target = (self.dir / relative).resolve()
        try:
            target.relative_to(self.dir)
        except ValueError as exc:
            raise WorkspacePathError(f"path escapes workspace: {relative}") from exc
        return target

    def append_log(self, section: str, body: str) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with (self.dir / LOG_FILE).open("a", encoding="utf-8") as f:
            f.write(f"\n## [{ts}] {section}\n\n{body.rstrip()}\n")

    def read_file(self, relative: str) -> str:
        path = self.path(relative)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def write_file(self, relative: str, content: str) -> None:
        if relative == LOG_FILE:
            raise WorkspacePathError("exploration_log.md is append-only")
        if relative == HELPERS_FILE:
            raise WorkspacePathError("helpers.py is append-only")
        if relative in SCHEMA_MANAGED_FILES:
            raise WorkspacePathError(
                f"{relative} is schema-managed and cannot be written via workspace_write. "
                f"{SCHEMA_MANAGED_FILES[relative]}"
            )
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    # ──────────────────────────────────────────────────────────────────
    # Schema-aware IO for hypotheses.yaml
    # ──────────────────────────────────────────────────────────────────

    def read_hypotheses(self) -> HypothesesFile:
        """Load hypotheses.yaml validated against the Pydantic schema.

        Raises with clear message if file missing / unparseable / wrong shape.
        Returns an empty HypothesesFile if file doesn't exist yet (so the
        agent can call append_hypothesis on a fresh workspace).
        """
        path = self.dir / HYPOTHESES_FILE
        if not path.exists():
            return HypothesesFile.model_validate([])
        return HypothesesFile.load(path)

    def write_hypotheses(self, file: HypothesesFile) -> None:
        """Persist a full HypothesesFile. Use this for bulk rewrites
        (e.g. /explore at end of run). For single-record additions
        prefer append_hypothesis()."""
        file.save(self.dir / HYPOTHESES_FILE)

    def append_hypothesis(
        self,
        *,
        claim: str,
        source: str,
        status: HypothesisStatus = "unverified",
        priority: HypothesisPriority = "high",
        notes: str | None = None,
        result: str | None = None,
    ) -> Hypothesis:
        """Add a new hypothesis with auto-assigned id, schema-validated.

        Will refuse to write if hypotheses.yaml already exists but has the
        wrong shape (mapping instead of list, unparseable YAML, etc.) —
        the surfaced error tells the caller to fix the file before append.
        This replaces the previous string-append anti-pattern that
        silently produced unparseable YAML.
        """
        return append_hypothesis_to_file(
            self.dir / HYPOTHESES_FILE,
            claim=claim,
            source=source,
            status=status,
            priority=priority,
            notes=notes,
            result=result,
        )

    def set_hypothesis_status(
        self,
        hypothesis_id: str,
        status: HypothesisStatus,
        *,
        result: str | None = None,
        notes: str | None = None,
        wall_type: WallType | None = None,
    ) -> Hypothesis:
        """Update status on an existing hypothesis. Raises KeyError if not found."""
        file = self.read_hypotheses()
        updated = file.set_status(
            hypothesis_id, status, result=result, notes=notes, wall_type=wall_type
        )
        self.write_hypotheses(file)
        return updated

    # ──────────────────────────────────────────────────────────────────
    # Schema-aware IO for selectors.yaml
    # ──────────────────────────────────────────────────────────────────

    def read_selectors(self) -> SelectorsFile:
        """Load selectors.yaml validated against the Pydantic schema.
        Raises FileNotFoundError if missing — selectors are required for
        validation-agent to verify, callers should catch and treat as
        'no selectors yet, /explore must run first'.
        """
        return SelectorsFile.load(self.dir / SELECTORS_FILE)

    def write_selectors(self, file: SelectorsFile) -> None:
        """Persist a full SelectorsFile (full overwrite — selector catalog
        is best treated as one atomic snapshot, not incrementally appended)."""
        file.save(self.dir / SELECTORS_FILE)

    def read_download_manifest(self) -> DownloadManifestFile:
        """Load download_manifest.yaml validated against the schema. Raises
        FileNotFoundError if missing (no download workflow declared for this run)."""
        return DownloadManifestFile.load(self.dir / DOWNLOAD_MANIFEST_FILE)

    def write_download_manifest(self, file: DownloadManifestFile) -> None:
        """Persist a full DownloadManifestFile (full overwrite — one atomic snapshot)."""
        file.save(self.dir / DOWNLOAD_MANIFEST_FILE)

    def read_api_manifest(self) -> APIManifestFile:
        """Load api_manifest.yaml validated against the schema. Raises
        FileNotFoundError if missing (no api workflow declared for this run)."""
        return APIManifestFile.load(self.dir / API_MANIFEST_FILE)

    def write_api_manifest(self, file: APIManifestFile) -> None:
        """Persist a full APIManifestFile (full overwrite — one atomic snapshot)."""
        file.save(self.dir / API_MANIFEST_FILE)

    def append_discovered_source(self, item) -> int:
        """Append one DiscoveredSource to discovered_sources.yaml; return new total.
        Load-append-save (the agent may report several over a run)."""
        path = self.dir / DISCOVERED_SOURCES_FILE
        f = DiscoveredSourcesFile.load(path)
        f.sources.append(item)
        f.save(path)
        return len(f.sources)

    # ──────────────────────────────────────────────────────────────────
    # iter_summary.md (append-only, agent self-retrospection)
    # ──────────────────────────────────────────────────────────────────

    def append_iter_summary(
        self,
        *,
        iter_n: int | None = None,
        cost_usd: float | None = None,
        record_count: int | None = None,
        tried: list[str] | None = None,
        worked: list[str] | None = None,
        do_not_retry: list[str] | None = None,
        open_hypotheses: list[str] | None = None,
        next_strategy: str = "",
        free_text: str = "",
    ) -> int:
        """Append a new ## Iter N section to iter_summary.md.

        Designed to be called once at the end of each /explore run (right
        before the route declaration that closes the run). The
        file is append-only — sections accumulate; the SessionStart hook
        injects the LATEST section into the next /explore run, so this
        is the actor's primary mechanism for telling the next iteration
        "here's what I tried, what didn't work, what to try next".

        Returns the iter_n actually written (auto-computed if None).
        """
        return _iter_summary_append_section(
            self.dir / ITER_SUMMARY_FILE,
            site_id=self.site_id,
            iter_n=iter_n,
            cost_usd=cost_usd,
            record_count=record_count,
            tried=tried,
            worked=worked,
            do_not_retry=do_not_retry,
            open_hypotheses=open_hypotheses,
            next_strategy=next_strategy,
            free_text=free_text,
        )

    def read_latest_iter_summary(self):
        """Return the most recent ## Iter N section as an IterSummarySection
        named-tuple, or None if file missing / empty. Used by hook + orchestrator."""
        return _iter_summary_load_latest(self.dir / ITER_SUMMARY_FILE)

    def next_iter_summary_n(self) -> int:
        """Convenience — what would the next iter_n be?"""
        return _iter_summary_next_n(self.dir / ITER_SUMMARY_FILE)

    def append_facts(self, body: str) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path = self.dir / FACTS_FILE
        if not path.exists():
            path.write_text(f"# Verified facts for {self.site_id}\n\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n## [{ts}]\n\n{body.rstrip()}\n")

    def helper_append(self, name: str, code: str) -> None:
        path = self.dir / HELPERS_FILE
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        block = f"\n# --- {name} (added {ts}) ---\n{code.rstrip()}\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(block)

    def list_samples(self) -> list[str]:
        if not self.samples_dir.exists():
            return []
        return sorted(p.name for p in self.samples_dir.iterdir() if p.is_file())


class Memory:
    """Free-form cross-site notes the agent writes during a run.

    Sibling to `SkillLibrary`. Skills are structured and reusable as code;
    memory is loose prose. The two grow together: memory entries that
    stabilise across multiple sites get promoted into skills.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def index(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_file() and p.suffix == ".md")

    def read(self, name: str) -> str:
        path = (self.root / name).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise WorkspacePathError(f"memory name escapes root: {name}") from exc
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def append(self, name: str, body: str) -> None:
        path = (self.root / name).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise WorkspacePathError(f"memory name escapes root: {name}") from exc
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as f:
            if path.stat().st_size == 0:
                f.write(f"# {name}\n")
            f.write(f"\n## [{ts}]\n\n{body.rstrip()}\n")
