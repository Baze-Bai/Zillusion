"""Weak-schema support in the deterministic validator checks (2026-06-11):

  - verify_file_refs        — file_ref targets exist + non-empty, contract
                              violations (absolute / escaping) flagged
  - _within_tolerance       — TOLERANT string branch (length/prefix drift)
  - _clip                   — evidence values bounded for tool results
  - compare_output          — clipped mismatch values + source_url fallback
  - run_workflow_isolated   — produced_files listing (staged inputs / control
                              files excluded)

All pure filesystem + subprocess(sys.executable) — no SDK, no playwright.
"""

from __future__ import annotations

import json

import pytest

from runtime import validator_checks as vc


@pytest.fixture
def site(tmp_path, monkeypatch):
    """An isolated PROJECT_ROOT with one workspace; returns (site_id, ws)."""
    monkeypatch.setattr(vc, "PROJECT_ROOT", tmp_path)
    site_id = "weak-schema-site"
    ws = tmp_path / "workspaces" / site_id
    ws.mkdir(parents=True)
    return site_id, ws


def _write_records(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


# ── _within_tolerance: string branch ─────────────────────────────────


def test_tolerance_equal_strings():
    assert vc._within_tolerance("same", "same")


def test_tolerance_long_text_small_drift_within():
    head = "x" * vc._STR_PREFIX
    a = head + "tail " * 200
    b = head + "tail " * 200 + "appended bit"  # ~1% longer, same opening
    assert vc._within_tolerance(a, b)


def test_tolerance_long_text_different_opening_fails():
    a = "A" * vc._STR_PREFIX + "y" * 1000
    b = "B" * vc._STR_PREFIX + "y" * 1000  # same length, different head
    assert not vc._within_tolerance(a, b)


def test_tolerance_long_text_big_length_drift_fails():
    head = "x" * vc._STR_PREFIX
    a = head + "y" * 1000
    b = head + "y" * 2000  # ~50% longer
    assert not vc._within_tolerance(a, b)


def test_tolerance_short_strings_stay_exact():
    # prefix == whole string for short values → degrades to equality
    assert not vc._within_tolerance("hello", "hella")


def test_tolerance_numeric_strings_keep_numeric_path():
    assert vc._within_tolerance("1164", "1168")  # within ±5
    assert not vc._within_tolerance("1164", "2400")


# ── _clip ────────────────────────────────────────────────────────────


def test_clip_short_values_pass_through():
    assert vc._clip("short") == "short"
    assert vc._clip(42) == 42
    assert vc._clip({"a": 1}) == {"a": 1}


def test_clip_long_string_bounded_with_marker():
    v = "z" * (vc._CLIP_CHARS + 500)
    out = vc._clip(v)
    assert out.startswith("z" * vc._CLIP_CHARS)
    assert "+500 chars" in out
    assert len(out) < vc._CLIP_CHARS + 40


def test_clip_big_container_degrades_to_repr():
    v = [{"k": "v" * 50} for _ in range(20)]
    out = vc._clip(v)
    assert isinstance(out, str) and "chars)" in out


# ── verify_file_refs ─────────────────────────────────────────────────


def test_file_refs_not_applicable(site):
    site_id, ws = site
    _write_records(ws / "output_sample.json", [{"source_url": "https://x", "content": "inline"}])
    res = vc.verify_file_refs(site_id)
    assert res["applicable"] is False
    assert res["records"] == 1


def test_file_refs_buckets(site):
    site_id, ws = site
    (ws / "media").mkdir()
    (ws / "media" / "good.pdf").write_bytes(b"%PDF-1.7 data")
    (ws / "media" / "hollow.bin").write_bytes(b"")
    _write_records(
        ws / "output_sample.json",
        [
            {"source_url": "https://x/1", "file_ref": "media/good.pdf"},
            {"source_url": "https://x/2", "file_ref": "media/gone.pdf"},
            {"source_url": "https://x/3", "file_ref": "media/hollow.bin"},
            {"source_url": "https://x/4", "file_ref": "C:/evil/abs.bin"},
            {"source_url": "https://x/5", "file_ref": "../../escape.bin"},
            {"source_url": "https://x/6", "file_ref": ""},
            {"source_url": "https://x/7", "content": "inline only — no ref, legal"},
        ],
    )
    res = vc.verify_file_refs(site_id)
    assert res["applicable"] is True
    assert res["refs_total"] == 6
    assert res["refs_ok"] == 1
    assert res["all_ok"] is False
    assert [m["file_ref"] for m in res["missing"]] == ["media/gone.pdf"]
    assert [e["file_ref"] for e in res["empty"]] == ["media/hollow.bin"]
    issues = {v["file_ref"]: v["issue"] for v in res["violations"]}
    assert "absolute path" in issues["C:/evil/abs.bin"]
    assert "escapes" in issues["../../escape.bin"]
    assert "non-empty string" in issues[""]


def test_file_refs_resolve_against_rerun_output(site):
    site_id, ws = site
    rerun = ws / "validation" / "val-1" / "rerun"
    (rerun / "media").mkdir(parents=True)
    (rerun / "media" / "doc.txt").write_text("body", encoding="utf-8")
    _write_records(
        rerun / "output_sample.json",
        [{"source_url": "https://x", "file_ref": "media/doc.txt"}],
    )
    # Same ref is MISSING relative to explore's root — base must be the
    # output file's own directory, so pointing at the rerun output passes.
    res = vc.verify_file_refs(site_id, output_path=str(rerun / "output_sample.json"))
    assert res["all_ok"] is True
    assert res["base_dir"] == str(rerun.resolve())


def test_file_refs_bad_output(site):
    site_id, _ = site
    assert "error" in vc.verify_file_refs(site_id)


# ── compare_output: clipping + identifier fallback ───────────────────


def _selectors_yaml(ws, stability: dict[str, str]):
    doc = {
        "selectors": {
            "observed_at": "2026-06-11T00:00:00Z",
            "source_url": "https://x",
            "records_observed": 1,
            "record_locator": "article",
            "fields": {name: {"selector": "article", "extraction": "text"} for name in stability},
            "extract_js": "() => []",
        },
        "field_stability": {
            "observed_at": "2026-06-11T00:00:00Z",
            "source_url": "https://x",
            "probe_count": 2,
            "fields": {
                name: {"drifted": False, "suggested_class": cls} for name, cls in stability.items()
            },
        },
    }
    import yaml

    (ws / "selectors.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def test_compare_output_clips_and_falls_back_to_source_url(site):
    site_id, ws = site
    _selectors_yaml(ws, {"content": "STRICT", "source_url": "SKIP"})
    long_a = "same-head " * 100 + "AAAA" * 200
    long_b = "same-head " * 100 + "BBBB" * 200
    _write_records(
        ws / "output_sample.json",
        [{"source_url": "https://x/1", "content": long_a}],
    )
    rerun_out = ws / "validation" / "val-1" / "rerun" / "output_sample.json"
    _write_records(rerun_out, [{"source_url": "https://x/1", "content": long_b}])

    res = vc.compare_output(site_id, str(rerun_out))  # default identifier: post_url
    assert res["identifier_field"] == "source_url"
    assert res["identifier_requested"] == "post_url"
    assert res["identifier_fallback"] is True
    assert res["matched_by_id"] == 1
    assert res["strict_mismatch_count"] == 1
    mm = res["strict_mismatches"][0]
    assert len(mm["explore"]) < vc._CLIP_CHARS + 40
    assert "chars)" in mm["explore"] and "chars)" in mm["rerun"]


def test_compare_output_no_fallback_when_requested_id_present(site):
    site_id, ws = site
    _selectors_yaml(ws, {"title": "STRICT"})
    recs = [{"post_url": "https://x/p1", "source_url": "https://x/1", "title": "t"}]
    _write_records(ws / "output_sample.json", recs)
    rerun_out = ws / "validation" / "val-2" / "rerun" / "output_sample.json"
    _write_records(rerun_out, recs)
    res = vc.compare_output(site_id, str(rerun_out))
    assert res["identifier_field"] == "post_url"
    assert res["identifier_fallback"] is False
    assert res["strict_mismatch_count"] == 0


# ── run_workflow_isolated: produced_files ────────────────────────────


def test_produced_files_listing(site):
    site_id, ws = site
    (ws / "workflow.py").write_text(
        "import json, pathlib\n"
        "out = pathlib.Path('media'); out.mkdir(exist_ok=True)\n"
        "(out / 'a.bin').write_bytes(b'xx')\n"
        "(out / 'b.tmp').write_bytes(b'partial')\n"
        "pathlib.Path('_last_run_status.json').write_text('{}')\n"
        "pathlib.Path('output_sample.json').write_text(json.dumps([{'source_url': 'u'}]))\n",
        encoding="utf-8",
    )
    (ws / "helpers.py").write_text("# staged input\n", encoding="utf-8")
    res = vc.run_workflow_isolated(site_id, "val-3", timeout_s=60)
    assert res["exit_code"] == 0
    paths = [f["path"] for f in res["produced_files"]]
    assert "media/a.bin" in paths
    assert "output_sample.json" in paths
    assert "media/b.tmp" not in paths  # *.tmp partial
    assert "_last_run_status.json" not in paths  # control file
    assert "workflow.py" not in paths and "helpers.py" not in paths  # staged inputs
    assert res["produced_files_total"] == len(paths)
    assert res["produced_files_truncated"] is False
