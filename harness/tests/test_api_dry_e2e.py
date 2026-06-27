"""Dry end-to-end of the api validation chain — no LLM, no real network.

A hand-written fixture site (api_spec.json + credentials.json + workflow.py +
api_manifest.yaml, pointed at a local http.server stub) is driven through the
SAME deterministic functions the api validator agent calls, in the prompt's
suggested order, and the scorecard gate is computed at the end:

    run_workflow_isolated → compare_field_names → compare_output →
    probe_api_endpoint → check_no_secret_leak → new_scorecard("api") gate

A clean fixture must gate `pass`; a planted credential leak must gate `fail`.
A networked variant (`-m network`) points the same fixture at
jsonplaceholder.typicode.com — free, keyless, stable.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import runtime.validator_api as validator_api
import runtime.validator_checks as validator_checks
from mcp_server.schemas import APIEndpoint, APIEndpointAuth, APIFieldDecl, APIManifestFile
from mcp_server.schemas.scorecard import new_scorecard

SECRET = "sk-dry-e2e-secret-0123456789abcdef"

RECORDS = [
    {"id": 1, "title": "alpha record body", "score": 10},
    {"id": 2, "title": "beta record body", "score": 20},
    {"id": 3, "title": "gamma record body", "score": 30},
]

WORKFLOW = """\
import json, os, pathlib

def _find_credentials():
    if os.environ.get("API_KEY"):
        return {"api_key": os.environ["API_KEY"], "extra": {}}
    here = pathlib.Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        cand = parent / "credentials.json"
        if cand.exists():
            return json.loads(cand.read_text(encoding="utf-8"))
    return {}

import urllib.request

CRAWL_MODE = os.environ.get("CRAWL_MODE", "sample")
creds = _find_credentials()
req = urllib.request.Request(BASE_URL + "/v1/posts?page=1")
if creds.get("api_key"):
    req.add_header("X-Api-Key", creds["api_key"])
with urllib.request.urlopen(req, timeout=30) as r:
    data = json.loads(r.read().decode("utf-8"))
records = data["data"]["items"]
print(f"fetched {len(records)} records (mode={CRAWL_MODE})")
pathlib.Path("output_sample.json").write_text(
    json.dumps(records, ensure_ascii=False), encoding="utf-8"
)
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        payload = json.dumps({"data": {"items": RECORDS}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


@pytest.fixture
def stub_api():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def _stage_fixture_site(tmp_path, base_url: str) -> str:
    """Hand-author what explore would have produced for a tiny keyed API."""
    site = "api-dry-e2e"
    inp = tmp_path / "inputs" / site
    inp.mkdir(parents=True)
    (inp / "api_spec.json").write_text(json.dumps({"workflow_type": "api"}), encoding="utf-8")
    (inp / "credentials.json").write_text(json.dumps({"api_key": SECRET}), encoding="utf-8")

    ws = tmp_path / "workspaces" / site
    ws.mkdir(parents=True)
    (ws / "workflow.py").write_text(f'BASE_URL = "{base_url}"\n' + WORKFLOW, encoding="utf-8")
    APIManifestFile(
        source_url=base_url,
        identifier_field="id",
        endpoints=[
            APIEndpoint(
                url_template=base_url + "/v1/posts?page={page}",
                probe_url=base_url + "/v1/posts?page=1",
                auth=APIEndpointAuth(type="api_key", location="header", param_name="X-Api-Key"),
                record_path="data.items",
            )
        ],
        fields=[
            APIFieldDecl(name="id", stability="STRICT"),
            APIFieldDecl(name="title", stability="STRICT"),
            APIFieldDecl(name="score", stability="TOLERANT"),
        ],
        pagination="single page in this fixture",
        credentials_source="env API_KEY, else credentials.json walk-up",
    ).save(ws / "api_manifest.yaml")
    (ws / "output_sample.json").write_text(json.dumps(RECORDS), encoding="utf-8")
    return site


async def _run_chain(site: str, run_id: str) -> tuple[dict, "object"]:
    """The api validator's suggested spine, deterministically."""
    facts: dict = {}
    facts["syntax"] = validator_checks.check_workflow_syntax(site)
    facts["run"] = validator_checks.run_workflow_isolated(site, run_id, timeout_s=120)
    iso = facts["run"]["isolated_output"]
    facts["fields"] = validator_checks.compare_field_names(site, iso)
    facts["output"] = validator_checks.compare_output(site, iso, identifier_field="id")
    facts["probe"] = await validator_api.probe_api_endpoint(site, run_id)
    facts["secrets"] = validator_api.check_no_secret_leak(site, run_id)

    sc = new_scorecard(run_id, site, "api")
    sc.set_dimension("syntax", "pass" if facts["syntax"]["ok"] else "fail", evidence="ast")
    sc.set_dimension(
        "runs_clean", "pass" if facts["run"]["exit_code"] == 0 else "fail", evidence="rerun"
    )
    sc.set_dimension(
        "self_consistency",
        "pass"
        if not facts["fields"]["never_appeared"] and not facts["fields"]["undeclared"]
        else "fail",
        evidence="declared==produced",
    )
    sc.set_dimension(
        "reproducibility",
        "pass" if facts["output"]["strict_mismatch_count"] == 0 else "fail",
        evidence="cold rerun",
    )
    vm = facts["probe"]["value_match"]
    sc.set_dimension(
        "endpoint_match",
        "pass" if facts["probe"]["status_code"] == 200 and not vm["fields_never_found"] else "fail",
        evidence="live probe",
    )
    s = facts["secrets"]
    sc.set_dimension(
        "secrets_safe",
        "n/a" if s["na"] else ("pass" if s["ok"] else "fail"),
        evidence="leak scan",
    )
    return facts, sc


@pytest.fixture
def patched_root(tmp_path, monkeypatch):
    monkeypatch.setattr(validator_checks, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(validator_api, "PROJECT_ROOT", tmp_path)
    return tmp_path


async def test_dry_e2e_clean_fixture_gates_pass(patched_root, stub_api):
    site = _stage_fixture_site(patched_root, stub_api)
    facts, sc = await _run_chain(site, "val-dry")
    assert facts["run"]["exit_code"] == 0
    assert sc.overall == "pass", {n: d.status for n, d in sc.dimensions.items()}
    # detection agrees this is an api workspace
    from runtime.validate import _detect_workflow_type

    assert _detect_workflow_type(site, patched_root) == "api"


async def test_dry_e2e_planted_leak_gates_fail(patched_root, stub_api):
    site = _stage_fixture_site(patched_root, stub_api)
    ws = patched_root / "workspaces" / site
    wf = ws / "workflow.py"
    wf.write_text(
        wf.read_text(encoding="utf-8").replace(
            "creds = _find_credentials()", f'creds = {{"api_key": "{SECRET}"}}  # hardcoded!'
        ),
        encoding="utf-8",
    )
    _, sc = await _run_chain(site, "val-leak")
    assert sc.dimensions["secrets_safe"].status == "fail"
    assert sc.overall == "fail"


@pytest.mark.network
async def test_live_smoke_jsonplaceholder(patched_root):
    """Keyless live smoke against jsonplaceholder (flat list, id identifier)."""
    site = "api-live-smoke"
    inp = patched_root / "inputs" / site
    inp.mkdir(parents=True)
    (inp / "api_spec.json").write_text(json.dumps({"workflow_type": "api"}), encoding="utf-8")
    ws = patched_root / "workspaces" / site
    ws.mkdir(parents=True)
    APIManifestFile(
        source_url="https://jsonplaceholder.typicode.com",
        identifier_field="id",
        endpoints=[
            APIEndpoint(
                url_template="https://jsonplaceholder.typicode.com/posts?_page={page}&_limit={n}",
                probe_url="https://jsonplaceholder.typicode.com/posts?_page=1&_limit=5",
                auth=None,
                record_path=None,  # root IS the list — exercises the _unwrap fallback
            )
        ],
        fields=[APIFieldDecl(name="id"), APIFieldDecl(name="title")],
        credentials_source="none (open API)",
    ).save(ws / "api_manifest.yaml")
    import httpx

    live = httpx.get(
        "https://jsonplaceholder.typicode.com/posts?_page=1&_limit=3", timeout=30
    ).json()
    (ws / "output_sample.json").write_text(json.dumps(live), encoding="utf-8")

    res = await validator_api.probe_api_endpoint(site, "val-live")
    assert res["status_code"] == 200
    assert res["records_found"] == 5
    assert res["authenticated_as"] is None
    vm = res["value_match"]
    assert vm["identifiers_found_in_response"] == vm["output_records_checked"] == 3
    assert vm["fields_never_found"] == []
    secrets = validator_api.check_no_secret_leak(site, "val-live")
    assert secrets["na"] is True  # open API → secrets_safe records n/a
