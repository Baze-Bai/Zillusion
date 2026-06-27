"""Stage-2 MCP server seams for the `api` workflow type.

- workspace_attach binds the active site WITHOUT starting Chromium
- api_manifest_write validates against APIManifestFile before writing;
  a bad payload leaves an existing manifest untouched
- workspace_write refuses api_manifest.yaml (schema-managed)
- Browser.ensure_started refuses to launch under an api-workflow env
  (the backstop behind disallowed_tools)
"""

from __future__ import annotations

import pytest

from mcp_server import server
from mcp_server.schemas import APIManifestFile

VALID_MANIFEST_YAML = """\
workflow_type: api
source_url: https://api.example.com
identifier_field: id
endpoints:
  - url_template: "https://api.example.com/v1/posts?page={page}"
    probe_url: "https://api.example.com/v1/posts?page=1"
    method: GET
    auth:
      type: api_key
      location: header
      param_name: X-Api-Key
fields:
  - name: id
    semantic: record id
    stability: STRICT
credentials_source: env API_KEY, else credentials.json walk-up
"""


@pytest.fixture
def bound_site():
    prev = server._active_site
    server._active_site = None
    yield "api-site-tools"
    server._active_site = prev


async def test_workspace_attach_binds_without_browser(bound_site):
    res = await server.workspace_attach(bound_site)
    assert res["site_id"] == bound_site
    assert "not started" in res["browser"]
    # No Chromium came up as a side effect.
    assert server._browser.page is None
    # Workspace tools work off the binding.
    listing = await server.workspace_list_samples()
    assert listing is not None


async def test_api_manifest_write_valid(bound_site):
    await server.workspace_attach(bound_site)
    res = await server.api_manifest_write(VALID_MANIFEST_YAML)
    assert res["status"] == "ok"
    assert res["endpoint_count"] == 1
    assert res["field_count"] == 1
    assert res["identifier_field"] == "id"

    ws = server._workspace()
    loaded = APIManifestFile.load(ws.dir / "api_manifest.yaml")
    assert loaded.endpoints[0].probe_url.endswith("page=1")


async def test_api_manifest_write_invalid_leaves_existing(bound_site):
    await server.workspace_attach(bound_site)
    assert (await server.api_manifest_write(VALID_MANIFEST_YAML))["status"] == "ok"

    bad = VALID_MANIFEST_YAML + 'api_key: "sk-LEAKED-should-be-rejected"\n'
    res = await server.api_manifest_write(bad)
    assert res["status"] == "error"
    assert "schema validation failed" in res["error"]

    # The previously-written manifest is untouched and still loads.
    ws = server._workspace()
    loaded = APIManifestFile.load(ws.dir / "api_manifest.yaml")
    assert loaded.source_url == "https://api.example.com"


async def test_api_manifest_write_unparseable_yaml(bound_site):
    await server.workspace_attach(bound_site)
    res = await server.api_manifest_write("endpoints: [unclosed")
    assert res["status"] == "error"
    assert "unparseable" in res["error"]


async def test_workspace_write_refuses_api_manifest(bound_site):
    await server.workspace_attach(bound_site)
    from mcp_server.workspace import WorkspacePathError

    with pytest.raises(WorkspacePathError, match="api_manifest_write"):
        await server.workspace_write("api_manifest.yaml", "workflow_type: api\n")


async def test_browser_backstop_refuses_api_workflow(monkeypatch):
    from mcp_server.browser import Browser

    monkeypatch.setenv("CRAWLER_EXPLORER_WORKFLOW", "api")
    b = Browser(headless=True)
    with pytest.raises(RuntimeError, match="browser disabled"):
        await b.ensure_started()
    # Nothing was launched.
    assert b.page is None and b._browser is None
