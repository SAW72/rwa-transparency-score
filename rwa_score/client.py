"""Thin wrapper around the CoinMarketCap Pro API (Basic / Startup).

Endpoints used (all available on free Basic):
  - GET /v5/real-world-assets/map            -> rwa_id (0 credits)
  - GET /v5/real-world-assets/info           -> metadata incl. CIK (1 credit / 250)
  - GET /v5/real-world-assets/issuers/list   -> issuer directory (1 credit)
  - GET /v5/real-world-assets/issuers        -> single issuer + tokens (1 credit)
  - GET /v2/cryptocurrency/quotes/latest     -> token price/volume (standard)

Use :func:`create_client` so the demo can fall back to canned fixtures when
``CMC_API_KEY`` is missing. Never log or print the key.
"""

from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://pro-api.coinmarketcap.com"

# Env values that mean "use canned CMC-shaped responses, no network".
_TRUTHY = {"1", "true", "yes", "on"}


class CMCError(RuntimeError):
    pass


@runtime_checkable
class RWAClient(Protocol):
    """Shared surface for the live CMC client and the offline fixture client."""

    source: str

    def rwa_map(self, symbol: str | None = None) -> list[dict[str, Any]]: ...

    def rwa_info(self, rwa_id: int) -> dict[str, Any]: ...

    def issuers_list(self) -> list[dict[str, Any]]: ...

    def issuer(self, issuer_id: str) -> dict[str, Any]: ...

    def crypto_quote(self, crypto_id: int) -> dict[str, Any]: ...


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY


def create_client(
    api_key: str | None = None,
    *,
    use_fixtures: bool | None = None,
    session: requests.Session | None = None,
) -> RWAClient:
    """Return a live CMC client or the offline fixture client.

    Resolution order when ``use_fixtures`` is omitted:
      1. ``USE_FIXTURES=1`` → fixtures
      2. no API key → fixtures (so the demo always boots)
      3. otherwise live CMC
    """
    key = (api_key if api_key is not None else os.getenv("CMC_API_KEY", "")).strip()
    if use_fixtures is None:
        use_fixtures = _env_flag("USE_FIXTURES") or not key
    if use_fixtures:
        from .fixtures import FixtureClient

        return FixtureClient()
    return CMCClient(api_key=key, session=session)


class CMCClient:
    source = "cmc-live"

    def __init__(self, api_key: str | None = None, session: requests.Session | None = None) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("CMC_API_KEY", "")).strip()
        if not self.api_key:
            raise CMCError(
                "Set CMC_API_KEY in .env (free Basic key from https://coinmarketcap.com/api/) "
                "or run with USE_FIXTURES=1 / --fixtures."
            )
        self.session = session or requests.Session()
        self.session.headers.update(
            {"X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"}
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self.session.get(f"{BASE_URL}{path}", params=params or {}, timeout=20)
        if resp.status_code != 200:
            raise CMCError(f"{path} -> HTTP {resp.status_code}: {resp.text[:400]}")
        payload = resp.json()
        status = payload.get("status") or {}
        if status.get("error_code"):
            raise CMCError(f"{path} -> CMC {status.get('error_code')}: {status.get('error_message')}")
        return payload

    def rwa_map(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Resolve a ticker to its rwa_id. Costs 0 credits on Basic."""
        params: dict[str, Any] = {"asset_type": "stock"}
        if symbol:
            params = {"symbol": symbol}
        data = self._get("/v5/real-world-assets/map", params)
        return data.get("data", {}).get("rwa_assets", [])

    def rwa_info(self, rwa_id: int) -> dict[str, Any]:
        data = self._get("/v5/real-world-assets/info", {"rwa_id": rwa_id})
        assets = data.get("data", {}).get("rwa_assets", [])
        return assets[0] if assets else {}

    def issuers_list(self) -> list[dict[str, Any]]:
        data = self._get("/v5/real-world-assets/issuers/list")
        return data.get("data", {}).get("issuers", [])

    def issuer(self, issuer_id: str) -> dict[str, Any]:
        data = self._get("/v5/real-world-assets/issuers", {"issuer_id": issuer_id})
        return data.get("data", {})

    def crypto_quote(self, crypto_id: int) -> dict[str, Any]:
        data = self._get(
            "/v2/cryptocurrency/quotes/latest",
            {"id": crypto_id, "convert": "USD"},
        )
        return data.get("data", {}).get(str(crypto_id), {})
