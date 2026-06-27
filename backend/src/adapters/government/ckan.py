"""CKAN adapter — reusable for data.gov, data.europa.eu, data.gov.uk, etc.

CKAN is the gold standard for government open data portals. Same protocol,
different base_url. Register multiple instances for each portal.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime

from src.adapters.base import BaseRegistryAdapter, RateLimitConfig
from src.adapters.registry import AdapterRegistry
from src.models.candidates import RawCandidate
from src.models.data_source import AccessLevel, DataSource, DataSourceType
from src.services.http_client import instrumented_get, instrumented_post

logger = logging.getLogger(__name__)


def _flatten_multilingual(value, fallback: str = "") -> str:
    """CKAN-baseline portals return strings; DCAT-AP portals (notably the
    EU Open Data Portal) return ``{"en": "...", "de": "...", ...}`` for
    title/notes. Pick English when present, otherwise the first non-empty
    value. Always returns a string so it can flow into pydantic models.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for lang in ("en", "en-US", "en-GB"):
            v = value.get(lang)
            if isinstance(v, str) and v.strip():
                return v
        for v in value.values():
            if isinstance(v, str) and v.strip():
                return v
    return fallback


class CKANAdapter(BaseRegistryAdapter):
    name = "ckan"
    display_name = "CKAN"
    domains = ["government", "statistics", "census", "economics", "health", "education", "environment"]
    worker_tags = ["gov", "datasets"]
    rate_limit = RateLimitConfig(requests_per_second=5, burst=10)
    requires_auth = False

    def __init__(
        self,
        base_url: str,
        portal_name: str = "CKAN",
        country: str | None = None,
        search_method: str = "GET",
        search_path: str = "/api/3/action/package_search",
        rows_param: str = "rows",
        result_inner_key: str = "results",
    ):
        self.base_url = base_url.rstrip("/")
        self.display_name = portal_name
        # Slugify to ensure clean adapter names regardless of portal_name punctuation.
        slug = re.sub(r"[^a-z0-9]+", "_", portal_name.lower()).strip("_") or "instance"
        self.name = f"ckan_{slug}"
        # Geographic scope: a single-country list when the portal is national
        # (e.g. ["United States"]); None for international portals (HDX,
        # World Bank, EU). Read by _registry_worker_search to skip country-
        # specific portals when the request's geographic_scope clearly
        # doesn't match.
        self.geographic_scope: list[str] | None = [country] if country else None
        # Per-portal protocol overrides — most CKAN portals are uniform, but
        # a few (EU, London) ship slight variants we accommodate in-place
        # rather than carrying separate adapter classes.
        self.search_method = search_method.upper()
        self.search_path = search_path
        self.rows_param = rows_param
        self.result_inner_key = result_inner_key

    async def search(
        self, keywords: list[str], filters: dict
    ) -> list[RawCandidate]:
        query = " ".join(keywords[:5])
        params = {"q": query, self.rows_param: 15}
        url = f"{self.base_url}{self.search_path}"

        logger.debug("CKAN (%s) %s %s: query=%s, %s=15",
                     self.display_name, self.search_method, self.search_path, query[:80], self.rows_param)
        try:
            if self.search_method == "POST":
                data = await instrumented_post(
                    provider="ckan",
                    url=url,
                    json_body=params,
                    result_count_path=f"result.{self.result_inner_key}",
                )
            else:
                data = await instrumented_get(
                    provider="ckan",
                    url=url,
                    params=params,
                    result_count_path=f"result.{self.result_inner_key}",
                )
        except Exception as e:
            logger.warning("CKAN (%s) search failed: %s", self.display_name, e)
            return []
        if data is None:
            return []

        result = data.get("result", {})
        candidates = []
        for package in result.get(self.result_inner_key, []):
            # Standard CKAN: name (slug). DCAT-AP variants (EU): no name —
            # fall through to id, then identifier[0]. Whatever we get
            # ends up in the dataset URL path.
            pkg_name = package.get("name") or package.get("id") or package.get("identifier") or ""
            if isinstance(pkg_name, list):
                pkg_name = pkg_name[0] if pkg_name else ""
            pkg_name = str(pkg_name)
            url = f"{self.base_url}/dataset/{pkg_name}"

            # Determine format from resources
            formats = set()
            for resource in package.get("resources", []) or []:
                fmt = (resource.get("format") or "").lower()
                if fmt:
                    formats.add(fmt)

            org_field = package.get("organization") or {}
            org_title = _flatten_multilingual(org_field.get("title", "") if isinstance(org_field, dict) else "")

            candidates.append(
                RawCandidate(
                    url=url,
                    title=_flatten_multilingual(package.get("title"), fallback=pkg_name),
                    snippet=_flatten_multilingual(package.get("notes"))[:300],
                    source_engine=f"ckan_{self.display_name}",
                    discovery_method="registry",
                    known_type=DataSourceType.DOWNLOADABLE_FILE,
                    metadata={
                        "organization": org_title,
                        "license_title": _flatten_multilingual(package.get("license_title")),
                        "formats": list(formats),
                        "num_resources": len(package.get("resources") or []),
                        "metadata_modified": package.get("metadata_modified"),
                        "domain": "government",
                        "tags": [_flatten_multilingual(t.get("name") if isinstance(t, dict) else t) for t in (package.get("tags") or [])],
                        "data_format": list(formats),
                    },
                )
            )

        for c in candidates:
            logger.debug("CKAN (%s) package: %s (org=%s, formats=%s, resources=%d)",
                          self.display_name, c.url[:60],
                          c.metadata.get("organization"), c.metadata.get("formats"),
                          c.metadata.get("num_resources", 0))
        logger.debug("CKAN (%s) returned %d results for: %s",
                      self.display_name, len(candidates), query[:50])
        return candidates

    def normalize(self, raw: dict) -> DataSource:
        pkg_name = raw.get("name") or "unknown"
        if isinstance(pkg_name, list):
            pkg_name = pkg_name[0] if pkg_name else "unknown"
        pkg_name = str(pkg_name)
        source_id = hashlib.sha256(f"{self.base_url}/{pkg_name}".encode()).hexdigest()[:16]
        formats = set()
        for r in raw.get("resources") or []:
            fmt = r.get("format") if isinstance(r, dict) else None
            if fmt:
                formats.add(fmt.lower())

        return DataSource(
            id=source_id,
            name=_flatten_multilingual(raw.get("title"), fallback=pkg_name),
            provider=self.display_name,
            url=f"{self.base_url}/dataset/{pkg_name}",
            source_type=DataSourceType.DOWNLOADABLE_FILE,
            description=_flatten_multilingual(raw.get("notes"))[:500],
            domain="government",
            tags=[_flatten_multilingual(t.get("name") if isinstance(t, dict) else t) for t in (raw.get("tags") or [])],
            data_format=list(formats),
            access_level=AccessLevel.OPEN,
            license=_flatten_multilingual(raw.get("license_title")) or None,
            discovery_method="registry",
            metadata=raw,
        )


# Register CKAN instances from the portal manifest (see ckan_portals.py).
# Failures are logged but do not block startup — one bad portal shouldn't
# take down the whole adapter registry.
from src.adapters.government.ckan_portals import CKAN_PORTALS  # noqa: E402

_RECOGNIZED_FIELDS = {
    "base_url", "portal_name", "country",
    "search_method", "search_path", "rows_param", "result_inner_key",
}

for _entry in CKAN_PORTALS:
    try:
        _kwargs = {k: v for k, v in _entry.items() if k in _RECOGNIZED_FIELDS}
        AdapterRegistry.register(CKANAdapter(**_kwargs))
    except Exception as _err:  # pragma: no cover — defensive
        logger.warning(
            "Failed to register CKAN portal %s: %s",
            _entry.get("portal_name", "<unnamed>"),
            _err,
        )

logger.info("Registered %d CKAN portal instances", len(CKAN_PORTALS))
