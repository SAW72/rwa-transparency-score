"""Thin wrapper around the CoinMarketCap Pro API (Basic plan).

Endpoints used (all available on free Basic):
  - GET /v5/real-world-assets/map            -> rwa_id (0 credits)
  - GET /v5/real-world-assets/info           -> metadata incl. CIK (1 credit / 250)
  - GET /v5/real-world-assets/issuers/list   -> issuer directory (1 credit)
  - GET /v5/real-world-assets/issuers        -> single issuer + tokens (1 credit)
  - GET /v2/cryptocurrency/quotes/latest     -> token price/volume (standard)
"""

from __future__ import annotations

import os
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://pro-api.coinmarketcap.com"


class CMCError(RuntimeError):
    pass


class CMCClient:
    def __init__(self, api_key: str | None = None, session: requests.Session | None = None) -> None:
        self.api_key = api_key or os.getenv("CMC_API_KEY", "")
        if not self.api_key:
            raise CMCError(
                "Set CMC_API_KEY in .env (free Basic key from https://coinmarketcap.com/api/)"
            )
        self.session = session or requests.Session()
        self.session.headers.update(
            {"X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"}
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self.session.get(f"{BASE_URL}{path}", params=params or {}, timeout=20)
        if resp.status_code != 200:
            raise CMCError(f"{path} -> HTTP {resp.status_code}: {resp.text[:400]}")
        return resp.json()

    # --- RWA endpoints ---

    def rwa_map(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Resolve a ticker to its rwa_id. Costs 0 credits on Basic."""
        params = {"symbol": symbol} if symbol else {}
        data = self._get("/v5/real-world-assets/map", params)
        return data.get("data", {}).get("rwa_assets", [])

    def rwa_info(self, rwa_id: int) -> dict[str, Any]:
        data = self._get("/v5/real-world-assets/info", {"id": rwa_id})
        assets = data.get("data", {}).get("rwa_assets", [])
        return assets[0] if assets else {}

    def issuers_list(self) -> list[dict[str, Any]]:
        data = self._get("/v5/real-world-assets/issuers/list")
        return data.get("data", {}).get("issuers", [])

    def issuer(self, issuer_id: str) -> dict[str, Any]:
        data = self._get("/v5/real-world-assets/issuers", {"issuer_id": issuer_id})
        return data.get("data", {})

    # --- Crypto quotes (for the on-chain token) ---

    def crypto_quote(self, crypto_id: int) -> dict[str, Any]:
        data = self._get(
            "/v2/cryptocurrency/quotes/latest",
            {"id": crypto_id, "convert": "USD"},
        )
        return data.get("data", {}).get(str(crypto_id), {})
