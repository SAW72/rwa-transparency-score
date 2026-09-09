"""CoinMarketCap Pro API client plus an offline fixture client.

Live endpoints used (Basic plan):
  - GET /v5/real-world-assets/map            -> rwa_id (0 credits)
  - GET /v5/real-world-assets/info           -> metadata incl. CIK (1 credit / 250)
  - GET /v5/real-world-assets/issuers/list   -> issuer directory (1 credit)
  - GET /v5/real-world-assets/issuers        -> single issuer + tokens (1 credit)
  - GET /v2/cryptocurrency/quotes/latest     -> token price/volume
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

import requests
from dotenv import load_dotenv

from .fixtures import DEMO_FIXTURE_PATH

load_dotenv()

BASE_URL = "https://pro-api.coinmarketcap.com"

TRUE_VALUES = {"1", "true", "yes", "on"}


class CMCError(RuntimeError):
    """Raised when the live CMC API cannot be used or returns an error."""


class RWAClient(Protocol):
    """Shared surface for live CMC and offline fixture clients."""

    source: str

    def rwa_map(self, symbol: str | None = None) -> list[dict[str, Any]]: ...

    def rwa_info(self, rwa_id: int) -> dict[str, Any]: ...

    def issuers_list(self) -> list[dict[str, Any]]: ...

    def issuer(self, issuer_id: str) -> dict[str, Any]: ...

    def crypto_quote(self, crypto_id: int) -> dict[str, Any]: ...


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in TRUE_VALUES


def use_fixtures(explicit: bool | None = None) -> bool:
    """Return True when fixture mode is requested via arg or env."""
    if explicit is not None:
        return explicit
    return env_flag("RWA_USE_FIXTURES")


def create_client(
    *,
    use_fixtures_mode: bool | None = None,
    api_key: str | None = None,
    fixture_path: Path | None = None,
) -> RWAClient:
    """Build a live or fixture client.

    Fixture mode: ``use_fixtures_mode=True`` or ``RWA_USE_FIXTURES=1``.
    Live mode: requires ``CMC_API_KEY`` (arg or env).
    """
    if use_fixtures(use_fixtures_mode):
        return FixtureClient(path=fixture_path)
    return CMCClient(api_key=api_key)


class CMCClient:
    """Thin wrapper around the CoinMarketCap Pro API."""

    source = "live"

    def __init__(self, api_key: str | None = None, session: requests.Session | None = None) -> None:
        self.api_key = api_key or os.getenv("CMC_API_KEY", "")
        if not self.api_key:
            raise CMCError(
                "Live mode needs CMC_API_KEY. Set it in .env, or run with --fixtures "
                "/ RWA_USE_FIXTURES=1 for the offline demo."
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
        error_code = status.get("error_code")
        if error_code not in (None, 0, "0"):
            raise CMCError(
                f"{path} -> CMC error {error_code}: {status.get('error_message') or payload}"
            )
        return payload

    def rwa_map(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Resolve a ticker to its rwa_id. Costs 0 credits on Basic."""
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
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


class FixtureClient:
    """Offline client that serves bundled demo JSON. Never calls CMC."""

    source = "fixture"

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DEMO_FIXTURE_PATH
        with self.path.open(encoding="utf-8") as fh:
            self._data = json.load(fh)
        meta = self._data.get("meta") or {}
        if meta.get("kind") != "demo_fixture":
            raise CMCError(
                f"Fixture file {self.path} is missing meta.kind=demo_fixture. "
                "Refusing to treat unlabeled data as live CMC output."
            )

    @property
    def label(self) -> str:
        return (self._data.get("meta") or {}).get(
            "label", "DEMO FIXTURE DATA — not live CoinMarketCap API responses"
        )

    def rwa_map(self, symbol: str | None = None) -> list[dict[str, Any]]:
        assets = list(self._data.get("map") or [])
        if not symbol:
            return assets
        wanted = {part.strip().upper() for part in symbol.split(",") if part.strip()}
        return [a for a in assets if (a.get("symbol") or "").upper() in wanted]

    def rwa_info(self, rwa_id: int) -> dict[str, Any]:
        info = self._data.get("info") or {}
        return dict(info.get(str(rwa_id)) or {})

    def issuers_list(self) -> list[dict[str, Any]]:
        return list(self._data.get("issuers_list") or [])

    def issuer(self, issuer_id: str) -> dict[str, Any]:
        issuers = self._data.get("issuers") or {}
        return dict(issuers.get(str(issuer_id)) or {})

    def crypto_quote(self, crypto_id: int) -> dict[str, Any]:
        quotes = self._data.get("quotes") or {}
        return dict(quotes.get(str(crypto_id)) or {})
