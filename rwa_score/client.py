"""CoinMarketCap Pro API client plus an offline fixture client.

Live endpoints used (Basic plan):
  - GET /v5/real-world-assets/map                -> rwa_id (0 credits)
  - GET /v5/real-world-assets/info               -> metadata incl. CIK (1 credit / 250)
  - GET /v5/real-world-assets/issuers/list       -> issuer directory (1 credit)
  - GET /v5/real-world-assets/issuers            -> single issuer + tokens (1 credit)
  - GET /v5/real-world-assets/quotes/latest      -> tokenized avg/mcap/vol, tokens[], tradfi
  - GET /v5/real-world-assets/assets/list        -> ranked directory / asset_type browse
  - GET /v5/real-world-assets/market-pairs/list  -> wrapper markets for one RWA
  - GET /v2/cryptocurrency/quotes/latest         -> token 24hΔ fallback when RWA quotes lack it
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
# Map / info / market-pairs / RWA quotes / assets list can refresh; issuer directory
# is process-lifetime (no TTL). Short TTLs absorb Streamlit widget reruns.
DEFAULT_MAP_TTL_SECONDS = 120.0
DEFAULT_INFO_TTL_SECONDS = 120.0
DEFAULT_PAIRS_TTL_SECONDS = 120.0
DEFAULT_QUOTES_TTL_SECONDS = 120.0
DEFAULT_ASSETS_TTL_SECONDS = 120.0
DEFAULT_CRYPTO_QUOTE_TTL_SECONDS = 120.0
DEFAULT_MARKET_PAIRS_LIMIT = 100
DEFAULT_ASSETS_LIST_LIMIT = 250
DEFAULT_PAGE_LIMIT = 250
DEFAULT_MAX_PAGES = 80
# Full map / assets/list directory (7.9K+ rows, many pages). Longer than the
# per-widget TTL so Streamlit reruns do not re-paginate or re-burn credits.
DEFAULT_DIRECTORY_TTL_SECONDS = 1800.0
# Directory pagination does not sleep between pages. CMC 429 / error 1008
# still back off inside ``_get``. A fixed gap made full-book walks feel like
# 30–60s whiteouts on Search reruns.
DEFAULT_PAGE_GAP_SECONDS = 0.0
ASSET_TYPES = (
    "stock",
    "commodity",
    "currency",
    "government_security",
    "etf",
    "real_estate",
)
ASSET_TYPE_LABELS = {
    "stock": "Stocks",
    "commodity": "Commodities",
    "currency": "Currencies",
    "government_security": "Treasuries",
    "etf": "ETFs",
    "real_estate": "Real Estate",
}
ENDPOINT_MAP = "/v5/real-world-assets/map"
ENDPOINT_INFO = "/v5/real-world-assets/info"
ENDPOINT_ISSUERS_LIST = "/v5/real-world-assets/issuers/list"
ENDPOINT_ISSUERS = "/v5/real-world-assets/issuers"
ENDPOINT_QUOTES = "/v5/real-world-assets/quotes/latest"
ENDPOINT_ASSETS_LIST = "/v5/real-world-assets/assets/list"
ENDPOINT_MARKET_PAIRS = "/v5/real-world-assets/market-pairs/list"
ENDPOINT_CRYPTO_QUOTE = "/v2/cryptocurrency/quotes/latest"


class CMCError(RuntimeError):
    """Raised when the live CMC API cannot be used or returns an error."""


class RWAClient(Protocol):
    """Shared surface for live CMC and offline fixture clients."""

    source: str

    def rwa_map(
        self,
        symbol: str | None = None,
        *,
        asset_type: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def rwa_info(self, rwa_id: int) -> dict[str, Any]: ...

    def issuers_list(self) -> list[dict[str, Any]]: ...

    def issuer(self, issuer_id: str) -> dict[str, Any]: ...

    def crypto_quote(self, crypto_id: int) -> dict[str, Any]: ...

    def rwa_quotes(
        self,
        *,
        rwa_id: int | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]: ...

    def assets_list(
        self,
        *,
        asset_type: str | None = None,
        start: int = 1,
        limit: int = DEFAULT_ASSETS_LIST_LIMIT,
        sort: str = "rwa_rank",
        sort_dir: str = "asc",
    ) -> dict[str, Any]: ...

    def assets_list_all(
        self,
        *,
        asset_type: str | None = None,
        sort: str = "rwa_rank",
        sort_dir: str = "asc",
    ) -> dict[str, Any]: ...

    def market_pairs(
        self,
        *,
        rwa_id: int | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]: ...

    def begin_run(self) -> None: ...

    def call_log(self) -> list[dict[str, Any]]: ...


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


def _optional_float(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _optional_int(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _normalize_rwa_token(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    return {
        "symbol": (row.get("symbol") or "").strip(),
        "name": (row.get("name") or "").strip(),
        "price": _optional_float(row.get("price")),
        "crypto_id": _optional_int(row.get("crypto_id")),
        "issuer_id": row.get("issuer_id"),
        "issuer_name": (row.get("issuer_name") or "").strip(),
        "market_cap": _optional_float(row.get("market_cap")),
        "volume_24h": _optional_float(row.get("volume_24h")),
    }


def _normalize_tradfi_market(row: Any) -> dict[str, Any] | None:
    """Keep venue identity only — CMC tradfi rows do not include a last price."""
    if not isinstance(row, dict):
        return None
    exchange = row.get("exchange") if isinstance(row.get("exchange"), dict) else {}
    return {
        "exchange": {
            "slug": (exchange.get("slug") or "").strip(),
            "name": (exchange.get("name") or "").strip(),
            "exchange_id": _optional_int(exchange.get("exchange_id")),
        },
        "ticker": (row.get("ticker") or "").strip(),
        "market_url": (row.get("market_url") or "").strip(),
    }


def parse_rwa_quotes_payload(data: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize a CMC (or fixture) ``quotes/latest`` asset object.

    Accepts the inner asset, ``{rwa_assets: [asset]}``, or empty input.
    Missing numeric fields stay ``None`` — callers must not invent them.
    """
    payload = data if isinstance(data, dict) else {}
    if isinstance(payload.get("rwa_assets"), list) and payload["rwa_assets"]:
        first = payload["rwa_assets"][0]
        payload = first if isinstance(first, dict) else {}
    tokens = [
        tok
        for tok in (_normalize_rwa_token(row) for row in (payload.get("tokens") or []))
        if tok is not None
    ]
    tradfi = [
        row
        for row in (_normalize_tradfi_market(item) for item in (payload.get("tradfi_markets") or []))
        if row is not None
    ]
    quotes = payload.get("quotes") if isinstance(payload.get("quotes"), list) else []
    return {
        "rwa_id": _optional_int(payload.get("rwa_id")),
        "name": payload.get("name") or "",
        "symbol": (payload.get("symbol") or "").upper(),
        "slug": payload.get("slug") or "",
        "asset_type": (payload.get("asset_type") or "").strip(),
        "rwa_rank": _optional_int(payload.get("rwa_rank")),
        "has_tokens": bool(payload.get("has_tokens")),
        "average_tokenized_price": _optional_float(payload.get("average_tokenized_price")),
        "tokenized_market_cap": _optional_float(payload.get("tokenized_market_cap")),
        "tokenized_volume_24h": _optional_float(payload.get("tokenized_volume_24h")),
        "quotes": list(quotes),
        "tokens": tokens,
        "tradfi_markets": tradfi,
        "last_updated": payload.get("last_updated"),
    }


def _normalize_directory_row(row: dict[str, Any]) -> dict[str, Any]:
    """Shared map / assets/list fields used by the search directory."""
    return {
        "name": row.get("name") or "",
        "symbol": (row.get("symbol") or "").upper(),
        "slug": row.get("slug") or "",
        "rwa_id": _optional_int(row.get("rwa_id")),
        "asset_type": (row.get("asset_type") or "").strip(),
        "rwa_rank": _optional_int(row.get("rwa_rank")),
        "has_tokens": bool(row.get("has_tokens")),
        "industry": (row.get("industry") or "").strip(),
        "average_tokenized_price": _optional_float(row.get("average_tokenized_price")),
        "tokenized_market_cap": _optional_float(row.get("tokenized_market_cap")),
        "tokenized_volume_24h": _optional_float(row.get("tokenized_volume_24h")),
    }


def directory_has_more(
    parsed: dict[str, Any] | None,
    *,
    start: int,
    batch_len: int,
) -> bool:
    """Continue paging when ``has_more`` or ``total_size`` says the book is incomplete.

    Live CMC sometimes omits ``has_more`` and only sends ``total_size``. Trust
    either signal. ``start`` is the 1-based item offset of this page.
    """
    payload = parsed if isinstance(parsed, dict) else {}
    if payload.get("has_more"):
        return True
    raw_total = payload.get("total_size")
    try:
        total = int(raw_total) if raw_total is not None else None
    except (TypeError, ValueError):
        total = None
    if total is None:
        return False
    page_end = max(0, int(start) - 1) + max(0, int(batch_len))
    return page_end < total


def parse_rwa_map_payload(data: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize a CMC (or fixture) ``map`` ``data`` object, including pagination."""
    payload = data if isinstance(data, dict) else {}
    rows = payload.get("rwa_assets")
    if not isinstance(rows, list):
        rows = []
    assets = [_normalize_directory_row(row) for row in rows if isinstance(row, dict)]
    raw_total = payload.get("total_size")
    try:
        total_size = int(raw_total) if raw_total is not None else len(assets)
    except (TypeError, ValueError):
        total_size = len(assets)
    return {
        "rwa_assets": assets,
        "total_size": total_size,
        "has_more": bool(payload.get("has_more")),
    }


def parse_assets_list_payload(data: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize a CMC (or fixture) ``assets/list`` ``data`` object."""
    payload = data if isinstance(data, dict) else {}
    rows = payload.get("rwa_assets")
    if not isinstance(rows, list):
        rows = []
    assets = [_normalize_directory_row(row) for row in rows if isinstance(row, dict)]
    raw_total = payload.get("total_size")
    try:
        total_size = int(raw_total) if raw_total is not None else len(assets)
    except (TypeError, ValueError):
        total_size = len(assets)
    return {
        "rwa_assets": assets,
        "total_size": total_size,
        "has_more": bool(payload.get("has_more")),
    }


def parse_market_pairs_payload(data: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize a CMC (or fixture) market-pairs ``data`` object.

    Live shape: ``{rwa_id, name, symbol, num_market_pairs, market_pairs, ...}``.
    Missing or empty input becomes an empty-but-valid structure so callers
    never have to special-case ``None``.
    """
    payload = data if isinstance(data, dict) else {}
    pairs = payload.get("market_pairs")
    if not isinstance(pairs, list):
        pairs = []
    rwa_id = payload.get("rwa_id")
    try:
        rwa_id_out = int(rwa_id) if rwa_id is not None else None
    except (TypeError, ValueError):
        rwa_id_out = None
    raw_count = payload.get("num_market_pairs")
    try:
        num_pairs = int(raw_count) if raw_count is not None else len(pairs)
    except (TypeError, ValueError):
        num_pairs = len(pairs)
    return {
        "rwa_id": rwa_id_out,
        "name": payload.get("name") or "",
        "symbol": (payload.get("symbol") or "").upper(),
        "num_market_pairs": num_pairs,
        "market_pairs": list(pairs),
        "total_size": payload.get("total_size", num_pairs),
        "has_more": bool(payload.get("has_more")),
    }


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


class _CallJournal:
    """Per-client CMC/fixture call evidence. Fixture clients never label ``live``."""

    def __init__(self, source: str) -> None:
        self.source = source
        self._calls: list[dict[str, Any]] = []

    def begin_run(self) -> None:
        self._calls = []

    def record(self, endpoint: str, *, via: str, cached: bool = False) -> None:
        source = "fixture" if self.source == "fixture" else "live"
        self._calls.append(
            {
                "endpoint": endpoint,
                "source": source,
                "via": "fixture" if source == "fixture" else via,
                "cached": bool(cached) and source != "fixture",
            }
        )

    def call_log(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._calls]


def summarize_call_log(
    calls: list[dict[str, Any]] | None,
    *,
    client_source: str,
) -> dict[str, Any]:
    """UI/API evidence block. Never claims live when the client is a fixture."""
    source = "fixture" if client_source == "fixture" else "live"
    rows = []
    seen: set[tuple[str, str, str]] = set()
    for raw in calls or []:
        endpoint = str(raw.get("endpoint") or "")
        if not endpoint:
            continue
        row_source = "fixture" if source == "fixture" or raw.get("source") == "fixture" else "live"
        via = "fixture" if row_source == "fixture" else str(raw.get("via") or "network")
        key = (endpoint, row_source, via)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "endpoint": endpoint,
                "source": row_source,
                "via": via,
                "cached": bool(raw.get("cached")) and row_source == "live",
            }
        )
    return {
        "source": source,
        "live": source == "live",
        "label": (
            "bundled DEMO FIXTURES — not live CoinMarketCap"
            if source == "fixture"
            else "live CoinMarketCap API"
        ),
        "endpoints": rows,
    }


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
        pairs_ttl: float | None = DEFAULT_PAIRS_TTL_SECONDS,
        quotes_ttl: float | None = DEFAULT_QUOTES_TTL_SECONDS,
        assets_ttl: float | None = DEFAULT_ASSETS_TTL_SECONDS,
        directory_ttl: float | None = DEFAULT_DIRECTORY_TTL_SECONDS,
        crypto_quote_ttl: float | None = DEFAULT_CRYPTO_QUOTE_TTL_SECONDS,
        page_gap: float = DEFAULT_PAGE_GAP_SECONDS,
        max_pages: int = DEFAULT_MAX_PAGES,
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
        self.pairs_ttl = pairs_ttl
        self.quotes_ttl = quotes_ttl
        self.assets_ttl = assets_ttl
        self.directory_ttl = directory_ttl
        self.page_gap = max(0.0, float(page_gap))
        self.max_pages = max(1, int(max_pages))
        self.crypto_quote_ttl = crypto_quote_ttl
        self._sleep = sleeper or time.sleep
        self._cache = _TTLCache()
        self._journal = _CallJournal(self.source)

    def begin_run(self) -> None:
        self._journal.begin_run()

    def call_log(self) -> list[dict[str, Any]]:
        return self._journal.call_log()

    def _record(self, endpoint: str, *, via: str, cached: bool = False) -> None:
        self._journal.record(endpoint, via=via, cached=cached)

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

    def _normalize_asset_type(self, asset_type: str | None) -> str:
        kind = (asset_type or "").strip().lower()
        if kind and kind not in ASSET_TYPES:
            raise CMCError(
                f"asset_type must be one of {', '.join(ASSET_TYPES)}"
            )
        return kind

    def _collect_directory_pages(
        self,
        *,
        endpoint: str,
        cache_key: str,
        fetch_page: Callable[[int, int], dict[str, Any]],
        ttl: float | None,
        page_limit: int = DEFAULT_PAGE_LIMIT,
    ) -> list[dict[str, Any]]:
        """Page a map / assets/list listing until ``has_more`` is false.

        Caches the assembled directory. Each network page is journaled; a
        later hit is a single cache record. Honors 429 backoff via ``_get``.
        """
        cached = self._cache.get(cache_key, ttl)
        if cached is not None:
            self._record(endpoint, via="cache", cached=True)
            return cached
        rows: list[dict[str, Any]] = []
        seen: set[Any] = set()
        start = 1
        size = max(1, min(int(page_limit), DEFAULT_PAGE_LIMIT))
        for page_i in range(self.max_pages):
            parsed = fetch_page(start, size)
            batch = parsed.get("rwa_assets") or []
            for row in batch:
                if not isinstance(row, dict):
                    continue
                marker = row.get("rwa_id")
                if marker is None:
                    marker = (row.get("symbol") or "").upper()
                if marker in seen:
                    continue
                seen.add(marker)
                rows.append(row)
            self._record(endpoint, via="network")
            if not directory_has_more(parsed, start=start, batch_len=len(batch)) or not batch:
                break
            start += len(batch)
            if page_i + 1 < self.max_pages and self.page_gap:
                self._sleep(self.page_gap)
        self._cache.set(cache_key, rows)
        return copy.deepcopy(rows)

    def rwa_map(
        self,
        symbol: str | None = None,
        *,
        asset_type: str | None = None,
        start: int = 1,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve tickers to ``rwa_id``. Costs 0 credits on Basic.

        With no ``symbol``, paginates the full (optionally typed) map so the
        search directory is complete. Pass ``limit`` for a single page (Search
        lazy-load). A symbol lookup is a single request.
        """
        kind = self._normalize_asset_type(asset_type)
        if symbol:
            key = f"map:sym:{symbol.upper()}:{kind}"
            cached = self._cache.get(key, self.map_ttl)
            if cached is not None:
                self._record(ENDPOINT_MAP, via="cache", cached=True)
                return cached
            params: dict[str, Any] = {"symbol": symbol}
            if kind:
                params["asset_type"] = kind
            data = self._get(ENDPOINT_MAP, params)
            parsed = parse_rwa_map_payload(data.get("data") or {})
            assets = parsed["rwa_assets"]
            self._cache.set(key, assets)
            self._record(ENDPOINT_MAP, via="network")
            return copy.deepcopy(assets)

        if limit is not None:
            page = max(1, int(start))
            size = max(1, min(int(limit), DEFAULT_PAGE_LIMIT))
            cache_key = f"map:PAGE:{kind}:{page}:{size}"
            cached = self._cache.get(cache_key, self.directory_ttl)
            if cached is not None:
                self._record(ENDPOINT_MAP, via="cache", cached=True)
                return cached
            params = {
                "start": page,
                "limit": size,
                "sort": "rwa_rank",
            }
            if kind:
                params["asset_type"] = kind
            data = self._get(ENDPOINT_MAP, params)
            parsed = parse_rwa_map_payload(data.get("data") or {})
            assets = parsed["rwa_assets"]
            self._cache.set(cache_key, assets)
            self._record(ENDPOINT_MAP, via="network")
            return copy.deepcopy(assets)

        def _page(page_start: int, page_limit: int) -> dict[str, Any]:
            params: dict[str, Any] = {
                "start": page_start,
                "limit": page_limit,
                "sort": "rwa_rank",
            }
            if kind:
                params["asset_type"] = kind
            data = self._get(ENDPOINT_MAP, params)
            return parse_rwa_map_payload(data.get("data") or {})

        return self._collect_directory_pages(
            endpoint=ENDPOINT_MAP,
            cache_key=f"map:ALL:{kind}",
            fetch_page=_page,
            ttl=self.directory_ttl,
        )

    def rwa_info(self, rwa_id: int) -> dict[str, Any]:
        key = f"info:{int(rwa_id)}"
        cached = self._cache.get(key, self.info_ttl)
        if cached is not None:
            self._record(ENDPOINT_INFO, via="cache", cached=True)
            return cached
        data = self._get(ENDPOINT_INFO, {"rwa_id": rwa_id})
        assets = data.get("data", {}).get("rwa_assets", [])
        info = assets[0] if assets else {}
        self._cache.set(key, info)
        self._record(ENDPOINT_INFO, via="network")
        return copy.deepcopy(info)

    def issuers_list(self) -> list[dict[str, Any]]:
        cached = self._cache.get("issuers_list", ttl=None)
        if cached is not None:
            self._record(ENDPOINT_ISSUERS_LIST, via="cache", cached=True)
            return cached
        data = self._get(ENDPOINT_ISSUERS_LIST)
        issuers = data.get("data", {}).get("issuers", [])
        self._cache.set("issuers_list", issuers)
        self._record(ENDPOINT_ISSUERS_LIST, via="network")
        return copy.deepcopy(issuers)

    def issuer(self, issuer_id: str) -> dict[str, Any]:
        key = f"issuer:{issuer_id}"
        cached = self._cache.get(key, ttl=None)
        if cached is not None:
            self._record(ENDPOINT_ISSUERS, via="cache", cached=True)
            return cached
        data = self._get(ENDPOINT_ISSUERS, {"issuer_id": issuer_id})
        detail = data.get("data", {})
        self._cache.set(key, detail)
        self._record(ENDPOINT_ISSUERS, via="network")
        return copy.deepcopy(detail)

    def crypto_quote(self, crypto_id: int) -> dict[str, Any]:
        key = f"crypto_quote:{int(crypto_id)}"
        cached = self._cache.get(key, self.crypto_quote_ttl)
        if cached is not None:
            self._record(ENDPOINT_CRYPTO_QUOTE, via="cache", cached=True)
            return cached
        data = self._get(
            ENDPOINT_CRYPTO_QUOTE,
            {"id": crypto_id, "convert": "USD"},
        )
        quote = data.get("data", {}).get(str(crypto_id), {})
        self._cache.set(key, quote)
        self._record(ENDPOINT_CRYPTO_QUOTE, via="network")
        return copy.deepcopy(quote)

    def rwa_quotes(
        self,
        *,
        rwa_id: int | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Latest tokenized aggregates, issuer tokens, and TradFi venues.

        Requires exactly one of ``rwa_id`` or ``symbol``. Cached on a short TTL.
        Does not pass ``convert`` — extra convert credits are not needed for the
        USD aggregates already on the asset object.
        """
        if rwa_id is None and not symbol:
            raise CMCError("rwa_quotes requires rwa_id or symbol")
        if rwa_id is not None and symbol:
            raise CMCError("rwa_quotes accepts only one of rwa_id or symbol")
        if rwa_id is not None:
            key = f"quotes:id:{int(rwa_id)}"
            params: dict[str, Any] = {"rwa_id": int(rwa_id)}
        else:
            key = f"quotes:sym:{(symbol or '').upper()}"
            params = {"symbol": (symbol or "").upper()}
        cached = self._cache.get(key, self.quotes_ttl)
        if cached is not None:
            self._record(ENDPOINT_QUOTES, via="cache", cached=True)
            return cached
        data = self._get(ENDPOINT_QUOTES, params)
        parsed = parse_rwa_quotes_payload(data.get("data") or {})
        self._cache.set(key, parsed)
        self._record(ENDPOINT_QUOTES, via="network")
        return copy.deepcopy(parsed)

    def assets_list(
        self,
        *,
        asset_type: str | None = None,
        start: int = 1,
        limit: int = DEFAULT_ASSETS_LIST_LIMIT,
        sort: str = "rwa_rank",
        sort_dir: str = "asc",
    ) -> dict[str, Any]:
        """Ranked RWA directory. One Basic credit per 250 rows; default page is 250."""
        kind = self._normalize_asset_type(asset_type)
        page = max(1, int(start))
        size = max(1, min(int(limit), 250))
        key = f"assets:{kind}:{page}:{size}:{sort}:{sort_dir}"
        cached = self._cache.get(key, self.assets_ttl)
        if cached is not None:
            self._record(ENDPOINT_ASSETS_LIST, via="cache", cached=True)
            return cached
        params: dict[str, Any] = {
            "start": page,
            "limit": size,
            "sort": sort,
            "sort_dir": sort_dir,
        }
        if kind:
            params["asset_type"] = kind
        data = self._get(ENDPOINT_ASSETS_LIST, params)
        parsed = parse_assets_list_payload(data.get("data") or {})
        self._cache.set(key, parsed)
        self._record(ENDPOINT_ASSETS_LIST, via="network")
        return copy.deepcopy(parsed)

    def assets_list_all(
        self,
        *,
        asset_type: str | None = None,
        sort: str = "rwa_rank",
        sort_dir: str = "asc",
    ) -> dict[str, Any]:
        """Every ``assets/list`` page for one type (or the full ranked book).

        Cached as one directory blob. Prefer this for search; use ``assets_list``
        when a single page is enough. Credits: 1 per 250 rows.
        """
        kind = self._normalize_asset_type(asset_type)
        # ``assets_list`` journals each page (cache or network). This method
        # only journals a cache hit for the assembled directory.
        cache_key = f"assets:ALL:{kind}:{sort}:{sort_dir}"
        cached = self._cache.get(cache_key, self.directory_ttl)
        if cached is not None:
            self._record(ENDPOINT_ASSETS_LIST, via="cache", cached=True)
            return cached
        rows: list[dict[str, Any]] = []
        seen: set[Any] = set()
        start = 1
        size = DEFAULT_PAGE_LIMIT
        for page_i in range(self.max_pages):
            parsed = self.assets_list(
                asset_type=kind or None,
                start=start,
                limit=size,
                sort=sort,
                sort_dir=sort_dir,
            )
            batch = parsed.get("rwa_assets") or []
            for row in batch:
                marker = row.get("rwa_id")
                if marker is None:
                    marker = (row.get("symbol") or "").upper()
                if marker in seen:
                    continue
                seen.add(marker)
                rows.append(row)
            if not directory_has_more(parsed, start=start, batch_len=len(batch)) or not batch:
                break
            start += len(batch)
            if page_i + 1 < self.max_pages and self.page_gap:
                self._sleep(self.page_gap)
        assembled = {
            "rwa_assets": rows,
            "total_size": len(rows),
            "has_more": False,
        }
        self._cache.set(cache_key, assembled)
        return copy.deepcopy(assembled)

    def market_pairs(
        self,
        *,
        rwa_id: int | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """List markets CMC tracks for one RWA's underlying wrapper tokens.

        Requires exactly one of ``rwa_id`` or ``symbol``. Cached on a short TTL
        so Streamlit widget reruns do not re-burn the Basic-plan credit.
        """
        if rwa_id is None and not symbol:
            raise CMCError("market_pairs requires rwa_id or symbol")
        if rwa_id is not None and symbol:
            raise CMCError("market_pairs accepts only one of rwa_id or symbol")
        if rwa_id is not None:
            key = f"pairs:id:{int(rwa_id)}"
            params: dict[str, Any] = {
                "rwa_id": int(rwa_id),
                "limit": DEFAULT_MARKET_PAIRS_LIMIT,
            }
        else:
            key = f"pairs:sym:{(symbol or '').upper()}"
            params = {
                "symbol": (symbol or "").upper(),
                "limit": DEFAULT_MARKET_PAIRS_LIMIT,
            }
        cached = self._cache.get(key, self.pairs_ttl)
        if cached is not None:
            self._record(ENDPOINT_MARKET_PAIRS, via="cache", cached=True)
            return cached
        data = self._get(ENDPOINT_MARKET_PAIRS, params)
        parsed = parse_market_pairs_payload(data.get("data") or {})
        self._cache.set(key, parsed)
        self._record(ENDPOINT_MARKET_PAIRS, via="network")
        return copy.deepcopy(parsed)


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
        self._journal = _CallJournal(self.source)

    def begin_run(self) -> None:
        self._journal.begin_run()

    def call_log(self) -> list[dict[str, Any]]:
        return self._journal.call_log()

    def _record(self, endpoint: str) -> None:
        self._journal.record(endpoint, via="fixture")

    @property
    def label(self) -> str:
        return (self._data.get("meta") or {}).get(
            "label", "DEMO FIXTURE DATA — not live CoinMarketCap API responses"
        )

    def rwa_map(
        self,
        symbol: str | None = None,
        *,
        asset_type: str | None = None,
        start: int = 1,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self._record(ENDPOINT_MAP)
        assets = list(self._data.get("map") or [])
        kind = (asset_type or "").strip().lower()
        if kind:
            assets = [a for a in assets if (a.get("asset_type") or "").lower() == kind]
        if symbol:
            wanted = {part.strip().upper() for part in symbol.split(",") if part.strip()}
            assets = [a for a in assets if (a.get("symbol") or "").upper() in wanted]
        if limit is not None:
            size = max(1, min(int(limit), DEFAULT_PAGE_LIMIT))
            offset = max(0, int(start) - 1)
            assets = assets[offset : offset + size]
        return assets

    def rwa_info(self, rwa_id: int) -> dict[str, Any]:
        self._record(ENDPOINT_INFO)
        info = self._data.get("info") or {}
        return dict(info.get(str(rwa_id)) or {})

    def issuers_list(self) -> list[dict[str, Any]]:
        self._record(ENDPOINT_ISSUERS_LIST)
        return list(self._data.get("issuers_list") or [])

    def issuer(self, issuer_id: str) -> dict[str, Any]:
        self._record(ENDPOINT_ISSUERS)
        issuers = self._data.get("issuers") or {}
        return dict(issuers.get(str(issuer_id)) or {})

    def crypto_quote(self, crypto_id: int) -> dict[str, Any]:
        self._record(ENDPOINT_CRYPTO_QUOTE)
        quotes = self._data.get("quotes") or {}
        return dict(quotes.get(str(crypto_id)) or {})

    def rwa_quotes(
        self,
        *,
        rwa_id: int | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        self._record(ENDPOINT_QUOTES)
        catalog = self._data.get("rwa_quotes") or {}
        if rwa_id is not None:
            return parse_rwa_quotes_payload(catalog.get(str(int(rwa_id))) or {})
        if not symbol:
            return parse_rwa_quotes_payload({})
        wanted = symbol.strip().upper()
        for payload in catalog.values():
            if isinstance(payload, dict) and (payload.get("symbol") or "").upper() == wanted:
                return parse_rwa_quotes_payload(payload)
        return parse_rwa_quotes_payload({})

    def assets_list(
        self,
        *,
        asset_type: str | None = None,
        start: int = 1,
        limit: int = DEFAULT_ASSETS_LIST_LIMIT,
        sort: str = "rwa_rank",
        sort_dir: str = "asc",
    ) -> dict[str, Any]:
        self._record(ENDPOINT_ASSETS_LIST)
        kind = (asset_type or "").strip().lower()
        raw = self._data.get("assets_list")
        if isinstance(raw, dict):
            rows = list(raw.get("rwa_assets") or [])
        elif isinstance(raw, list):
            rows = list(raw)
        else:
            # Older fixtures: ranked map rows stand in for assets/list.
            rows = list(self._data.get("map") or [])
        if kind:
            rows = [row for row in rows if (row.get("asset_type") or "").lower() == kind]
        reverse = sort_dir.lower() == "desc"

        def _sort_key(row: dict[str, Any]) -> tuple:
            if sort == "symbol":
                return ((row.get("symbol") or "").upper(),)
            if sort == "tokenized_market_cap":
                return (_optional_float(row.get("tokenized_market_cap")) or 0.0,)
            if sort == "tokenized_volume_24h":
                return (_optional_float(row.get("tokenized_volume_24h")) or 0.0,)
            if sort == "average_tokenized_price":
                return (_optional_float(row.get("average_tokenized_price")) or 0.0,)
            rank = _optional_int(row.get("rwa_rank"))
            return (rank if rank is not None else 10**9,)

        rows = sorted(rows, key=_sort_key, reverse=reverse)
        page = max(1, int(start))
        size = max(1, min(int(limit), 250))
        offset = page - 1
        sliced = rows[offset : offset + size]
        return parse_assets_list_payload(
            {
                "rwa_assets": sliced,
                "total_size": len(rows),
                "has_more": offset + size < len(rows),
            }
        )

    def assets_list_all(
        self,
        *,
        asset_type: str | None = None,
        sort: str = "rwa_rank",
        sort_dir: str = "asc",
    ) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        start = 1
        while True:
            page = self.assets_list(
                asset_type=asset_type,
                start=start,
                limit=DEFAULT_PAGE_LIMIT,
                sort=sort,
                sort_dir=sort_dir,
            )
            batch = page.get("rwa_assets") or []
            rows.extend(batch)
            if not page.get("has_more") or not batch:
                break
            start += DEFAULT_PAGE_LIMIT
        return {
            "rwa_assets": rows,
            "total_size": len(rows),
            "has_more": False,
        }

    def market_pairs(
        self,
        *,
        rwa_id: int | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        self._record(ENDPOINT_MARKET_PAIRS)
        catalog = self._data.get("market_pairs") or {}
        if rwa_id is not None:
            return parse_market_pairs_payload(catalog.get(str(int(rwa_id))) or {})
        if not symbol:
            return parse_market_pairs_payload({})
        wanted = symbol.strip().upper()
        for payload in catalog.values():
            if isinstance(payload, dict) and (payload.get("symbol") or "").upper() == wanted:
                return parse_market_pairs_payload(payload)
        return parse_market_pairs_payload({})
