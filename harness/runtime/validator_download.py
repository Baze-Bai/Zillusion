"""Deterministic validator checks for DOWNLOAD workflows — file data sources.

The download analog of the record-oriented checks in ``validator_checks``.
After the download workflow.py is re-run in isolation (reuse
``validator_checks.run_workflow_isolated`` — it copies workflow.py + helpers.py
and runs in ``validation/<run_id>/rerun/``, so files land in
``rerun/<output_dir>/``), this verifies each file the workflow DECLARED in
``download_manifest.yaml`` against the actual bytes on disk:

  - file_downloaded -> the declared file exists in the rerun output dir
  - non_empty       -> size >= declared min_bytes
  - format_valid    -> magic bytes / extension match the declared format
  - parses          -> the format's parser loads it (csv/json/xml stdlib;
                       xlsx/parquet best-effort if the dep is installed, else
                       reported as not-verifiable rather than pass/fail)

Pure logic + filesystem; the @tool wrapper lives in ``runtime.validator_tools``.
"""

from __future__ import annotations

from pathlib import Path

from runtime.validator_checks import _workspace


# Leading magic bytes for binary/container formats.
_MAGIC: dict[str, bytes] = {
    "xlsx": b"PK\x03\x04",
    "xls": b"\xd0\xcf\x11\xe0",
    "zip": b"PK\x03\x04",
    "parquet": b"PAR1",
    "gzip": b"\x1f\x8b",
    "gz": b"\x1f\x8b",
}


def _check_format_and_parse(path: Path, fmt: str) -> tuple[bool, bool | None, str]:
    """Return (format_valid, parses, detail).

    ``parses`` is True/False, or None when the format can't be deeply parsed
    here (e.g. parquet without pyarrow) — the validator records None as
    not_verified rather than a pass/fail.
    """
    fmt = (fmt or "").lower().lstrip(".")
    try:
        head = path.read_bytes()[:16]
    except OSError as exc:
        return False, False, f"unreadable: {exc}"

    # ── format_valid (cheap, by magic bytes / shape) ──
    if fmt in _MAGIC:
        fmt_ok = head.startswith(_MAGIC[fmt])
    elif fmt in ("csv", "tsv", "txt"):
        fmt_ok = b"\x00" not in head  # text, not a binary blob
    elif fmt == "json":
        fmt_ok = head.lstrip()[:1] in (b"{", b"[")
    elif fmt in ("xml", "html"):
        fmt_ok = head.lstrip()[:1] == b"<"
    else:
        # Unknown format token — don't fail on format alone.
        fmt_ok = True

    # ── parses (format-specific, best-effort) ──
    try:
        if fmt == "json":
            import json as _json

            _json.loads(path.read_text(encoding="utf-8-sig"))
            return fmt_ok, True, "json.loads ok"
        if fmt in ("csv", "tsv"):
            import csv

            delim = "\t" if fmt == "tsv" else ","
            with path.open(encoding="utf-8-sig", newline="") as fh:
                rows = sum(1 for _, _ in zip(csv.reader(fh, delimiter=delim), range(3)))
            return fmt_ok, rows >= 1, f"csv: read {rows} row(s)"
        if fmt in ("xml", "html"):
            import xml.etree.ElementTree as ET

            ET.parse(path)
            return fmt_ok, True, "xml parse ok"
        if fmt == "xlsx":
            try:
                import openpyxl  # type: ignore

                openpyxl.load_workbook(path, read_only=True)
                return fmt_ok, True, "openpyxl load ok"
            except ImportError:
                import zipfile

                return fmt_ok, (True if zipfile.is_zipfile(path) else False), "no openpyxl; zip-structure check"
        if fmt == "parquet":
            try:
                import pyarrow.parquet as pq  # type: ignore

                pq.read_metadata(path)
                return fmt_ok, True, "pyarrow metadata ok"
            except ImportError:
                return fmt_ok, None, "no pyarrow; format checked by PAR1 magic only"
        return fmt_ok, None, f"format '{fmt}' not deeply parsed (no stdlib parser)"
    except Exception as exc:  # noqa: BLE001 — any parse failure is a real signal
        return fmt_ok, False, f"parse failed: {type(exc).__name__}: {exc}"


def verify_downloaded_files(site_id: str, run_id: str) -> dict:
    """Verify the files declared in download_manifest.yaml against the bytes the
    workflow actually wrote into the ISOLATED rerun output dir.

    Run ``run_workflow_isolated(site_id, run_id)`` first so the files exist in
    ``validation/<run_id>/rerun/<output_dir>/``. Returns per-file results +
    an aggregate the validator maps onto the download scorecard dimensions
    (file_downloaded / non_empty / format_valid / parses).
    """
    from mcp_server.schemas import DownloadManifestFile

    ws = _workspace(site_id)
    manifest = DownloadManifestFile.load(ws / "download_manifest.yaml")  # raises if missing
    out_dir = ws / "validation" / run_id / "rerun" / manifest.output_dir

    results: list[dict] = []
    for t in manifest.files:
        fp = out_dir / t.filename
        rec: dict = {"filename": t.filename, "declared_format": t.file_format, "exists": fp.exists()}
        if fp.exists():
            size = fp.stat().st_size
            fmt_ok, parses, detail = _check_format_and_parse(fp, t.file_format)
            rec.update(
                {
                    "bytes": size,
                    "min_bytes": t.min_bytes,
                    "non_empty": size >= max(0, t.min_bytes),
                    "format_valid": fmt_ok,
                    "parses": parses,
                    "parse_detail": detail,
                }
            )
        else:
            rec.update(
                {
                    "bytes": 0,
                    "min_bytes": t.min_bytes,
                    "non_empty": False,
                    "format_valid": False,
                    "parses": False,
                    "parse_detail": "file not found in rerun output dir",
                }
            )
        results.append(rec)

    present = [r for r in results if r["exists"]]
    aggregate = {
        "files_declared": len(results),
        "files_present": len(present),
        "all_present": bool(results) and all(r["exists"] for r in results),
        "all_non_empty": bool(results) and all(r["non_empty"] for r in results),
        "all_format_valid": bool(results) and all(r["format_valid"] for r in results),
        # None (not-verifiable) is NOT a failure: all_parse is False only on an
        # explicit False; None counts as "not verified", surfaced separately.
        "all_parse": bool(results) and all(r["parses"] is not False for r in results),
        "parse_unverified": [r["filename"] for r in results if r["parses"] is None],
    }
    return {"output_dir": str(out_dir), "files": results, "aggregate": aggregate}


__all__ = ["verify_downloaded_files"]
