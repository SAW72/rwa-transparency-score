"""CoinMarketCap Pro API client plus an offline fixture client.

Live endpoints used (Basic plan):
  - GET /v5/real-world-assets/map            -> rwa_id (0 credits)
  - GET /v5/real-world-assets/info           -> metadata incl. CIK (1 credit / 250)
  - GET /v5/real-world-assets/issuers/list   -> issuer directory (1 credit)
  - GET /v5/real-world-assets/issuers        -> single issuer + tokens (1 credit)
  - GET /v2/cryptocurrency/quotes/latest     -> token price/volume
"""

from __future__ import annotations

import copy
import json
import os
import random
import time
from pathlib import Path
from typing import Any, Callable, Protocol

import requests
from dotenv import load_dotenv

from .fixtures import DEMO_FIXTURE_PATH

load_dotenv()

BASE_URL = "https://pro-api.coinmarketcap.com"

TRUE_VALUES = {"1", "true", "yes", "on"}

# CMC Basic: HTTP 429 and status.error_code 1008 share the same per-minute cap.
RATE_LIMIT_HTTP = 429
RATE_LIMIT_CMC_CODES = {1008, "1008"}
DEFAULT_MAX_RETRIES = 4
DEFAULT_MAX_WAIT_SECONDS = 60.0
# Map / info can refresh; issuer directory is process-lifetime (no TTL).
DEFAULT_MAP_TTL_SECONDS = 120.0
DEFAULT_INFO_TTL_SECONDS = 120.0


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


def _parse_retry_after(headers: dict[str, Any] | Any) -> float | None:
    raw = None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:  # noqa: BLE001 — CaseInsensitiveDict / plain dict
        raw = None
    if raw is None or raw == "":
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _cmc_error_code(payload: dict[str, Any] | None) -> Any:
    if not payload:
        return None
    return (payload.get("status") or {}).get("error_code")


def _is_rate_limited(status_code: int, payload: dict[str, Any] | None) -> bool:
    if status_code == RATE_LIMIT_HTTP:
        return True
    return _cmc_error_code(payload) in RATE_LIMIT_CMC_CODES


def _rate_limit_message(path: str, status_code: int, payload: dict[str, Any] | None) -> str:
    code = _cmc_error_code(payload)
    if code in (None, 0, "0"):
        code = 1008 if status_code == RATE_LIMIT_HTTP else code
    detail = ""
    if payload:
        detail = (payload.get("status") or {}).get("error_message") or ""
    extra = f" {detail}" if detail else ""
    return (
        f"{path} hit CoinMarketCap's HTTP request rate limit "
        f"(HTTP {status_code}, error_code {code}).{extra} "
        "Basic plan limits reset every minute. Wait a minute and try again, "
        "or use demo fixtures (RWA_USE_FIXTURES=1). "
        "DoraHacks Startup unlocks a higher request rate."
    )


class _TTLCache:
    """In-process cache. ``ttl=None`` means keep until process exit."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: float | None) -> Any | None:
        hit = self._store.get(key)
        if hit is None:
            return None
        stamped, value = hit
        if ttl is not None and (time.monotonic() - stamped) > ttl:
            del self._store[key]
            return None
        return copy.deepcopy(value)

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.monotonic(), copy.deepcopy(value))


class CMCClient:
    """Thin wrapper around the CoinMarketCap Pro API.

    Retries HTTP 429 and CMC error 1008 with exponential backoff + jitter.
    Caches ``issuers/list`` and per-issuer detail for the process lifetime so
    Streamlit reruns and a second ticker do not re-burn those calls. Map and
    info use a short TTL (enough to absorb widget interactions).
    """

    source = "live"

    def __init__(
        self,
        api_key: str | None = None,
        session: requests.Session | None = None,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
        map_ttl: float | None = DEFAULT_MAP_TTL_SECONDS,
        info_ttl: float | None = DEFAULT_INFO_TTL_SECONDS,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
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
        self.max_retries = max_retries
        self.max_wait_seconds = max_wait_seconds
        self.map_ttl = map_ttl
        self.info_ttl = info_ttl
        self._sleep = sleeper or time.sleep
        self._cache = _TTLCache()

    def _backoff_delay(self, attempt: int, retry_after: float | None, remaining: float) -> float:
        """Seconds to wait before the next try. ``attempt`` is 0 on the first retry."""
        if retry_after is not None:
            return min(retry_after, max(remaining, 0.0), self.max_wait_seconds)
        base = min(2**attempt, self.max_wait_seconds)
        jitter = random.uniform(0.0, max(base * 0.25, 0.05))
        return min(base + jitter, max(remaining, 0.0), self.max_wait_seconds)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        waited = 0.0
        last_status = 0
        last_payload: dict[str, Any] | None = None
        last_text = ""

        for attempt in range(self.max_retries + 1):
            resp = self.session.get(f"{BASE_URL}{path}", params=params or {}, timeout=20)
            last_status = resp.status_code
            last_text = resp.text or ""
            try:
                last_payload = resp.json()
            except ValueError:
                last_payload = None

            if _is_rate_limited(resp.status_code, last_payload):
                remaining = self.max_wait_seconds - waited
                can_retry = attempt < self.max_retries and remaining > 0
                if can_retry:
                    delay = self._backoff_delay(
                        attempt, _parse_retry_after(resp.headers), remaining
                    )
                    if delay > 0:
                        self._sleep(delay)
                        waited += delay
                    continue
                raise CMCError(_rate_limit_message(path, resp.status_code, last_payload))

            if resp.status_code != 200:
                raise CMCError(f"{path} -> HTTP {resp.status_code}: {last_text[:400]}")
            if last_payload is None:
                raise CMCError(f"{path} -> HTTP {resp.status_code}: response was not JSON")
            error_code = _cmc_error_code(last_payload)
            if error_code not in (None, 0, "0"):
                raise CMCError(
                    f"{path} -> CMC error {error_code}: "
                    f"{(last_payload.get('status') or {}).get('error_message') or last_payload}"
                )
            return last_payload

        raise CMCError(_rate_limit_message(path, last_status, last_payload))

    def rwa_map(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Resolve a ticker to its rwa_id. Costs 0 credits on Basic."""
        key = f"map:{(symbol or '').upper()}"
        cached = self._cache.get(key, self.map_ttl)
        if cached is not None:
            return cached
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/v5/real-world-assets/map", params)
        assets = data.get("data", {}).get("rwa_assets", [])
        self._cache.set(key, assets)
        return copy.deepcopy(assets)

    def rwa_info(self, rwa_id: int) -> dict[str, Any]:
        key = f"info:{int(rwa_id)}"
        cached = self._cache.get(key, self.info_ttl)
        if cached is not None:
            return cached
        data = self._get("/v5/real-world-assets/info", {"rwa_id": rwa_id})
        assets = data.get("data", {}).get("rwa_assets", [])
        info = assets[0] if assets else {}
        self._cache.set(key, info)
        return copy.deepcopy(info)

    def issuers_list(self) -> list[dict[str, Any]]:
        cached = self._cache.get("issuers_list", ttl=None)
        if cached is not None:
            return cached
        data = self._get("/v5/real-world-assets/issuers/list")
        issuers = data.get("data", {}).get("issuers", [])
        self._cache.set("issuers_list", issuers)
        return copy.deepcopy(issuers)

    def issuer(self, issuer_id: str) -> dict[str, Any]:
        key = f"issuer:{issuer_id}"
        cached = self._cache.get(key, ttl=None)
        if cached is not None:
            return cached
        data = self._get("/v5/real-world-assets/issuers", {"issuer_id": issuer_id})
        detail = data.get("data", {})
        self._cache.set(key, detail)
        return copy.deepcopy(detail)

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
