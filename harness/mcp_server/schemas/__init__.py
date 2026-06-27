"""Framework-level Pydantic schemas for workspace artifacts.

These models are the **single source of truth** for the shape of files
that multiple skills + tools produce and consume. Without this layer,
every producer (`hypothesis-loop`, `validation-agent`, ...) and every
consumer (the validator, `SessionStart` hook, ...) would
maintain its own mental model — and they would drift apart.

Concretely the lesson learned was:

  - `hypothesis-loop` wrote `hypotheses.yaml` as a YAML mapping (one
    top-level key per hypothesis block)
  - `validation-agent` SKILL.md template said to append a YAML list
    item (`- id: H1`)
  - Both individually plausible. Both incompatible. The cross-skill
    append silently produced an unparseable YAML, cascading into
    the validator returning INCONCLUSIVE on
    a file that visually still had selector info.

This package turns the implicit cross-skill contract into an executable
one: any producer that doesn't go through these models cannot write,
and any consumer that doesn't load through them fails loudly at parse
time rather than silently at runtime.

Import surface:

    from mcp_server.schemas import (
        Hypothesis, HypothesesFile,
        SelectorEntry, FieldStabilityEntry, SelectorsFile,
    )
"""

from mcp_server.schemas.hypotheses import (
    Hypothesis,
    HypothesesFile,
    HypothesisStatus,
    HypothesisPriority,
)
from mcp_server.schemas.selectors import (
    SelectorEntry,
    SelectorsBlock,
    FieldStabilityBlock,
    FieldStabilityEntry,
    SelectorsFile,
    SelectorStability,
    FieldStabilityClass,
)
from mcp_server.schemas.iter_summary import (
    IterSummarySection,
    load_latest_section as load_latest_iter_summary,
    next_iter_n as next_iter_summary_n,
    append_section as append_iter_summary_section,
    check_required_subsections as check_iter_summary_subsections,
)
from mcp_server.schemas.scorecard import (
    Dimension,
    ScorecardFile,
    DimensionStatus,
    OverallVerdict,
    new_scorecard,
)
from mcp_server.schemas.download_manifest import (
    DownloadTarget,
    DownloadManifestFile,
)
from mcp_server.schemas.api_manifest import (
    APIEndpointAuth,
    APIEndpoint,
    APIFieldDecl,
    APIManifestFile,
)
from mcp_server.schemas.discovered_sources import (
    DiscoveredSource,
    DiscoveredSourcesFile,
)
from mcp_server.schemas.run_manifest import (
    RunManifestFile,
    RunOutcome,
    new_run_manifest,
)
from mcp_server.schemas.product_manifest import (
    ProductManifestFile,
    ProductOutcome,
    SourceRef,
    ProductEntry,
    new_product_manifest,
)
from mcp_server.schemas.crawl_brief import (
    CrawlBriefFile,
    CompletenessAnchor,
)

__all__ = [
    "Hypothesis",
    "HypothesesFile",
    "HypothesisStatus",
    "HypothesisPriority",
    "SelectorEntry",
    "SelectorsBlock",
    "FieldStabilityBlock",
    "FieldStabilityEntry",
    "SelectorsFile",
    "SelectorStability",
    "FieldStabilityClass",
    "IterSummarySection",
    "load_latest_iter_summary",
    "next_iter_summary_n",
    "append_iter_summary_section",
    "check_iter_summary_subsections",
    "Dimension",
    "ScorecardFile",
    "DimensionStatus",
    "OverallVerdict",
    "new_scorecard",
    "DownloadTarget",
    "DownloadManifestFile",
    "APIEndpointAuth",
    "APIEndpoint",
    "APIFieldDecl",
    "APIManifestFile",
    "DiscoveredSource",
    "DiscoveredSourcesFile",
    "RunManifestFile",
    "RunOutcome",
    "new_run_manifest",
    "ProductManifestFile",
    "ProductOutcome",
    "SourceRef",
    "ProductEntry",
    "new_product_manifest",
    "CrawlBriefFile",
    "CompletenessAnchor",
]
