"""Stage-4 api validator checks — fully offline (local http.server stub).

- probe_api_endpoint: auth header injection, whole-body save under
  validation/, value_match math, secret masking (even when the API echoes
  the key back)
- check_no_secret_leak: na / clean / shipped-artifact leak / advisory leak
- compare_field_names + compare_output: declared fields + stability classes
  come from api_manifest.yaml when there is no selectors.yaml
- run_workflow_isolated: delivers credentials.json into the isolated rerun
  (inputs/<site>/ is not on the rerun's parent chain — the copy is the fix)
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import runtime.validator_api as validator_api
import runtime.validator_checks as validator_checks
from mcp_server.schemas import APIEndpoint, APIEndpointAuth, APIFieldDecl, APIManifestFile

SECRET = "sk-test-superlongsecret-1234567890"

RECORDS = [
    {"id": 101, "title": "hello world record", "score": 42},
    {"id": 102, "title": "second record title", "score": 7},
]


class _Handler(BaseHTTPRequestHandler):
    captured: list[dict] = []
    echo_secret = False

    def do_GET(self):  # noqa: N802 — stdlib naming
        type(self).captured.append({"path": self.path, "headers": dict(self.headers)})
        body = {"data": {"items": RECORDS}}
        if type(self).echo_secret:
            body["echoed_key"] = self.headers.get("X-Api-Key", "")
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-RateLimit-Remaining", "59")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def stub_api():
    _Handler.captured = []
    _Handler.echo_secret = False
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", _Handler
    finally:
        srv.shutdown()


@pytest.fixture
def api_site(tmp_path, monkeypatch):
    """A temp project tree with one api-staged site; PROJECT_ROOT patched in
    both modules (each holds its own imported binding)."""
    monkeypatch.setattr(validator_checks, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(validator_api, "PROJECT_ROOT", tmp_path)
    site = "api-probe-site"
    ws = tmp_path / "workspaces" / site
    ws.mkdir(parents=True)
    (ws / "output_sample.json").write_text(json.dumps(RECORDS), encoding="utf-8")
    inp = tmp_path / "inputs" / site
    inp.mkdir(parents=True)
    (inp / "credentials.json").write_text(json.dumps({"api_key": SECRET}), encoding="utf-8")
    return site, ws, inp


def _write_manifest(ws, base_url: str) -> None:
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
        credentials_source="env API_KEY, else credentials.json walk-up",
    ).save(ws / "api_manifest.yaml")


# ── probe_api_endpoint ───────────────────────────────────────────────


async def test_probe_signs_saves_and_matches(api_site, stub_api):
    site, ws, _ = api_site
    base, handler = stub_api
    _write_manifest(ws, base)

    res = await validator_api.probe_api_endpoint(site, "val-x")

    assert res["status_code"] == 200
    assert handler.captured[0]["headers"].get("X-Api-Key") == SECRET  # auth applied
    assert res["authenticated_as"] != SECRET  # masked in the result
    assert SECRET not in json.dumps(res)
    assert res["records_found"] == 2
    assert res["rate_limit_headers"].get("x-ratelimit-remaining") == "59"  # httpx lowercases
    assert "hello world record" in res["raw_preview"]

    saved = ws / "validation" / "val-x" / "samples" / "probe_ep0.json"
    assert saved.exists()
    assert "second record title" in saved.read_text(encoding="utf-8")  # whole body on disk

    vm = res["value_match"]
    assert vm["output_records_checked"] == 2
    assert vm["identifiers_found_in_response"] == 2
    assert vm["fields_never_found"] == []
    assert all(r == 1.0 for r in vm["per_record_value_hit_ratio"])


async def test_probe_masks_echoed_secret(api_site, stub_api):
    site, ws, _ = api_site
    base, handler = stub_api
    handler.echo_secret = True
    _write_manifest(ws, base)

    res = await validator_api.probe_api_endpoint(site, "val-echo")
    assert SECRET not in res["raw_preview"]
    saved = ws / "validation" / "val-echo" / "samples" / "probe_ep0.json"
    assert SECRET not in saved.read_text(encoding="utf-8")


async def test_probe_value_match_flags_fabricated_field(api_site, stub_api):
    site, ws, _ = api_site
    base, _ = stub_api
    _write_manifest(ws, base)
    fabricated = [dict(r, phantom_field=f"not-in-response-{i}") for i, r in enumerate(RECORDS)]
    (ws / "output_sample.json").write_text(json.dumps(fabricated), encoding="utf-8")

    res = await validator_api.probe_api_endpoint(site, "val-fab")
    assert res["value_match"]["fields_never_found"] == ["phantom_field"]


# ── check_no_secret_leak ─────────────────────────────────────────────


def test_secret_leak_na_without_credentials(api_site):
    site, _, inp = api_site
    (inp / "credentials.json").unlink()
    res = validator_api.check_no_secret_leak(site, "val-x")
    assert res["na"] is True and res["ok"] is True


def test_secret_leak_clean_workspace(api_site):
    site, ws, _ = api_site
    (ws / "workflow.py").write_text(
        "import os\nKEY = os.environ.get('API_KEY')\n", encoding="utf-8"
    )
    res = validator_api.check_no_secret_leak(site, "val-x")
    assert res["ok"] is True and res["na"] is False and res["leaks"] == []


def test_secret_leak_in_shipped_artifact_gates(api_site):
    site, ws, _ = api_site
    (ws / "workflow.py").write_text(f'KEY = "{SECRET}"\n', encoding="utf-8")
    res = validator_api.check_no_secret_leak(site, "val-x")
    assert res["ok"] is False
    assert res["leaks"][0]["file"] == "workflow.py"
    assert SECRET not in json.dumps(res)  # masked everywhere


def test_secret_leak_in_notes_is_advisory(api_site):
    site, ws, _ = api_site
    (ws / "exploration_log.md").write_text(f"probed with {SECRET}\n", encoding="utf-8")
    res = validator_api.check_no_secret_leak(site, "val-x")
    assert res["ok"] is True
    assert res["advisory_leaks"] and res["advisory_leaks"][0]["file"] == "exploration_log.md"


# ── manifest-driven compare_field_names / compare_output ─────────────


def test_compare_field_names_reads_manifest(api_site):
    site, ws, _ = api_site
    _write_manifest(ws, "http://x")
    res = validator_checks.compare_field_names(site)
    assert res["declared"] == ["id", "score", "title"]
    assert res["never_appeared"] == []
    assert res["undeclared"] == []


def test_compare_output_stability_from_manifest(api_site, tmp_path):
    site, ws, _ = api_site
    _write_manifest(ws, "http://x")
    rerun = tmp_path / "rerun_out.json"
    drifted = [dict(RECORDS[0], score=44), RECORDS[1]]  # +2 within TOLERANT ±5
    rerun.write_text(json.dumps(drifted), encoding="utf-8")

    res = validator_checks.compare_output(site, str(rerun), identifier_field="id")
    assert res["matched_by_id"] == 2
    assert res["strict_mismatch_count"] == 0
    assert res["tolerant_warning_count"] == 0
    assert res["unclassified_fields"] == []  # classes came from the manifest


# ── credentials delivery into the isolated rerun ─────────────────────


def test_run_workflow_isolated_delivers_credentials(api_site):
    site, ws, _ = api_site
    (ws / "workflow.py").write_text(
        "import json, pathlib\n"
        "creds = pathlib.Path('credentials.json')\n"
        "found = creds.exists() and bool(json.loads(creds.read_text())['api_key'])\n"
        "pathlib.Path('output_sample.json').write_text(json.dumps([{'ok': found}]))\n"
        "print('creds found:', found)\n",
        encoding="utf-8",
    )
    res = validator_checks.run_workflow_isolated(site, "val-creds", timeout_s=60)
    assert res["exit_code"] == 0
    rerun = ws / "validation" / "val-creds" / "rerun"
    assert (rerun / "credentials.json").exists()
    out = json.loads((rerun / "output_sample.json").read_text(encoding="utf-8"))
    assert out == [{"ok": True}]
