"""Stage-1 plumbing for the `api` workflow type.

Covers the three additive seams with no behavior change for existing types:
  - APIManifestFile round-trip + schema strictness (extra=forbid)
  - workflow-type detection precedence (api > download > input-fallback > extraction)
  - gate math for the api dimension catalogs (scorecard + run manifest)
"""

from __future__ import annotations

import pytest
import yaml

from mcp_server.schemas import APIEndpoint, APIEndpointAuth, APIFieldDecl, APIManifestFile
from mcp_server.schemas.run_manifest import new_run_manifest
from mcp_server.schemas.scorecard import new_scorecard


def _manifest() -> APIManifestFile:
    return APIManifestFile(
        source_url="https://api.example.com",
        docs_url="https://docs.example.com",
        identifier_field="id",
        endpoints=[
            APIEndpoint(
                url_template="https://api.example.com/v1/posts?page={page}",
                probe_url="https://api.example.com/v1/posts?page=1",
                method="GET",
                params={"page": "1-based page index"},
                auth=APIEndpointAuth(type="api_key", location="header", param_name="X-Api-Key"),
                record_path="data.items",
            )
        ],
        fields=[
            APIFieldDecl(name="id", semantic="record id", stability="STRICT"),
            APIFieldDecl(name="title", semantic="post title", stability="TOLERANT"),
        ],
        pagination="page param; stop on empty",
        rate_limit="60 req/min observed",
        credentials_source="env API_KEY, else credentials.json walk-up (key: api_key)",
    )


# ── APIManifestFile round-trip + strictness ──────────────────────────


def test_api_manifest_roundtrip(tmp_path):
    m = _manifest()
    path = tmp_path / "api_manifest.yaml"
    m.save(path)

    text = path.read_text(encoding="utf-8")
    assert text.startswith("#")  # managed-file header survives
    loaded = APIManifestFile.load(path)
    assert loaded == m
    assert loaded.workflow_type == "api"
    assert loaded.endpoints[0].auth is not None
    assert loaded.endpoints[0].auth.param_name == "X-Api-Key"


def test_api_manifest_missing_file_is_loud(tmp_path):
    with pytest.raises(FileNotFoundError, match="api_manifest_write"):
        APIManifestFile.load(tmp_path / "api_manifest.yaml")


def test_api_manifest_rejects_extra_keys(tmp_path):
    data = yaml.safe_load(yaml.safe_dump(_manifest().model_dump()))
    data["api_key"] = "sk-should-never-be-here"
    path = tmp_path / "api_manifest.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(Exception):  # pydantic ValidationError via extra=forbid
        APIManifestFile.load(path)


def test_api_manifest_requires_endpoint(tmp_path):
    data = _manifest().model_dump()
    data["endpoints"] = []
    path = tmp_path / "api_manifest.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(Exception):
        APIManifestFile.load(path)


def test_api_manifest_unparseable_yaml_is_loud(tmp_path):
    path = tmp_path / "api_manifest.yaml"
    path.write_text("workflow_type: api\n  bad: [indent", encoding="utf-8")
    with pytest.raises(ValueError, match="unparseable"):
        APIManifestFile.load(path)


# ── workflow-type detection precedence ───────────────────────────────


def _site(tmp_path, *, api=False, download=False, selectors=False, api_spec=False, site="s1"):
    ws = tmp_path / "workspaces" / site
    ws.mkdir(parents=True, exist_ok=True)
    if api:
        (ws / "api_manifest.yaml").write_text("workflow_type: api\n", encoding="utf-8")
    if download:
        (ws / "download_manifest.yaml").write_text("workflow_type: download\n", encoding="utf-8")
    if selectors:
        (ws / "selectors.yaml").write_text("selectors: {}\n", encoding="utf-8")
    if api_spec:
        inp = tmp_path / "inputs" / site
        inp.mkdir(parents=True, exist_ok=True)
        (inp / "api_spec.json").write_text("{}", encoding="utf-8")
    return site


DETECTION_MATRIX = [
    # (api_manifest, download_manifest, selectors, input api_spec) -> expected
    ((True, False, False, False), "api"),
    ((False, True, False, False), "download"),
    ((True, True, False, False), "api"),  # api wins over download
    ((False, False, False, True), "api"),  # input-side fallback
    ((False, False, True, True), "api"),  # lane discipline: stray selectors.yaml doesn't escape api
    ((False, True, False, True), "download"),  # api→download pivot is allowed; artifact wins
    ((False, False, True, False), "extraction"),
    ((False, False, False, False), "extraction"),
]


@pytest.mark.parametrize("flags,expected", DETECTION_MATRIX)
def test_validate_detect_workflow_type(tmp_path, flags, expected):
    from runtime.validate import _detect_workflow_type

    api, download, selectors, api_spec = flags
    site = _site(tmp_path, api=api, download=download, selectors=selectors, api_spec=api_spec)
    assert _detect_workflow_type(site, tmp_path) == expected


@pytest.mark.parametrize("flags,expected", DETECTION_MATRIX)
def test_run_exec_detect_workflow_type(tmp_path, monkeypatch, flags, expected):
    import runtime.run_exec as run_exec
    import runtime.validator_checks as validator_checks

    # detect_workflow_type resolves the workspace via validator_checks._workspace
    # (validator_checks.PROJECT_ROOT) and the input fallback via its own imported
    # PROJECT_ROOT copy — patch both so they agree on the temp tree.
    monkeypatch.setattr(validator_checks, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(run_exec, "PROJECT_ROOT", tmp_path)

    api, download, selectors, api_spec = flags
    site = _site(tmp_path, api=api, download=download, selectors=selectors, api_spec=api_spec)
    assert run_exec.detect_workflow_type(site) == expected


# ── gate math: scorecard (validation) ────────────────────────────────

API_GATING = [
    "syntax",
    "runs_clean",
    "self_consistency",
    "reproducibility",
    "endpoint_match",
    "secrets_safe",
]


def test_api_scorecard_catalog_shape():
    sc = new_scorecard("r1", "s1", "api")
    gating = [n for n, d in sc.dimensions.items() if d.gating]
    advisory = [n for n, d in sc.dimensions.items() if not d.gating]
    assert gating == API_GATING
    assert advisory == ["field_reasonableness", "completeness"]


def test_api_scorecard_all_pass_gates_pass():
    sc = new_scorecard("r1", "s1", "api")
    for n in API_GATING:
        sc.set_dimension(n, "pass", evidence="x")
    assert sc.overall == "pass"


def test_api_scorecard_secret_leak_gates_fail():
    sc = new_scorecard("r1", "s1", "api")
    for n in API_GATING:
        sc.set_dimension(n, "pass", evidence="x")
    sc.set_dimension("secrets_safe", "fail", evidence="api_key literal in workflow.py")
    assert sc.overall == "fail"


def test_api_scorecard_secrets_na_still_passes():
    # Open API, no credentials.json -> secrets_safe is n/a, gate treats it as pass.
    sc = new_scorecard("r1", "s1", "api")
    for n in API_GATING[:-1]:
        sc.set_dimension(n, "pass", evidence="x")
    sc.set_dimension("secrets_safe", "n/a", evidence="no credentials.json")
    assert sc.overall == "pass"


def test_api_scorecard_unverified_gate_is_inconclusive():
    sc = new_scorecard("r1", "s1", "api")
    for n in API_GATING[:-1]:
        sc.set_dimension(n, "pass", evidence="x")
    assert sc.overall == "inconclusive"  # secrets_safe still not_verified


# ── gate math: run manifest (production run) ─────────────────────────


def test_api_run_manifest_mirrors_extraction_catalog():
    api = new_run_manifest("r1", "s1", "api")
    extraction = new_run_manifest("r1", "s1", "extraction")
    assert list(api.dimensions) == list(extraction.dimensions)
    assert api.workflow_type == "api"


def test_api_run_manifest_outcome_gate():
    m = new_run_manifest("r1", "s1", "api")
    for n, d in m.dimensions.items():
        if d.gating:
            m.set_dimension(n, "pass", evidence="x")
    assert m.outcome == "complete"
    m.set_dimension("within_budget", "fail", evidence="stall kill")
    assert m.outcome == "partial"  # output kept -> partial, not aborted
