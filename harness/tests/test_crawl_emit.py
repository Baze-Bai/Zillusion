"""Agentic Crawl: emit substrate + gate semantics + agent assembly.

Deterministic (no LLM). Exercises the commit/dedup/tombstone/cursor/resume
substrate (``runtime.crawl_emit``), the agentic manifest gate (including the
new ``completeness``-fail → PARTIAL branch and the driver ``abort_crawl``
salvage path), and that ``runtime.crawl_agent`` assembles the right tool
surface (full browser + emit, no Bash/Write/Edit).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from runtime import crawl_emit as ce
from runtime import run_tools as rt


def _rec(i, **extra):
    """A floor-valid record: identifier `id` + a `source_url` provenance handle
    (commit_records now rejects records lacking source_url + a content carrier)."""
    return {"id": i, "source_url": f"https://ex/{i}", **extra}


@pytest.fixture
def fake_ws(tmp_path, monkeypatch):
    """Point both crawl_emit and run_tools at a throwaway workspaces/ root and
    clear in-process crawl state. Returns a (site_id, run_id) factory."""
    ws_root = tmp_path / "workspaces"

    def _ws(site_id: str) -> Path:
        return ws_root / site_id

    monkeypatch.setattr(ce, "_workspace", _ws)
    monkeypatch.setattr(rt, "_workspace", _ws)
    ce._CRAWL_STATES.clear()

    def _make() -> tuple[str, str]:
        return f"site-{uuid.uuid4().hex[:6]}", f"run-{uuid.uuid4().hex[:6]}"

    return _make


# ── substrate ────────────────────────────────────────────────────────


def test_commit_dedup_skip_cursor_finalize_complete(fake_ws):
    site, run = fake_ws()
    ce.init_crawl(
        site, run, identifier_field="id", anchor={"hardness": "hard", "estimated_total": 4}
    )
    assert ce.commit_records(site, run, [_rec(1), _rec(2)])["committed"] == 2
    # batch with one repeat → 1 fresh, 1 dup
    r = ce.commit_records(site, run, [_rec(2), _rec(3)])
    assert r["committed"] == 1 and r["duplicates"] == 1 and r["total_committed"] == 3
    ce.record_skip(site, run, "4", "404")
    ce.mark_cursor(site, run, {"page": 2})

    prog = ce.get_crawl_progress(site, run)
    assert prog["committed"] == 3 and prog["tombstoned"] == 1 and prog["processed"] == 4
    assert prog["progress_ratio"] == 1.0  # 4 processed / 4 estimated

    fin = ce.finalize_crawl(site, run, "pass", "cursor reached full set; 3 committed + 1 skipped")
    assert fin["outcome"] == "complete" and fin["record_count"] == 3
    out = Path(fin["output_path"])
    assert out.exists() and len(json.loads(out.read_text(encoding="utf-8"))) == 3


def test_content_hash_dedup_without_identifier(fake_ws):
    site, run = fake_ws()
    ce.init_crawl(site, run, anchor={"hardness": "soft"})  # no identifier_field
    ce.commit_records(site, run, [{"a": 1, "b": 2, "source_url": "u"}])
    r = ce.commit_records(
        site, run, [{"a": 1, "b": 2, "source_url": "u"}, {"a": 9, "source_url": "u"}]
    )  # first is identical
    assert r["committed"] == 1 and r["duplicates"] == 1


def test_resume_reloads_committed_from_disk(fake_ws):
    site, run = fake_ws()
    ce.init_crawl(site, run, identifier_field="id", anchor={"hardness": "soft"})
    ce.commit_records(site, run, [_rec(1), _rec(2)])
    ce.record_skip(site, run, "x", "blocked")
    ce.mark_cursor(site, run, 7)

    ce._CRAWL_STATES.clear()  # simulate a fresh process (resume)
    res = ce.init_crawl(site, run, identifier_field="id", anchor={"hardness": "soft"})
    assert res["resumed"] and res["already_committed"] == 2 and res["already_tombstoned"] == 1
    assert res["cursor"] == 7
    # re-committing a seen id after resume is a no-op
    assert ce.commit_records(site, run, [_rec(1)])["committed"] == 0


def test_check_committed(fake_ws):
    site, run = fake_ws()
    ce.init_crawl(site, run, identifier_field="id", anchor={"hardness": "soft"})
    ce.commit_records(site, run, [_rec("a"), _rec("b")])
    res = ce.check_committed(site, run, ["a", "b", "c"])
    assert res["already_committed"] == ["a", "b"] and res["remaining"] == ["c"]


# ── gate semantics ───────────────────────────────────────────────────


def test_completeness_fail_with_data_is_partial(fake_ws):
    """Agent finished its loop but didn't reach the anchor (kept data) → PARTIAL,
    not FAILED — the new completeness branch in the manifest gate."""
    site, run = fake_ws()
    ce.init_crawl(
        site, run, identifier_field="id", anchor={"hardness": "hard", "estimated_total": 100}
    )
    ce.commit_records(site, run, [_rec(i) for i in range(8)])
    fin = ce.finalize_crawl(
        site, run, "fail", "only 8/100 reachable; rest behind an unclearable wall"
    )
    assert fin["outcome"] == "partial" and fin["record_count"] == 8


def test_empty_finalize_is_failed(fake_ws):
    """Zero records → produced_output fail (mechanical) → FAILED even if the agent
    claims completeness pass."""
    site, run = fake_ws()
    ce.init_crawl(site, run, anchor={"hardness": "soft"})
    fin = ce.finalize_crawl(site, run, "pass", "nothing matched")
    assert fin["record_count"] == 0 and fin["outcome"] == "failed"


def test_abort_salvages_partial(fake_ws):
    """Driver kill (progress stall / wall clock) with committed data → within_budget
    fail + PARTIAL, salvaging what was scraped."""
    site, run = fake_ws()
    ce.init_crawl(site, run, identifier_field="id", anchor={"hardness": "soft"})
    ce.commit_records(site, run, [_rec(1), _rec(2), _rec(3)])
    res = ce.abort_crawl(site, run, "progress_stall: no new commits for 900s")
    assert res["aborted"] and res["record_count"] == 3 and res["outcome"] == "partial"
    man = rt.read_run_manifest(site, run)
    assert man["dimensions"]["within_budget"]["status"] == "fail"


def test_abort_empty_is_aborted(fake_ws):
    site, run = fake_ws()
    ce.init_crawl(site, run, anchor={"hardness": "soft"})
    res = ce.abort_crawl(site, run, "wall_clock_cap exceeded")
    assert res["record_count"] == 0 and res["outcome"] == "aborted"


# ── item 1: weak-schema commit floor ─────────────────────────────────


def test_commit_rejects_missing_source_url_and_empty_shell(fake_ws):
    site, run = fake_ws()
    ce.init_crawl(site, run, identifier_field="id", anchor={"hardness": "soft"})
    batch = [
        _rec(1),  # valid
        {"id": 2},  # no source_url
        {"source_url": "u3"},  # empty shell (URL only, no carrier)
        "not-a-dict",  # not an object
    ]
    r = ce.commit_records(site, run, batch)
    assert r["committed"] == 1  # only _rec(1) lands
    assert r["rejected"] == 3
    reasons = " ".join(d["reason"] for d in r["rejected_detail"])
    assert "source_url" in reasons and "carrier" in reasons and "JSON object" in reasons


def test_commit_accepts_content_and_file_ref_carriers(fake_ws):
    site, run = fake_ws()
    ce.init_crawl(site, run, anchor={"hardness": "soft"})  # no identifier → content hash
    r = ce.commit_records(
        site,
        run,
        [
            {"source_url": "u1", "content": "a document body"},  # inline content carrier
            {"source_url": "u2", "file_ref": "media/a.bin"},  # file_ref carrier
        ],
    )
    assert r["committed"] == 2 and not r.get("rejected")


# ── item 2: self_consistency gate ────────────────────────────────────


def test_self_consistency_pass_on_clean_run(fake_ws):
    site, run = fake_ws()
    ce.init_crawl(site, run, identifier_field="id", anchor={"hardness": "soft"})
    ce.commit_records(site, run, [_rec(1), _rec(2)])
    ce.finalize_crawl(site, run, "pass", "scrolled to the end")
    man = rt.read_run_manifest(site, run)
    assert man["dimensions"]["self_consistency"]["status"] == "pass"


def test_self_consistency_fails_on_broken_file_ref(fake_ws):
    site, run = fake_ws()
    ce.init_crawl(site, run, identifier_field="id", anchor={"hardness": "soft"})
    # file_ref is a non-empty string (passes the commit floor) but the target
    # never exists on disk → self_consistency fail → outcome failed.
    ce.commit_records(site, run, [{"id": 1, "source_url": "u1", "file_ref": "media/missing.bin"}])
    fin = ce.finalize_crawl(site, run, "pass", "done")
    man = rt.read_run_manifest(site, run)
    assert man["dimensions"]["self_consistency"]["status"] == "fail"
    assert fin["outcome"] == "failed"


# ── item 3: secrets_safe scan + hard floor ───────────────────────────


def _write_auth(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "cookies": [{"name": "sid", "value": token, "domain": "x", "path": "/"}],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )


def test_secrets_safe_na_without_auth(fake_ws, monkeypatch, tmp_path):
    from runtime import secret_scan

    monkeypatch.setattr(
        secret_scan, "resolve_auth_path", lambda: tmp_path / "nope" / "auth_state.json"
    )
    site, run = fake_ws()
    ce.init_crawl(site, run, identifier_field="id", anchor={"hardness": "soft"})
    ce.commit_records(site, run, [_rec(1)])
    fin = ce.finalize_crawl(site, run, "pass", "done")
    man = rt.read_run_manifest(site, run)
    assert man["dimensions"]["secrets_safe"]["status"] == "n/a"
    assert fin["outcome"] == "complete"


def test_secrets_safe_fail_on_leak_and_hard_floor(fake_ws, monkeypatch, tmp_path):
    from runtime import secret_scan

    token = "SUPER_SECRET_SESSION_TOKEN_abcdef123456"
    auth = tmp_path / "auth_state.json"
    _write_auth(auth, token)
    monkeypatch.setattr(secret_scan, "resolve_auth_path", lambda: auth)

    site, run = fake_ws()
    ce.init_crawl(site, run, identifier_field="id", anchor={"hardness": "soft"})
    # the agent accidentally commits the live session token into a record field
    ce.commit_records(site, run, [{"id": 1, "source_url": "u1", "blob": f"x {token} y"}])
    fin = ce.finalize_crawl(site, run, "pass", "done")
    man = rt.read_run_manifest(site, run)
    assert man["dimensions"]["secrets_safe"]["status"] == "fail"
    assert fin["outcome"] == "failed"  # secrets leak overrides everything

    # hard floor: the agent cannot overwrite secrets_safe to pass while the leak
    # is still on disk.
    res = rt.update_run_manifest(site, run, "secrets_safe", "pass", basis="looks fine to me")
    assert res.get("rejected") is True
    assert rt.read_run_manifest(site, run)["dimensions"]["secrets_safe"]["status"] == "fail"


# ── agent assembly (no LLM) ──────────────────────────────────────────


def test_agent_tool_surface():
    from runtime import crawl_agent as ca

    al = ca._allowed_tools()
    # full emit surface + browser drive + Read, NO Bash/Write/Edit
    assert "mcp__crawl__commit_records" in al and "mcp__crawl__finalize_crawl" in al
    assert "mcp__browser-harness__browser_player" in al
    assert "mcp__browser-harness__browser_request_user_login" in al
    assert "Read" in al
    assert not any(t in ("Bash", "Write", "Edit", "NotebookEdit") for t in al)


def test_agent_options_mount_both_servers():
    from runtime import crawl_agent as ca

    root = Path(ca.__file__).resolve().parent.parent
    opts = ca._build_options(
        root, permission_mode="bypassPermissions", max_turns=200, model=None, config={}
    )
    assert "crawl" in opts.mcp_servers and "browser-harness" in opts.mcp_servers
    assert opts.allowed_tools and "mcp__crawl__init_crawl" in opts.allowed_tools


def test_prompt_carries_run_id_and_outcome_contract():
    from runtime import crawl_agent as ca

    p = ca._build_prompt("mysite", "run-z9", True)
    assert "init_crawl" in p and "run_id=run-z9" in p
    assert "[COMPLETE|PARTIAL|FAILED|ABORTED]" in p
