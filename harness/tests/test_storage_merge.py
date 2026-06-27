"""`_merge_storage_state` — the rule that makes one global auth_state.json
accumulate logins across many sites instead of clobbering on each save.

Pure function, no browser. Each test pins one clause of the docstring contract
in browser.py:357 ("cookies keyed by (name, domain, path); localStorage by
origin; the new login wins on conflict").
"""

from __future__ import annotations

from mcp_server.browser import _merge_storage_state


def _ck(name, domain, value, path="/"):
    return {"name": name, "domain": domain, "path": path, "value": value}


def test_accumulates_across_domains():
    # Logging into site B must NOT wipe site A's cookie — the whole point of
    # the project-global merged auth store.
    old = {"cookies": [_ck("a", "x.com", "1")], "origins": []}
    new = {"cookies": [_ck("b", "y.com", "2")], "origins": []}
    merged = _merge_storage_state(old, new)
    assert {(c["name"], c["domain"]) for c in merged["cookies"]} == {("a", "x.com"), ("b", "y.com")}


def test_same_key_new_login_wins():
    # Re-login to the same site refreshes the value, does not duplicate the row.
    old = {"cookies": [_ck("sid", "x.com", "OLD")], "origins": []}
    new = {"cookies": [_ck("sid", "x.com", "NEW")], "origins": []}
    merged = _merge_storage_state(old, new)
    sids = [c for c in merged["cookies"] if c["name"] == "sid"]
    assert len(sids) == 1
    assert sids[0]["value"] == "NEW"


def test_same_name_domain_different_path_kept_separate():
    # Path is part of the key — /foo and /bar are distinct cookies.
    old = {"cookies": [_ck("a", "x.com", "1", path="/foo")], "origins": []}
    new = {"cookies": [_ck("a", "x.com", "2", path="/bar")], "origins": []}
    merged = _merge_storage_state(old, new)
    assert len(merged["cookies"]) == 2


def test_origins_merged_by_origin_new_wins():
    old = {"cookies": [], "origins": [{"origin": "https://x.com", "localStorage": [{"name": "k", "value": "old"}]}]}
    new = {
        "cookies": [],
        "origins": [
            {"origin": "https://x.com", "localStorage": [{"name": "k", "value": "new"}]},
            {"origin": "https://y.com", "localStorage": []},
        ],
    }
    merged = _merge_storage_state(old, new)
    by_origin = {o["origin"]: o for o in merged["origins"]}
    assert set(by_origin) == {"https://x.com", "https://y.com"}
    assert by_origin["https://x.com"]["localStorage"][0]["value"] == "new"


def test_tolerates_empty_or_missing_sides():
    # save_auth feeds {} when the file is missing/corrupt — must not crash.
    merged = _merge_storage_state({}, {"cookies": [_ck("a", "x.com", "1")], "origins": []})
    assert len(merged["cookies"]) == 1
    assert _merge_storage_state({}, {}) == {"cookies": [], "origins": []}
