"""Thin wrapper around the CoinMarketCap API (Basic plan)."""

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
            raise CMCError("Set CMC_API_KEY in .env (free Basic key from coinmarketcap.com/api)")
        self.session = session or requests.Session()
        self.session.headers.update({"X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"})

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self.session.get(f"{BASE_URL}{path}", params=params or {}, timeout=20)
        if resp.status_code != 200:
            raise CMCError(f"{path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # --- RWA endpoints ---

    def rwa_map(self) -> list[dict[str, Any]]:
        """All tokenized assets. Costs 0 credits on Basic."""
        data = self._get("/v5/real-world-assets/map")
        return data.get("data", [])

    def rwa_info(self, rwa_id: int) -> dict[str, Any]:
        data = self._get("/v5/real-world-assets/info", {"id": rwa_id})
        return data.get("data", {}).get(str(rwa_id), {})

    def rwa_issuers(self) -> list[dict[str, Any]]:
        data = self._get("/v5/real-world-assets/issuers")
        return data.get("data", [])

    # --- Crypto quotes ---

    def crypto_quote(self, symbol: str) -> dict[str, Any]:
        data = self._get("/v2/cryptocurrency/quotes/latest", {"symbol": symbol})
        return data.get("data", {}).get(symbol, {})
