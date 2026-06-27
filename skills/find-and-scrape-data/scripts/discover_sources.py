#!/usr/bin/env python3
"""discover_sources — find candidate data sources for a question (no key, no LLM).

Queries free / public source registries with httpx and prints ranked candidate
sources as JSON. Distilled from the Zillusion backend adapters:
  - CKAN open-data portals (default: catalog.data.gov)   -> source_type "file"
  - OpenAlex (datasets only)                              -> source_type "file"
  - Hugging Face datasets                                 -> source_type "file"
  - APIs.guru OpenAPI directory                           -> source_type "api"

The calling agent does the judging/ranking on top of these candidates.

Usage:
    python discover_sources.py "<question>" [--limit N] [--ckan URL ...]
                               [--sources ckan,openalex,huggingface,apis_guru]
                               [--timeout S]

Output (stdout): JSON list of
    {"name","url","source_type","registry","snippet","score"}
Progress / per-source errors go to stderr; one failing registry never aborts
the others.
"""
from __future__ import annotations

import argparse
import json
import sys

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.stderr.write("discover_sources needs httpx:  pip install httpx\n")
    raise SystemExit(2)

UA = "zillusion-find-and-scrape-data/0.2"
DEFAULT_CKAN = ["https://catalog.data.gov"]


def _err(tag: str, e: Exception) -> None:
    sys.stderr.write(f"  [{tag}] {type(e).__name__}: {e}\n")


def ckan(client, portal, query, limit):
    out = []
    base = portal.rstrip("/")
    try:
        r = client.get(f"{base}/api/3/action/package_search", params={"q": query, "rows": limit})
        r.raise_for_status()
        results = (r.json().get("result") or {}).get("results") or []
    except Exception as e:  # noqa: BLE001
        _err(f"ckan {base}", e)
        return out
    for pkg in results:
        name = pkg.get("name") or pkg.get("id") or ""
        if isinstance(name, list):
            name = name[0] if name else ""
        if not name:
            continue
        out.append({
            "name": pkg.get("title") or name,
            "url": f"{base}/dataset/{name}",
            "source_type": "file",
            "registry": f"ckan:{base}",
            "snippet": (pkg.get("notes") or "")[:300],
            "score": pkg.get("num_resources", len(pkg.get("resources") or [])),
        })
    return out


def openalex(client, query, limit):
    out = []
    try:
        r = client.get("https://api.openalex.org/works", params={
            "search": query,
            "per_page": limit,
            "sort": "relevance_score:desc",
            "filter": "type:dataset",
            "select": "id,display_name,doi,cited_by_count,type,open_access,publication_date",
        })
        r.raise_for_status()
        results = r.json().get("results") or []
    except Exception as e:  # noqa: BLE001
        _err("openalex", e)
        return out
    for w in results:
        # Null-safe: ``doi`` may be present-but-null (the backend adapter had a
        # crash here). ``or`` chains past None to the OpenAlex id.
        url = w.get("doi") or w.get("id") or ""
        if not url:
            continue
        oa = w.get("open_access") or {}
        out.append({
            "name": w.get("display_name") or url,
            "url": url,
            "source_type": "file",
            "registry": "openalex",
            "snippet": f"cited_by={w.get('cited_by_count', 0)}, oa={oa.get('is_oa', False)}",
            "score": w.get("cited_by_count", 0),
        })
    return out


def huggingface(client, query, limit):
    out = []
    try:
        r = client.get("https://huggingface.co/api/datasets", params={
            "search": query, "limit": limit, "sort": "downloads", "direction": "-1"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        _err("huggingface", e)
        return out
    for d in data if isinstance(data, list) else []:
        did = d.get("id") or ""
        if not did:
            continue
        out.append({
            "name": did,
            "url": f"https://huggingface.co/datasets/{did}",
            "source_type": "file",
            "registry": "huggingface",
            "snippet": f"downloads={d.get('downloads', 0)}, tags={','.join((d.get('tags') or [])[:5])}",
            "score": d.get("downloads", 0),
        })
    return out


def apis_guru(client, query, limit):
    """Live APIs.guru directory (the backend uses a local dump; standalone we
    fetch the canonical list.json and rank by query-token overlap)."""
    out = []
    try:
        r = client.get("https://api.apis.guru/v2/list.json")
        r.raise_for_status()
        catalog = r.json()
    except Exception as e:  # noqa: BLE001
        _err("apis_guru", e)
        return out
    tokens = [t for t in query.lower().split() if len(t) > 2]
    scored = []
    for provider, entry in catalog.items():
        versions = entry.get("versions") or {}
        ver = versions.get(entry.get("preferred")) or next(iter(versions.values()), {})
        info = ver.get("info") or {}
        hay = f"{provider} {info.get('title', '')} {info.get('description', '')}".lower()
        matches = sum(1 for t in tokens if t in hay)
        if not matches:
            continue
        ext = info.get("externalDocs") or {}
        url = (ext.get("url") if isinstance(ext, dict) else "") or ver.get("swaggerUrl") or f"https://apis.guru/#{provider}"
        scored.append((matches, {
            "name": info.get("title") or provider,
            "url": url,
            "source_type": "api",
            "registry": "apis_guru",
            "snippet": (info.get("description") or "")[:300],
            "score": matches,
        }))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:limit]]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="discover_sources", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("question", help="plain-language data need")
    p.add_argument("--limit", type=int, default=15, help="per-registry result cap")
    p.add_argument("--ckan", action="append", default=None, metavar="URL",
                   help="CKAN portal base URL (repeatable; default catalog.data.gov)")
    p.add_argument("--sources", default="ckan,openalex,huggingface,apis_guru",
                   help="comma-separated subset of registries to query")
    p.add_argument("--timeout", type=float, default=20.0)
    args = p.parse_args(argv)

    want = {s.strip() for s in args.sources.split(",") if s.strip()}
    portals = args.ckan or DEFAULT_CKAN
    results = []
    with httpx.Client(timeout=args.timeout, follow_redirects=True,
                      headers={"User-Agent": UA}) as client:
        if "ckan" in want:
            for portal in portals:
                results += ckan(client, portal, args.question, args.limit)
        if "openalex" in want:
            results += openalex(client, args.question, args.limit)
        if "huggingface" in want:
            results += huggingface(client, args.question, args.limit)
        if "apis_guru" in want:
            results += apis_guru(client, args.question, args.limit)

    seen, deduped = set(), []
    for r in results:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        deduped.append(r)
    deduped.sort(key=lambda r: r.get("score", 0), reverse=True)

    print(json.dumps(deduped, ensure_ascii=False, indent=2))
    sys.stderr.write(f"discover_sources: {len(deduped)} candidates from {sorted(want)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
