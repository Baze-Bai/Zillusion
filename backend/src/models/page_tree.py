"""Data page tree model for list→detail page structures in embedded data."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DataPageNode(BaseModel):
    """A single node in the data page tree."""

    url: str
    page_type: str  # list / detail / category / pagination / hub
    title: str = ""
    depth: int = 0  # Tree depth, root=0
    fields_available: list[str] = Field(default_factory=list)
    record_count: int | None = None  # Number of records (for list pages)
    children: list[DataPageNode] = Field(default_factory=list)
    is_sampled: bool = False  # Whether actually fetched (vs inferred from URL)
    # Session-relative POSIX path to the saved markdown (e.g.
    # "crawled/csi-x/abc.md"), populated by crawl_list_tree / firecrawl_map
    # fallback. Empty when the node wasn't fetched. Agent uses this to
    # Read the full content when the markdown_excerpt isn't enough.
    markdown_path: str = ""


class DataPageTree(BaseModel):
    """Complete tree from list page to detail pages."""

    root: DataPageNode
    # `null` is a valid value the agent emits when the count is genuinely
    # unknowable (page rendered server-side without a total stamp). Strictly
    # typing these as `int` made hotel-a3/hotel-5c trees fail Pydantic
    # validation and silently disappear from the final report — Agoda lost
    # both rounds for this reason. `int | None` keeps the tree usable.
    total_detail_pages: int | None = 0
    sampled_detail_pages: int | None = 0
    field_progression: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Fields available at each level, e.g. {'list_page': [...], 'detail_page': [...]}",
    )
    tree_summary: str = ""  # LLM-generated natural language description

    # Evidence anchor — the session_id returned by the crawl_list_tree (or
    # firecrawl_map fallback) call that produced this tree. Required by the
    # prompt; runner-side warn-only validation falls back to empty string +
    # warning if the agent omits it. Used by _validate_portal_tree_evidence
    # to look up the actual tool result in the run log and detect narrative
    # drift (agent says "0 hotel data" but tool reported 8 leaves).
    crawl_session_id: str = ""

    # Populated by runner-side validation. Empty list = clean; non-empty =
    # narrative-evidence mismatch was flagged (warn-only — the tree is still
    # included in the result, but downstream nodes/the diagnostics writer
    # surface these warnings for offline review).
    validation_warnings: list[str] = Field(default_factory=list)
