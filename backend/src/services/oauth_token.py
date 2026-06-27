"""OAuth2 token manager with automatic refresh.

Used by adapters whose APIs require OAuth client_credentials flow:
  - Copernicus Data Space (CDSE)
  - Future: other OAuth2 providers
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class TokenState:
    access_token: str = ""
    expires_at: float = 0.0  # epoch seconds
    refresh_margin: float = 60.0  # refresh 60s before expiry


class OAuthTokenManager:
    """Manages OAuth2 client_credentials tokens with automatic refresh.

    Usage:
        mgr = OAuthTokenManager(
            token_url="https://identity.../token",
            client_id="...",
            client_secret="...",
        )
        token = await mgr.get_token()  # auto-fetches and caches
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        grant_type: str = "client_credentials",
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._grant_type = grant_type
        self._state = TokenState()
        self._lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _is_expired(self) -> bool:
        return time.time() >= (self._state.expires_at - self._state.refresh_margin)

    async def get_token(self) -> str:
        """Get valid access token, refreshing if needed."""
        if not self.is_configured:
            raise RuntimeError("OAuth client not configured (missing client_id/secret)")

        # Double-checked locking: only one concurrent refresh
        if not self._state.access_token or self._is_expired():
            async with self._lock:
                if not self._state.access_token or self._is_expired():
                    await self._refresh_token()

        return self._state.access_token

    async def _refresh_token(self) -> None:
        """Request a new access token."""
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                self._token_url,
                data={
                    "grant_type": self._grant_type,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            data = response.json()

        self._state.access_token = data["access_token"]
        expires_in = data.get("expires_in", 600)  # default 10 min
        self._state.expires_at = time.time() + expires_in

        logger.info(
            "OAuth token refreshed (expires in %d seconds)", expires_in
        )

    async def auth_header(self) -> dict[str, str]:
        """Get Authorization header for HTTP requests."""
        token = await self.get_token()
        return {"Authorization": f"Bearer {token}"}


# ──────────────────────────────────────────
# Provider-specific singletons
# ──────────────────────────────────────────

_copernicus_manager: OAuthTokenManager | None = None


def get_copernicus_token_manager() -> OAuthTokenManager:
    """Get or create the Copernicus OAuth token manager."""
    global _copernicus_manager
    if _copernicus_manager is None:
        from src.config import settings
        _copernicus_manager = OAuthTokenManager(
            token_url=settings.adapter.copernicus_token_url,
            client_id=settings.adapter.copernicus_client_id,
            client_secret=settings.adapter.copernicus_client_secret,
        )
    return _copernicus_manager
