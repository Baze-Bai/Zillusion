"""Unit tests for src.services.run_bundle — multi-file run deliverable zipping."""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

import pytest

from src.services import run_bundle

if TYPE_CHECKING:
    from pathlib import Path


def _mk(run_dir: Path, rel: str, content: bytes = b"x") -> Path:
    p = run_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs" / "run-1"
    d.mkdir(parents=True)
    return d


def test_iter_bundle_files_excludes_machinery(run_dir: Path):
    keep = {
        _mk(run_dir, "output.json"),
        _mk(run_dir, "report.md"),
        _mk(run_dir, "manifest.yaml"),
        _mk(run_dir, "task_plan.md"),
        _mk(run_dir, "media/img.jpg"),
        _mk(run_dir, "downloads/data.csv"),
    }
    # machinery, excluded
    _mk(run_dir, "_run_progress.json")
    _mk(run_dir, "_crawl_meta.json")
    _mk(run_dir, "_agent_messages.jsonl")
    _mk(run_dir, "crawl_stdout.log")
    _mk(run_dir, "feedback.yaml")
    _mk(run_dir, "records.jsonl")
    _mk(run_dir, "output_sample.json")
    _mk(run_dir, "output.json.tmp")
    _mk(run_dir, "_scratch/notes.txt")  # underscore dir, any depth

    assert set(run_bundle.iter_bundle_files(run_dir)) == keep


def test_machinery_names_only_excluded_at_top_level(run_dir: Path):
    # A downloaded file that happens to share a machinery name, in a subdir.
    nested = _mk(run_dir, "downloads/records.jsonl")
    assert nested in run_bundle.iter_bundle_files(run_dir)


def test_duplicates_kept_when_no_canonical_output(run_dir: Path):
    # No output.json: records.jsonl / output_sample.json may be the ONLY copy
    # of the data (salvage fallback, crawl died before materialization) — they
    # must ship, not be dropped as "duplicates" of a file that isn't there.
    sample = _mk(run_dir, "output_sample.json", b"[]")
    records = _mk(run_dir, "records.jsonl", b"{}")
    files = set(run_bundle.iter_bundle_files(run_dir))
    assert sample in files and records in files
    # ...and they count as data for the bundle decision.
    assert run_bundle.should_bundle(run_dir, None) is True


def test_duplicates_excluded_once_canonical_appears(run_dir: Path):
    _mk(run_dir, "output_sample.json", b"[]")
    _mk(run_dir, "records.jsonl", b"{}")
    out = _mk(run_dir, "output.json", b"[]")
    assert set(run_bundle.iter_bundle_files(run_dir)) == {out}


def test_staged_inputs_and_secrets_never_ship(run_dir: Path):
    # REAL run-dir shape: run_exec stages the workflow source + manifests into
    # the run dir (and credentials.json for api runs / auth_state.json for
    # --output-root runs). None of it is user data; secrets must never ship.
    out = _mk(run_dir, "output.json", b"[]")
    for staged in (
        "workflow.py",
        "helpers.py",
        "selectors.yaml",
        "api_manifest.yaml",
        "download_manifest.yaml",
        "credentials.json",
        "auth_state.json",
    ):
        _mk(run_dir, staged)
    _mk(run_dir, "nested/credentials.json")  # secrets excluded at ANY depth
    _mk(run_dir, "media/auth_state.json")

    assert set(run_bundle.iter_bundle_files(run_dir)) == {out}
    # ...and a staged-but-single-output run keeps the direct-link path alive.
    assert run_bundle.should_bundle(run_dir, out) is False


def test_should_bundle_false_for_single_output_with_explainers(run_dir: Path):
    out = _mk(run_dir, "output.json")
    _mk(run_dir, "report.md")
    _mk(run_dir, "manifest.yaml")
    _mk(run_dir, "task_plan.md")
    _mk(run_dir, "_run_progress.json")
    assert run_bundle.should_bundle(run_dir, out) is False


def test_should_bundle_true_with_media_companions(run_dir: Path):
    out = _mk(run_dir, "output.json")
    _mk(run_dir, "media/doc.pdf")
    assert run_bundle.should_bundle(run_dir, out) is True


def test_should_bundle_download_dir(run_dir: Path):
    # download-type: no canonical output file; any data file triggers a bundle.
    _mk(run_dir, "downloads/data.csv")
    _mk(run_dir, "report.md")
    assert run_bundle.should_bundle(run_dir, None) is True


def test_should_bundle_false_for_explainers_only(run_dir: Path):
    _mk(run_dir, "report.md")
    _mk(run_dir, "manifest.yaml")
    assert run_bundle.should_bundle(run_dir, None) is False


def test_build_zip_members_and_compression(run_dir: Path, tmp_path: Path):
    _mk(run_dir, "output.json", b'{"a": 1}')
    _mk(run_dir, "media/img.jpg", b"\xff\xd8\xff")
    _mk(run_dir, "report.md", b"# r")
    _mk(run_dir, "_run_progress.json")

    dest = tmp_path / "bundle.zip"
    n = run_bundle.build_zip(run_dir, dest, "site_run-1")
    assert n == 3

    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
        assert names == {
            "site_run-1/output.json",
            "site_run-1/media/img.jpg",
            "site_run-1/report.md",
        }
        by_name = {i.filename: i for i in zf.infolist()}
        assert by_name["site_run-1/output.json"].compress_type == zipfile.ZIP_DEFLATED
        assert by_name["site_run-1/media/img.jpg"].compress_type == zipfile.ZIP_STORED
        assert zf.read("site_run-1/output.json") == b'{"a": 1}'


def test_create_bundle_scratch_env_and_cleanup(run_dir: Path, tmp_path: Path, monkeypatch):
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("ZILLUSION_SCRATCH_DIR", str(scratch))
    _mk(run_dir, "output.json")
    _mk(run_dir, "media/a.bin")

    out = run_bundle.create_bundle(run_dir, "s_r")
    assert out.parent == scratch
    assert zipfile.ZipFile(out).testzip() is None
    out.unlink()


def test_create_bundle_empty_raises(run_dir: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ZILLUSION_SCRATCH_DIR", str(tmp_path / "scratch2"))
    _mk(run_dir, "_only_machinery.json")
    with pytest.raises(FileNotFoundError):
        run_bundle.create_bundle(run_dir, "s_r")
    assert not list((tmp_path / "scratch2").glob("*.zip"))  # no leftover tmp


def test_total_bytes(run_dir: Path):
    _mk(run_dir, "output.json", b"abcd")
    _mk(run_dir, "media/x.bin", b"123456")
    files = run_bundle.iter_bundle_files(run_dir)
    assert run_bundle.total_bytes(files) == 10
