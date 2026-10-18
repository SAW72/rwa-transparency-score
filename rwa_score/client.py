"""Shared RWA client surface plus an offline fixture client.

Live market data is ``CoinGeckoClient`` in ``coingecko_client.py``
(``GET /coins/markets`` on the hand-kept allowlist). This module keeps the
protocol, parsers, and fixture client. Fixture files are demo data, not a
live market-data response.
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
from .source_label import source_honesty_label, source_kind

load_dotenv()

TRUE_VALUES = {"1", "true", "yes", "on"}

# CMC Basic: HTTP 429 and status.error_code 1008 share the same per-minute cap.
RATE_LIMIT_HTTP = 429
RATE_LIMIT_CMC_CODES = {1008, "1008"}
# CMC error 1006 / HTTP 403: this API key's plan does not include the endpoint.
PLAN_BLOCK_HTTP = 403
PLAN_BLOCK_CMC_CODES = {1006, "1006"}
PLAN_BLOCK_REASON = "plan-blocked"
PLAN_BLOCKED_LABEL = "plan-blocked / unavailable"
_PLAN_BLOCK_MESSAGE_NEEDLES = (
    "subscription plan",
    "doesn't support this endpoint",
    "does not support this endpoint",
    "not available on your plan",
    "upgrade your plan",
    "your api key's plan",
    "plan doesn't have access",
    "plan does not have access",
    "your api key subscription plan",
)
DEFAULT_MAX_RETRIES = 4
DEFAULT_MAX_WAIT_SECONDS = 60.0
# Map / info / market-pairs (or its 1006 degrade) / RWA quotes / assets list can refresh; issuer directory
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

# Live CMC sometimes returns a display alias instead of the official enum
# (``treasury``, ``Government Security``). Request params stay on ASSET_TYPES.
_ASSET_TYPE_ALIASES = {
    "government_security": "government_security",
    "government_securities": "government_security",
    "govt_security": "government_security",
    "govt_securities": "government_security",
    "treasury": "government_security",
    "treasuries": "government_security",
    "us_treasury": "government_security",
    "us_treasuries": "government_security",
    "fixed_income": "government_security",
    "tbill": "government_security",
    "t_bill": "government_security",
    "t_bills": "government_security",
    "stocks": "stock",
    "equity": "stock",
    "equities": "stock",
    "commodities": "commodity",
    "currencies": "currency",
    "etfs": "etf",
    "realestate": "real_estate",
}


def canonical_asset_type(raw: Any) -> str:
    """Map a CMC / display ``asset_type`` string to the official enum.

    Unknown values return ``""`` — callers must not invent a class. Request
    params sent to CMC must stay on ``ASSET_TYPES`` (``government_security``).
    """
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if text in ASSET_TYPES:
        return text
    collapsed = "".join(ch if ch.isalnum() else "_" for ch in text)
    collapsed = "_".join(part for part in collapsed.split("_") if part)
    if collapsed in ASSET_TYPES:
        return collapsed
    return _ASSET_TYPE_ALIASES.get(collapsed, "")
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


class CMCPlanBlockedError(CMCError):
    """CMC subscription/plan does not include this endpoint (error 1006 / HTTP 403)."""

    def __init__(
        self,
        path: str = "",
        status_code: int = PLAN_BLOCK_HTTP,
        payload: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> None:
        self.path = path or ENDPOINT_MARKET_PAIRS
        self.status_code = int(status_code)
        self.payload = payload
        self.error_code = _cmc_error_code(payload)
        if self.error_code in (None, 0, "0"):
            self.error_code = 1006
        super().__init__(
            message or _plan_block_message(self.path, self.status_code, payload)
        )


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


def cmc_key_present() -> bool:
    """True when CMC_API_KEY is non-empty. The value is never returned."""
    return bool(os.getenv("CMC_API_KEY", "").strip())


def sidebar_uses_fixtures() -> bool:
    """Process default the Streamlit sidebar starts from.

    Fixtures are on only when RWA_USE_FIXTURES is set. Live mode is
    CoinGecko, including keyless public access when no demo key is set.
    A missing CMC key does not force fixtures. This does not see a later
    in-session toggle. Health reads this same function.
    """
    return env_flag("RWA_USE_FIXTURES")


def create_client(
    *,
    use_fixtures_mode: bool | None = None,
    api_key: str | None = None,
    fixture_path: Path | None = None,
) -> RWAClient:
    """Build a CoinGecko or fixture client.

    Fixture mode: ``use_fixtures_mode=True`` or ``RWA_USE_FIXTURES=1``.
    Live mode uses CoinGecko. ``api_key`` overrides ``COINGECKO_DEMO_API_KEY``
    for tests. An empty key uses the keyless public endpoints.
    """
    if use_fixtures(use_fixtures_mode):
        return FixtureClient(path=fixture_path)
    from .coingecko_client import CoinGeckoClient

    return CoinGeckoClient(api_key=api_key)


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


def _cmc_error_message(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    return str((payload.get("status") or {}).get("error_message") or "")


def is_cmc_plan_blocked(
    status_code: int, payload: dict[str, Any] | None
) -> bool:
    """True for CMC error 1006 or a 403 whose body says the plan lacks the endpoint."""
    if _cmc_error_code(payload) in PLAN_BLOCK_CMC_CODES:
        return True
    if int(status_code) != PLAN_BLOCK_HTTP:
        return False
    lowered = _cmc_error_message(payload).lower()
    return any(needle in lowered for needle in _PLAN_BLOCK_MESSAGE_NEEDLES)


def is_market_pairs_plan_block(
    status_code: int,
    payload: dict[str, Any] | None,
    path: str | None = None,
) -> bool:
    """Plan-block on market-pairs: error 1006, or a related HTTP 403 on that path."""
    if is_cmc_plan_blocked(status_code, payload):
        return True
    return path == ENDPOINT_MARKET_PAIRS and int(status_code) == PLAN_BLOCK_HTTP


def market_pairs_plan_blocked(data: dict[str, Any] | None) -> bool:
    """True when a market-pairs payload is the labeled plan-block degrade."""
    if not isinstance(data, dict):
        return False
    return bool(
        data.get("plan_blocked") or data.get("unavailable_reason") == PLAN_BLOCK_REASON
    )


def _plan_block_message(
    path: str, status_code: int, payload: dict[str, Any] | None
) -> str:
    code = _cmc_error_code(payload)
    if code in (None, 0, "0"):
        code = 1006
    detail = _cmc_error_message(payload)
    extra = f" {detail}" if detail else ""
    return (
        f"{path} is not on this CoinMarketCap plan "
        f"(HTTP {status_code}, error_code {code}).{extra} "
        f"Cross-issuer market-pairs are {PLAN_BLOCKED_LABEL} until the plan is upgraded. "
        "This is not live market-pairs data."
    )


def plan_blocked_market_pairs_payload(
    *,
    rwa_id: int | None = None,
    symbol: str | None = None,
    status_code: int | None = None,
    error_code: Any = 1006,
    error_message: str | None = None,
) -> dict[str, Any]:
    """Empty market-pairs shape labeled plan-blocked — never live pair rows."""
    parsed = parse_market_pairs_payload(
        {
            "rwa_id": rwa_id,
            "symbol": symbol or "",
            "num_market_pairs": 0,
            "market_pairs": [],
        }
    )
    parsed["plan_blocked"] = True
    parsed["unavailable"] = True
    parsed["available"] = False
    parsed["unavailable_reason"] = PLAN_BLOCK_REASON
    parsed["status_code"] = status_code
    parsed["error_code"] = error_code if error_code not in (None, 0, "0") else 1006
    parsed["error_message"] = error_message or (
        f"CMC market-pairs {PLAN_BLOCKED_LABEL} — not live market-pairs data."
    )
    return parsed


def _error_looks_like_pairs_plan_block(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        isinstance(exc, CMCPlanBlockedError)
        or "1006" in text
        or "http 403" in text
        or PLAN_BLOCK_REASON in text
        or any(needle in text for needle in _PLAN_BLOCK_MESSAGE_NEEDLES)
    )


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


def _directory_asset_type(row: dict[str, Any]) -> str:
    """CMC class from a map / list row.

    Canonicalize known aliases to the official enum. Preserve an unknown
    live string — do not drop it. Empty only when CMC omitted the field.
    """
    raw = row.get("asset_type")
    if raw in (None, ""):
        raw = row.get("assetType")
    if raw in (None, ""):
        return ""
    return canonical_asset_type(raw) or str(raw).strip()


def _normalize_directory_row(row: dict[str, Any]) -> dict[str, Any]:
    """Shared map / assets/list fields used by the search directory."""
    return {
        "name": row.get("name") or "",
        "symbol": (row.get("symbol") or "").upper(),
        "slug": row.get("slug") or "",
        "rwa_id": _optional_int(row.get("rwa_id")),
        "asset_type": _directory_asset_type(row),
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
        kind = source_kind(self.source)
        source = kind if kind in {"fixture", "live"} else "unknown"
        self._calls.append(
            {
                "endpoint": endpoint,
                "source": source,
                "via": "fixture" if source == "fixture" else via,
                "cached": bool(cached) and source == "live",
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
    kind = source_kind(client_source)
    source = kind if kind in {"fixture", "live"} else "unknown"
    rows = []
    seen: set[tuple[str, str, str]] = set()
    for raw in calls or []:
        endpoint = str(raw.get("endpoint") or "")
        if not endpoint:
            continue
        raw_kind = source_kind(raw.get("source"))
        if source == "fixture" or raw_kind == "fixture":
            row_source = "fixture"
        elif source == "live" or raw_kind == "live":
            row_source = "live"
        else:
            row_source = "unknown"
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
        "label": source_honesty_label(source),
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
            total = len(assets)
            assets = assets[offset : offset + size]
            if not symbol:
                self._directory_page = {
                    "truncated": offset + len(assets) < total,
                    "total_size": total,
                }
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
