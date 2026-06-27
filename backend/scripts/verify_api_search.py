"""Quick verification for the API-directory semantic search (query_api_directory).

Runs a few Chinese + English queries against the two-route index (dense bge-m3
+ BM25 + RRF) and prints the top hits joined back to apis_merged, so we can
eyeball recall + cross-language behaviour vs the old substring scorer.

    python backend/scripts/verify_api_search.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # project root (backend/scripts/..)
sys.path.insert(0, str(ROOT / "backend"))

from src.adapters.api_directory.semantic_search import index_exists, search  # noqa: E402

DB = ROOT / "API_scripts" / "data" / "unified.sqlite"

QUERIES = [
    "hotel booking and pricing data",
    "酒店预订 价格 数据",
    "weather forecast",
    "天气预报 API",
    "real-time stock market quotes",
]


def _rows_by_key() -> dict:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    try:
        cols = "merge_key,name,domain,category,has_free_tier,signup_url"
        return {str(r["merge_key"]): dict(r) for r in conn.execute(f"SELECT {cols} FROM apis_merged")}
    finally:
        conn.close()


def main() -> None:
    if not index_exists():
        raise SystemExit("semantic index missing — run python API_scripts/build_semantic_index.py")
    rows = _rows_by_key()
    for q in QUERIES:
        print(f"\n=== {q} ===")
        hits = search(q, 8)
        if not hits:
            print("  (no hits)")
        for mk, score in hits:
            r = rows.get(mk, {})
            name = str(r.get("name") or "?")[:30]
            domain = str(r.get("domain") or "?")[:26]
            print(f"  {score:.3f}  {name:<30}  {domain:<26}  free={r.get('has_free_tier')}  cat={r.get('category')}")


if __name__ == "__main__":
    main()
