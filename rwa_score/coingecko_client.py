"""CoinGecko market-data client for the existing RWA client surface.

Primary call: ``GET /coins/markets`` for ids on the hand-kept allowlist.
No CoinGecko category filter (the real-world-assets category is not trusted).

When that call returns HTTP 429, HTTP 5xx, or a network error, price, market
cap, and volume fall back to CoinMarketCap's keyless public quotes. No API
key is sent on that request.

Demo key: env ``COINGECKO_DEMO_API_KEY``, header ``x-cg-demo-api-key``.
Empty or unset → keyless public CoinGecko. The key is never logged or placed
in an error string.
"""

from __future__ import annotations

import copy
import json
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

import requests

from .client import (
    ASSET_TYPES,
    CMCError,
    _CallJournal,
    _optional_float,
    parse_assets_list_payload,
    parse_market_pairs_payload,
    parse_rwa_quotes_payload,
)

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
MARKETS_PATH = "/coins/markets"
CMC_KEYLESS_QUOTES_URL = (
    "https://pro-api.coinmarketcap.com/public-api/v3/cryptocurrency/quotes/latest"
)
DEMO_KEY_ENV = "COINGECKO_DEMO_API_KEY"
DEMO_KEY_HEADER = "x-cg-demo-api-key"
KEYLESS_NOTE = "Using CoinGecko public keyless access; limits are lower."
NOT_AVAILABLE = "not available"
NO_ONCHAIN_BACKING = "no on-chain backing evidence found"

# CoinGecko Demo is 100 calls/min and 10,000 calls/month. Stay under both.
CALLS_PER_MINUTE = 30
CALLS_PER_DAY = 300
MAX_RETRIES = 3
MAX_WAIT_SECONDS = 10.0
BACKOFF_SECONDS = (1.0, 2.0, 4.0)
# After CoinGecko and the CMC fallback both fail, skip every network call and
# sleep for this many seconds and serve the stale or empty result. One failed
# cycle costs at most 2 x MAX_WAIT_SECONDS; without this, each page call in an
# outage pays that cost again. Any success clears it.
COOLDOWN_SECONDS = 45.0
# Quotes refresh inside the 5–10 minute ceiling. Directory identity is local
# and may be reused for 30 minutes. Both are inside the 24h terms window.
QUOTE_TTL_SECONDS = 600.0
DIRECTORY_TTL_SECONDS = 1800.0
IDS_PER_REQUEST = 250

# Sanity thresholds. A single token above the ceiling, or above this share of
# the allowlist's positive market caps, is unverified (Figure Heloc is the
# example: about $25.6B against a much smaller sector total on rwa.xyz).
MARKET_CAP_CEILING_USD = 10_000_000_000.0
MAX_ALLOWLIST_SHARE = 0.25
SUPPLY_CONSISTENCY_TOLERANCE = 0.05
SOURCE_DISAGREEMENT_TOLERANCE = 0.20

ALLOWLIST_PATH = Path(__file__).resolve().parent / "data" / "rwa_allowlist.json"

UNAVAILABLE_FIELDS = (
    "cik",
    "tradfi_markets",
    "rwa_rank",
    "holders",
    "issuer_backing",
    "currency",
    "real_estate",
    "government_security",
)


class CoinGeckoError(CMCError):
    """Live CoinGecko (or the keyless fallback) could not return fresh data."""


def _redact(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "[redacted]")
    return text


def parse_retry_after(raw: Any, *, now: datetime | None = None) -> float | None:
    """Seconds to wait. ASCII digits or a clear HTTP-date only.

    Negatives, decimals, lists, non-ASCII digits (``²``, Arabic-Indic, full
    width), and other garbage return ``None`` so the
    caller uses 1s/2s/4s backoff instead of trusting the header.
    """
    if raw is None or isinstance(raw, (list, tuple, dict, bool)):
        return None
    if isinstance(raw, float):
        return None
    if isinstance(raw, int):
        if raw < 0:
            return None
        return float(raw)
    text = str(raw).strip()
    if not text:
        return None
    if text.isascii() and text.isdigit():
        return float(int(text))
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed is None or parsed.tzinfo is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - current).total_seconds())


def assess_market_row(
    metrics: dict[str, Any],
    *,
    allowlist_total: float | None,
    other_market_cap: float | None = None,
) -> list[str]:
    """Reasons a row is unverified. Empty means the row may feed a score.

    Missing or non-positive market cap, a non-positive price, a price times
    circulating supply that disagrees with market cap, a market cap above
    ``MARKET_CAP_CEILING_USD``, an implausible share of the allowlist total,
    or a CoinGecko vs fallback market-cap gap. Rows are never dropped here.
    """
    reasons: list[str] = []
    mcap = _optional_float(metrics.get("market_cap"))
    price = _optional_float(metrics.get("price"))
    supply = _optional_float(metrics.get("circulating_supply"))
    if mcap is None or mcap <= 0:
        reasons.append("market_cap_missing_or_non_positive")
    if price is None or price <= 0:
        reasons.append("price_missing_or_non_positive")
    if mcap is not None and mcap > 0 and price is not None and price > 0 and supply is not None:
        implied = price * supply
        if supply < 0 or implied <= 0 or abs(implied - mcap) / mcap > SUPPLY_CONSISTENCY_TOLERANCE:
            reasons.append("supply_inconsistent")
    if mcap is not None and mcap > MARKET_CAP_CEILING_USD:
        reasons.append("market_cap_above_ceiling")
    if (
        mcap is not None
        and mcap > 0
        and allowlist_total is not None
        and allowlist_total > 0
        and (mcap / allowlist_total) > MAX_ALLOWLIST_SHARE
    ):
        reasons.append("allowlist_share_implausible")
    other = _optional_float(other_market_cap)
    if mcap is not None and mcap > 0 and other is not None and other > 0:
        base = max(abs(mcap), abs(other))
        if abs(mcap - other) / base > SOURCE_DISAGREEMENT_TOLERANCE:
            reasons.append("source_disagreement")
    return reasons


def load_allowlist(path: Path | None = None) -> list[dict[str, Any]]:
    """Read the hand-kept allowlist. Does not call the network."""
    file_path = Path(path) if path else ALLOWLIST_PATH
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    rows = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise CoinGeckoError(f"Allowlist {file_path} has no assets.")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CoinGeckoError("Allowlist row is not an object.")
        cg_id = str(row.get("coingecko_id") or "").strip()
        symbol = str(row.get("symbol") or "").strip().upper()
        if not cg_id or not symbol:
            raise CoinGeckoError("Allowlist row needs coingecko_id and symbol.")
        if cg_id in seen:
            raise CoinGeckoError(f"Duplicate CoinGecko id in allowlist: {cg_id}")
        seen.add(cg_id)
        asset_class = str(row.get("asset_class") or "unknown").strip().lower() or "unknown"
        out.append(
            {
                "rwa_id": int(row["rwa_id"]),
                "name": str(row.get("name") or ""),
                "symbol": symbol,
                "chain": row.get("chain") or None,
                "contract_address": row.get("contract_address") or None,
                "coingecko_id": cg_id,
                "asset_class": asset_class,
            }
        )
    return out


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class _Budget:
    def __init__(
        self,
        *,
        per_minute: int = CALLS_PER_MINUTE,
        per_day: int = CALLS_PER_DAY,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.per_minute = per_minute
        self.per_day = per_day
        self._clock = clock or time.monotonic
        self._stamps: list[float] = []

    def allow(self) -> bool:
        now = self._clock()
        self._stamps = [stamp for stamp in self._stamps if now - stamp < 86400.0]
        minute = [stamp for stamp in self._stamps if now - stamp < 60.0]
        if len(minute) >= self.per_minute or len(self._stamps) >= self.per_day:
            return False
        self._stamps.append(now)
        return True


class CoinGeckoClient:
    """Allowlist-backed CoinGecko client. Same method names as ``RWAClient``."""

    source = "live"

    def __init__(
        self,
        api_key: str | None = None,
        session: requests.Session | None = None,
        *,
        allowlist_path: Path | None = None,
        max_retries: int = MAX_RETRIES,
        max_wait_seconds: float = MAX_WAIT_SECONDS,
        quote_ttl: float = QUOTE_TTL_SECONDS,
        directory_ttl: float = DIRECTORY_TTL_SECONDS,
        per_minute: int = CALLS_PER_MINUTE,
        per_day: int = CALLS_PER_DAY,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        now_iso: Callable[[], str] | None = None,
    ) -> None:
        raw = api_key if api_key is not None else os.getenv(DEMO_KEY_ENV, "")
        self._demo_key = str(raw or "").strip()
        self.keyless = not self._demo_key
        self.session = session or requests.Session()
        self.max_retries = max(0, int(max_retries))
        self.max_wait_seconds = float(max_wait_seconds)
        self.quote_ttl = float(quote_ttl)
        self.directory_ttl = float(directory_ttl)
        self._sleep = sleeper or time.sleep
        self._clock = clock or time.monotonic
        self._now_iso = now_iso or _utc_now
        self._budget = _Budget(per_minute=per_minute, per_day=per_day, clock=self._clock)
        self._allowlist_path = Path(allowlist_path) if allowlist_path else ALLOWLIST_PATH
        self._allowlist: list[dict[str, Any]] | None = None
        self._directory_at: float | None = None
        self._quotes: dict[str, dict[str, Any]] | None = None
        self._quotes_at: float | None = None
        self._quotes_as_of: str | None = None
        self._quotes_stale = False
        self._total_failure_at: float | None = None
        self._journal = _CallJournal(self.source)
        self.last_error = ""
        self.class_coverage_note = ""

    def __repr__(self) -> str:
        return f"CoinGeckoClient(keyless={self.keyless})"

    def begin_run(self) -> None:
        self._journal.begin_run()

    def call_log(self) -> list[dict[str, Any]]:
        return self._journal.call_log()

    def provider_notice(self) -> str:
        parts: list[str] = []
        if self.keyless:
            parts.append(KEYLESS_NOTE)
        if self._quotes_stale and self._quotes_as_of:
            parts.append(f"as of {self._quotes_as_of}")
        elif self._quotes_as_of and self._quotes is not None:
            parts.append(f"as of {self._quotes_as_of}")
        return " ".join(parts)

    def _record(self, endpoint: str, *, via: str, cached: bool = False) -> None:
        self._journal.record(endpoint, via=via, cached=cached)

    def _headers(self, host: str) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if host == "coingecko" and self._demo_key:
            headers[DEMO_KEY_HEADER] = self._demo_key
        return headers

    def _fail(self, message: str) -> CoinGeckoError:
        return CoinGeckoError(_redact(message, self._demo_key))

    def _backoff(self, attempt: int, retry_after: float | None, remaining: float) -> float:
        if retry_after is not None:
            return min(max(retry_after, 0.0), remaining, self.max_wait_seconds)
        base = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
        return min(base, remaining, self.max_wait_seconds)

    def _request(self, url: str, params: dict[str, Any], *, host: str) -> Any:
        """GET with bounded retries. Raises ``CoinGeckoError`` without the key."""
        waited = 0.0
        last_status = 0
        saw_transport = False
        for attempt in range(self.max_retries + 1):
            if not self._budget.allow():
                raise self._fail("Market-data call budget is exhausted for this process.")
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=self._headers(host),
                    timeout=min(5.0, max(self.max_wait_seconds - waited, 0.5)),
                )
            except requests.RequestException:
                saw_transport = True
                last_status = 0
                response = None
            if response is not None and response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    saw_transport = True
            elif response is not None:
                last_status = response.status_code
                if response.status_code != 429 and response.status_code < 500:
                    raise self._fail(f"{host} request failed (HTTP {response.status_code}).")
            if attempt >= self.max_retries or waited >= self.max_wait_seconds:
                break
            remaining = self.max_wait_seconds - waited
            header = None
            if response is not None and response.status_code == 429:
                header = parse_retry_after(response.headers.get("Retry-After"))
            delay = self._backoff(attempt, header, remaining)
            if delay > 0:
                self._sleep(delay)
                waited += delay
        if saw_transport and last_status == 0:
            raise self._fail(f"{host} request failed (network error).")
        raise self._fail(
            f"{host} request failed (HTTP {last_status or 'error'})."
        )

    def _allowlist_rows(self) -> list[dict[str, Any]]:
        now = self._clock()
        if (
            self._allowlist is not None
            and self._directory_at is not None
            and (now - self._directory_at) <= self.directory_ttl
        ):
            return copy.deepcopy(self._allowlist)
        rows = load_allowlist(self._allowlist_path)
        self._allowlist = rows
        self._directory_at = now
        return copy.deepcopy(rows)

    def _by_id(self) -> dict[int, dict[str, Any]]:
        return {int(row["rwa_id"]): row for row in self._allowlist_rows()}

    def _by_symbol(self) -> dict[str, dict[str, Any]]:
        return {row["symbol"]: row for row in self._allowlist_rows()}

    def _cg_metrics(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "price": _optional_float(row.get("current_price")),
            "market_cap": _optional_float(row.get("market_cap")),
            "volume_24h": _optional_float(row.get("total_volume")),
            "circulating_supply": _optional_float(row.get("circulating_supply")),
            "percent_change_24h": _optional_float(
                row.get("price_change_percentage_24h_in_currency")
                if row.get("price_change_percentage_24h_in_currency") is not None
                else row.get("price_change_percentage_24h")
            ),
            "last_updated": row.get("last_updated"),
        }

    def _parse_cmc_quotes(self, payload: Any) -> dict[str, dict[str, Any]]:
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data if isinstance(data, list) else []
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            quotes = row.get("quote") or []
            usd = next(
                (
                    quote
                    for quote in quotes
                    if isinstance(quote, dict) and str(quote.get("symbol") or "").upper() == "USD"
                ),
                None,
            )
            if not symbol or not isinstance(usd, dict):
                continue
            out[symbol] = {
                "price": _optional_float(usd.get("price")),
                "market_cap": _optional_float(usd.get("market_cap")),
                "volume_24h": _optional_float(usd.get("volume_24h")),
            }
        return out

    def _fetch_coingecko(self, ids: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for start in range(0, len(ids), IDS_PER_REQUEST):
            batch = ids[start : start + IDS_PER_REQUEST]
            params = {
                "vs_currency": "usd",
                "ids": ",".join(batch),
                "price_change_percentage": "24h",
                "per_page": IDS_PER_REQUEST,
                "page": 1,
            }
            payload = self._request(f"{COINGECKO_BASE}{MARKETS_PATH}", params, host="coingecko")
            if not isinstance(payload, list):
                raise self._fail("CoinGecko /coins/markets did not return a list.")
            rows.extend(item for item in payload if isinstance(item, dict))
        return rows

    def _fetch_cmc_keyless(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        if not symbols:
            return {}
        params = {"symbol": ",".join(symbols)}
        payload = self._request(CMC_KEYLESS_QUOTES_URL, params, host="cmc")
        return self._parse_cmc_quotes(payload)

    def _annotate(
        self,
        metrics_by_id: dict[str, dict[str, Any]],
        *,
        source: str,
        other_by_symbol: dict[str, float | None] | None = None,
    ) -> dict[str, dict[str, Any]]:
        allowlist = self._allowlist_rows()
        positive: list[float] = []
        for row in allowlist:
            metrics = metrics_by_id.get(row["coingecko_id"]) or {}
            mcap = _optional_float(metrics.get("market_cap"))
            if mcap is not None and mcap > 0:
                positive.append(mcap)
        total = sum(positive) if positive else None
        stamped = self._now_iso()
        annotated: dict[str, dict[str, Any]] = {}
        feeding = 0.0
        for row in allowlist:
            metrics = dict(metrics_by_id.get(row["coingecko_id"]) or {})
            other = None
            if other_by_symbol is not None:
                other = other_by_symbol.get(row["symbol"])
            reasons = assess_market_row(
                metrics,
                allowlist_total=total,
                other_market_cap=other,
            )
            mcap = _optional_float(metrics.get("market_cap"))
            if not reasons and mcap is not None and mcap > 0:
                feeding += mcap
            annotated[row["coingecko_id"]] = {
                **row,
                **metrics,
                "market_source": source,
                "as_of": stamped,
                "stale": False,
                "unverified": bool(reasons),
                "unverified_reasons": reasons,
                "score_feeding": not reasons,
            }
        self._score_feeding_total = feeding
        return annotated

    def _ensure_quotes(self) -> dict[str, dict[str, Any]]:
        now = self._clock()
        if (
            self._quotes is not None
            and self._quotes_at is not None
            and not self._quotes_stale
            and (now - self._quotes_at) <= self.quote_ttl
        ):
            self._record(MARKETS_PATH, via="cache", cached=True)
            return self._quotes
        if (
            self._total_failure_at is not None
            and 0.0 <= (now - self._total_failure_at) < COOLDOWN_SECONDS
        ):
            return self._serve_stale_or_empty()
        allowlist = self._allowlist_rows()
        ids = [row["coingecko_id"] for row in allowlist]
        symbols = [row["symbol"] for row in allowlist]
        try:
            raw_rows = self._fetch_coingecko(ids)
            by_id = {str(row.get("id") or ""): self._cg_metrics(row) for row in raw_rows}
            annotated = self._annotate(by_id, source="coingecko")
            self._quotes = annotated
            self._quotes_at = now
            self._quotes_as_of = self._now_iso()
            self._quotes_stale = False
            self._total_failure_at = None
            self.last_error = ""
            for row in annotated.values():
                row["as_of"] = self._quotes_as_of
            self._record(MARKETS_PATH, via="network")
            return annotated
        except CoinGeckoError as exc:
            self.last_error = str(exc)
        stale_cg = self._quotes
        try:
            cmc = self._fetch_cmc_keyless(symbols)
            metrics_by_id: dict[str, dict[str, Any]] = {}
            other: dict[str, float | None] = {}
            for row in allowlist:
                quote = cmc.get(row["symbol"]) or {}
                metrics_by_id[row["coingecko_id"]] = {
                    "price": quote.get("price"),
                    "market_cap": quote.get("market_cap"),
                    "volume_24h": quote.get("volume_24h"),
                    "circulating_supply": None,
                    "percent_change_24h": None,
                    "last_updated": None,
                }
                if stale_cg and row["coingecko_id"] in stale_cg:
                    other[row["symbol"]] = _optional_float(
                        stale_cg[row["coingecko_id"]].get("market_cap")
                    )
            annotated = self._annotate(
                metrics_by_id,
                source="cmc_keyless",
                other_by_symbol=other or None,
            )
            self._quotes = annotated
            self._quotes_at = now
            self._quotes_as_of = self._now_iso()
            self._quotes_stale = False
            self._total_failure_at = None
            for row in annotated.values():
                row["as_of"] = self._quotes_as_of
            self._record(CMC_KEYLESS_QUOTES_URL, via="network")
            return annotated
        except CoinGeckoError as exc:
            self.last_error = str(exc)
        self._total_failure_at = self._clock()
        return self._serve_stale_or_empty()

    def _serve_stale_or_empty(self) -> dict[str, dict[str, Any]]:
        """Last cached book marked stale, or empty. No network, no sleep."""
        if self._quotes is not None:
            self._quotes_stale = True
            for row in self._quotes.values():
                row["stale"] = True
            self._record(MARKETS_PATH, via="cache", cached=True)
            return self._quotes
        self._quotes_stale = True
        self._quotes_as_of = self._quotes_as_of or self._now_iso()
        return {}

    def _quote_for(self, row: dict[str, Any]) -> dict[str, Any]:
        book = self._ensure_quotes()
        found = book.get(row["coingecko_id"])
        if found is None:
            return {
                **row,
                "price": None,
                "market_cap": None,
                "volume_24h": None,
                "market_source": "coingecko",
                "as_of": self._quotes_as_of,
                "stale": True,
                "unverified": True,
                "unverified_reasons": ["market_data_unavailable"],
            }
        return copy.deepcopy(found)

    def _directory_row(self, row: dict[str, Any], quote: dict[str, Any] | None = None) -> dict[str, Any]:
        asset_class = row.get("asset_class") or "unknown"
        mapped = asset_class if asset_class in ASSET_TYPES else ""
        return {
            "name": row.get("name") or "",
            "symbol": row.get("symbol") or "",
            "slug": row.get("coingecko_id") or "",
            "rwa_id": row.get("rwa_id"),
            "asset_type": mapped,
            "asset_class": asset_class,
            "asset_class_status": NOT_AVAILABLE if asset_class == "unknown" else "",
            "rwa_rank": None,
            "rwa_rank_status": NOT_AVAILABLE,
            "has_tokens": False,
            "industry": "",
            "average_tokenized_price": None if quote is None else quote.get("price"),
            "tokenized_market_cap": None if quote is None else quote.get("market_cap"),
            "tokenized_volume_24h": None if quote is None else quote.get("volume_24h"),
            "unverified": True if quote is None else bool(quote.get("unverified")),
            "unverified_reasons": (
                ["market_data_unavailable"]
                if quote is None
                else list(quote.get("unverified_reasons") or [])
            ),
            "market_source": None if quote is None else quote.get("market_source"),
            "as_of": None if quote is None else quote.get("as_of"),
        }

    def rwa_map(
        self,
        symbol: str | None = None,
        *,
        asset_type: str | None = None,
        start: int = 1,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        kind = (asset_type or "").strip().lower()
        if kind and kind not in ASSET_TYPES:
            raise CMCError(f"asset_type must be one of {', '.join(ASSET_TYPES)}")
        rows = self._allowlist_rows()
        self.class_coverage_note = ""
        if kind:
            matched = [row for row in rows if row.get("asset_class") == kind]
            if not matched:
                self.class_coverage_note = NOT_AVAILABLE
            rows = matched
        if symbol:
            wanted = {part.strip().upper() for part in symbol.split(",") if part.strip()}
            rows = [row for row in rows if row["symbol"] in wanted]
        if limit is not None:
            size = max(1, min(int(limit), IDS_PER_REQUEST))
            offset = max(0, int(start) - 1)
            total = len(rows)
            rows = rows[offset : offset + size]
            self._directory_page = {
                "truncated": offset + len(rows) < total,
                "total_size": total,
            }
        self._record("allowlist", via="cache", cached=True)
        return [self._directory_row(row) for row in rows]

    def rwa_info(self, rwa_id: int) -> dict[str, Any]:
        row = self._by_id().get(int(rwa_id))
        self._record("rwa_info", via="cache", cached=True)
        if row is None:
            return {}
        return {
            "rwa_id": row["rwa_id"],
            "name": row["name"],
            "symbol": row["symbol"],
            "slug": row["coingecko_id"],
            "asset_type": "",
            "asset_class": row["asset_class"],
            "cik": None,
            "cik_status": NOT_AVAILABLE,
            "holders": None,
            "holders_status": NOT_AVAILABLE,
            "issuer": {"name": "", "backing_status": NOT_AVAILABLE},
            "issuer_backing_status": NOT_AVAILABLE,
            "chain": row.get("chain"),
            "contract_address": row.get("contract_address"),
            "unavailable_fields": list(UNAVAILABLE_FIELDS),
        }

    def issuers_list(self) -> list[dict[str, Any]]:
        self._record("issuers", via="cache", cached=True)
        return []

    def issuer(self, issuer_id: str) -> dict[str, Any]:
        self._record("issuer", via="cache", cached=True)
        return {
            "issuer_id": issuer_id,
            "name": "",
            "backing_status": NOT_AVAILABLE,
            "tokens": [],
        }

    def crypto_quote(self, crypto_id: int) -> dict[str, Any]:
        row = self._by_id().get(int(crypto_id))
        self._record(MARKETS_PATH, via="cache", cached=True)
        if row is None:
            return {}
        quote = self._quote_for(row)
        return {
            "id": crypto_id,
            "quote": {
                "USD": {
                    "price": quote.get("price"),
                    "market_cap": quote.get("market_cap"),
                    "volume_24h": quote.get("volume_24h"),
                    "percent_change_24h": quote.get("percent_change_24h"),
                }
            },
            "market_source": quote.get("market_source"),
            "as_of": quote.get("as_of"),
            "stale": bool(quote.get("stale")),
            "unverified": bool(quote.get("unverified")),
        }

    def rwa_quotes(
        self,
        *,
        rwa_id: int | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        if rwa_id is None and not symbol:
            raise CMCError("rwa_quotes requires rwa_id or symbol")
        if rwa_id is not None and symbol:
            raise CMCError("rwa_quotes accepts only one of rwa_id or symbol")
        row = self._by_id().get(int(rwa_id)) if rwa_id is not None else self._by_symbol().get(
            str(symbol or "").upper()
        )
        if row is None:
            return parse_rwa_quotes_payload({})
        quote = self._quote_for(row)
        payload = {
            "rwa_id": row["rwa_id"],
            "name": row["name"],
            "symbol": row["symbol"],
            "slug": row["coingecko_id"],
            "asset_type": "",
            "rwa_rank": None,
            "has_tokens": False,
            "average_tokenized_price": quote.get("price"),
            "tokenized_market_cap": quote.get("market_cap"),
            "tokenized_volume_24h": quote.get("volume_24h"),
            "quotes": [],
            "tokens": [],
            "tradfi_markets": [],
        }
        parsed = parse_rwa_quotes_payload(payload)
        parsed["rwa_rank_status"] = NOT_AVAILABLE
        parsed["tradfi_markets_status"] = NOT_AVAILABLE
        parsed["holders_status"] = NOT_AVAILABLE
        parsed["issuer_backing_status"] = NOT_AVAILABLE
        parsed["cik_status"] = NOT_AVAILABLE
        parsed["asset_class_status"] = NOT_AVAILABLE
        parsed["market_source"] = quote.get("market_source")
        parsed["as_of"] = quote.get("as_of")
        parsed["stale"] = bool(quote.get("stale"))
        parsed["unverified"] = bool(quote.get("unverified"))
        parsed["unverified_reasons"] = list(quote.get("unverified_reasons") or [])
        parsed["percent_change_24h"] = quote.get("percent_change_24h")
        parsed["last_updated"] = quote.get("last_updated") or quote.get("as_of")
        return parsed

    def _listed_rows(self) -> list[dict[str, Any]]:
        quotes = self._ensure_quotes()
        rows = []
        for row in self._allowlist_rows():
            quote = quotes.get(row["coingecko_id"])
            rows.append(self._directory_row(row, quote))
        rows.sort(key=lambda item: item["symbol"])
        return rows

    def assets_list(
        self,
        *,
        asset_type: str | None = None,
        start: int = 1,
        limit: int = 250,
        sort: str = "rwa_rank",
        sort_dir: str = "asc",
    ) -> dict[str, Any]:
        kind = (asset_type or "").strip().lower()
        if kind and kind not in ASSET_TYPES:
            raise CMCError(f"asset_type must be one of {', '.join(ASSET_TYPES)}")
        rows = self._listed_rows()
        if kind:
            rows = [row for row in rows if row.get("asset_class") == kind]
            if not rows:
                self.class_coverage_note = NOT_AVAILABLE
        reverse = sort_dir.lower() == "desc"
        if sort == "symbol":
            rows = sorted(rows, key=lambda item: item["symbol"], reverse=reverse)
        elif sort in {"tokenized_market_cap", "tokenized_volume_24h", "average_tokenized_price"}:
            rows = sorted(
                rows,
                key=lambda item: _optional_float(item.get(sort)) or 0.0,
                reverse=reverse,
            )
        else:
            rows = sorted(rows, key=lambda item: item["symbol"], reverse=reverse)
        page = max(1, int(start))
        size = max(1, min(int(limit), IDS_PER_REQUEST))
        offset = page - 1
        sliced = rows[offset : offset + size]
        parsed = parse_assets_list_payload(
            {
                "rwa_assets": sliced,
                "total_size": len(rows),
                "has_more": offset + size < len(rows),
            }
        )
        by_symbol = {row["symbol"]: row for row in sliced}
        for asset in parsed["rwa_assets"]:
            src = by_symbol.get(asset["symbol"])
            if src is None:
                continue
            asset["unverified"] = bool(src.get("unverified"))
            asset["unverified_reasons"] = list(src.get("unverified_reasons") or [])
            if src.get("market_source"):
                asset["market_source"] = src.get("market_source")
            if src.get("as_of"):
                asset["as_of"] = src.get("as_of")
        parsed["rwa_rank_status"] = NOT_AVAILABLE
        return parsed

    def assets_list_all(
        self,
        *,
        asset_type: str | None = None,
        sort: str = "rwa_rank",
        sort_dir: str = "asc",
    ) -> dict[str, Any]:
        page = self.assets_list(
            asset_type=asset_type,
            start=1,
            limit=IDS_PER_REQUEST,
            sort=sort,
            sort_dir=sort_dir,
        )
        return {
            "rwa_assets": list(page.get("rwa_assets") or []),
            "total_size": page.get("total_size") or len(page.get("rwa_assets") or []),
            "has_more": False,
            "rwa_rank_status": NOT_AVAILABLE,
        }

    def market_pairs(
        self,
        *,
        rwa_id: int | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        if rwa_id is None and not symbol:
            raise CMCError("market_pairs requires rwa_id or symbol")
        if rwa_id is not None and symbol:
            raise CMCError("market_pairs accepts only one of rwa_id or symbol")
        row = self._by_id().get(int(rwa_id)) if rwa_id is not None else self._by_symbol().get(
            str(symbol or "").upper()
        )
        parsed = parse_market_pairs_payload(
            {
                "rwa_id": None if row is None else row["rwa_id"],
                "name": "" if row is None else row["name"],
                "symbol": "" if row is None else row["symbol"],
                "num_market_pairs": 0,
                "market_pairs": [],
            }
        )
        parsed["plan_blocked"] = False
        parsed["unavailable"] = True
        parsed["available"] = False
        parsed["unavailable_reason"] = NOT_AVAILABLE
        parsed["error_message"] = "Venue names and market pairs are not available from this source."
        self._record("market_pairs", via="cache", cached=True)
        return parsed

    def score_feeding_market_cap_total(self) -> float:
        self._ensure_quotes()
        return float(getattr(self, "_score_feeding_total", 0.0))
