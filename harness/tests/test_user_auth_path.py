"""`_user_auth_path` — per-end-user cookie store resolution.

Different users have different login accounts, so each gets its OWN
auth_state.json (no cross-tenant cookie collision / leakage). The path is
keyed by CRAWLER_EXPLORER_USER_ID (forwarded by the orchestrator) and rides
the bind-mounted workspaces/ tree so it persists under the docker sandbox.
"""

from __future__ import annotations

from mcp_server import server


def test_per_user_path(monkeypatch):
    monkeypatch.setenv("CRAWLER_EXPLORER_USER_ID", "user-abc-123")
    p = server._user_auth_path()
    assert p == server._AUTH_BY_USER_ROOT / "user-abc-123" / "auth_state.json"


def test_distinct_users_get_distinct_files(monkeypatch):
    monkeypatch.setenv("CRAWLER_EXPLORER_USER_ID", "alice")
    a = server._user_auth_path()
    monkeypatch.setenv("CRAWLER_EXPLORER_USER_ID", "bob")
    b = server._user_auth_path()
    assert a != b
    assert a.parent.name == "alice" and b.parent.name == "bob"


def test_no_user_falls_back_to_shared(monkeypatch):
    monkeypatch.delenv("CRAWLER_EXPLORER_USER_ID", raising=False)
    assert server._user_auth_path() == server.GLOBAL_AUTH
    # blank / whitespace id is treated as "no user" too
    monkeypatch.setenv("CRAWLER_EXPLORER_USER_ID", "   ")
    assert server._user_auth_path() == server.GLOBAL_AUTH


def test_hostile_id_cannot_escape_auth_root(monkeypatch):
    monkeypatch.setenv("CRAWLER_EXPLORER_USER_ID", "../../evil id/x")
    p = server._user_auth_path()
    # exactly one sanitized segment under _auth — no traversal, no separators
    assert p.parent.parent == server._AUTH_BY_USER_ROOT
    seg = p.parent.name
    assert ".." not in seg and "/" not in seg and "\\" not in seg and " " not in seg
    assert server._AUTH_BY_USER_ROOT in p.parents
