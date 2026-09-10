"""Chainlink Proof of Reserve reads for Backed / xStocks assets.

On-chain ``AggregatorV3Interface.latestRoundData()`` via JSON-RPC ``eth_call``.
No web3 dependency — ``requests`` only. RPC URLs come from env
(``POLYGON_RPC_URL`` / ``BASE_RPC_URL`` / ``ETH_RPC_URL``). Public no-key
fallback endpoints are used only when those vars are unset. Never hardcode keys.

Feed addresses are the public Chainlink proxy contracts Backed already
publishes (Polygon today). Source:
https://docs.chain.link/data-feeds/smartdata/addresses
https://reference-data-directory.vercel.app/feeds-matic-mainnet.json
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests

CHAINLINK_SMARTDATA_DOCS = "https://docs.chain.link/data-feeds/smartdata/addresses"
CHAINLINK_REFERENCE_MATIC = (
    "https://reference-data-directory.vercel.app/feeds-matic-mainnet.json"
)

# AggregatorV3Interface selectors (first 4 bytes of keccak256).
LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"
DECIMALS_SELECTOR = "0x313ce567"
TOTAL_SUPPLY_SELECTOR = "0x18160ddd"

RPC_TIMEOUT_SECONDS = 12.0

# Public no-key JSON-RPC endpoints. Override with env in production.
PUBLIC_RPC_FALLBACKS: dict[str, tuple[str, ...]] = {
    "polygon": ("https://polygon-bor-rpc.publicnode.com", "https://polygon-rpc.com"),
    "base": ("https://mainnet.base.org",),
    "ethereum": ("https://cloudflare-eth.com",),
}

RPC_ENV_VARS: dict[str, tuple[str, ...]] = {
    "polygon": ("POLYGON_RPC_URL", "MATIC_RPC_URL"),
    "base": ("BASE_RPC_URL",),
    "ethereum": ("ETH_RPC_URL", "ETHEREUM_RPC_URL"),
}


@dataclass(frozen=True)
class PorFeed:
    """One public Chainlink PoR aggregator proxy."""

    symbol: str
    name: str
    chain: str
    proxy: str
    decimals: int
    unit: str
    token_address: str | None
    token_decimals: int
    docs: str
    aliases: tuple[str, ...]


# Public Backed Finance PoR proxies on Polygon (Chainlink SmartData directory).
# token_address is the ERC-20 Backed uses across EVM chains when known.
BACKED_POR_FEEDS: tuple[PorFeed, ...] = (
    PorFeed(
        symbol="bNVDA",
        name="bNVDA Reserves / Proof of Reserves",
        chain="polygon",
        proxy="0x0fB2beD999da86Cb1Fdd97E746600A96141EeA09",
        decimals=8,
        unit="NVDA",
        token_address="0xa34c5e0AbE843E10461E2C9586Ea03E55DbCc495",
        token_decimals=18,
        docs=CHAINLINK_SMARTDATA_DOCS,
        aliases=("NVDA", "NVDAX", "BNVDA"),
    ),
    PorFeed(
        symbol="bIB01",
        name="bIB01 Reserves",
        chain="polygon",
        proxy="0xad4395fc414Fc1575A7a38C20B0Bfdbdb09ee41A",
        decimals=8,
        unit="IB01",
        token_address="0xCA30c93B02514f86d5C86a6e375E3A330B435Fb5",
        token_decimals=18,
        docs=CHAINLINK_SMARTDATA_DOCS,
        aliases=("IB01", "BIB01"),
    ),
    PorFeed(
        symbol="bCSPX",
        name="bCSPX Reserves",
        chain="polygon",
        proxy="0x55e75d35c44A9EE1A5b05416640965EbcA4a8D33",
        decimals=8,
        unit="CSPX",
        token_address=None,
        token_decimals=18,
        docs=CHAINLINK_SMARTDATA_DOCS,
        aliases=("CSPX", "BCSPX"),
    ),
    PorFeed(
        symbol="bC3M",
        name="bC3M Reserves",
        chain="polygon",
        proxy="0x648E0fF6A36D58F6FCE5927cB77601b73cAdc2Af",
        decimals=8,
        unit="C3M",
        token_address=None,
        token_decimals=18,
        docs=CHAINLINK_SMARTDATA_DOCS,
        aliases=("C3M", "BC3M"),
    ),
    PorFeed(
        symbol="bIBTA",
        name="bIBTA Reserves",
        chain="polygon",
        proxy="0x4517002fCD31062Ea38680dF9Ee37f29528C2707",
        decimals=8,
        unit="IBTA",
        token_address=None,
        token_decimals=18,
        docs=CHAINLINK_SMARTDATA_DOCS,
        aliases=("IBTA", "BIBTA"),
    ),
)

# Canonical feed used by GET /health (Backed + Chainlink announcement pair).
HEALTH_POR_FEED = BACKED_POR_FEEDS[1]  # bIB01


@dataclass(frozen=True)
class LatestRound:
    """Decoded AggregatorV3Interface.latestRoundData()."""

    round_id: int
    answer: int
    started_at: int
    updated_at: int
    answered_in_round: int


@dataclass
class PorReading:
    """Normalized reserve reading used by the Backed verifier."""

    feed: PorFeed
    reserves: float
    circulating: float | None
    round_id: int
    updated_at: int
    rpc_url: str


def _strip_hex(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("0x") or text.startswith("0X"):
        return text[2:]
    return text


def decode_uint256(data: str, word: int = 0) -> int:
    raw = _strip_hex(data)
    start = word * 64
    chunk = raw[start : start + 64]
    if len(chunk) < 64:
        raise ValueError(f"ABI word {word} missing from eth_call result")
    return int(chunk, 16)


def decode_int256(data: str, word: int = 0) -> int:
    val = decode_uint256(data, word)
    if val >= 1 << 255:
        val -= 1 << 256
    return val


def decode_latest_round(data: str) -> LatestRound:
    """Decode the 5-word ``latestRoundData`` ABI return."""
    return LatestRound(
        round_id=decode_uint256(data, 0),
        answer=decode_int256(data, 1),
        started_at=decode_uint256(data, 2),
        updated_at=decode_uint256(data, 3),
        answered_in_round=decode_uint256(data, 4),
    )


def scale_answer(answer: int, decimals: int) -> float:
    if decimals < 0:
        raise ValueError("decimals must be >= 0")
    return float(answer) / float(10**decimals)


def normalize_ticker(ticker: str) -> str:
    return (ticker or "").strip().upper()


def resolve_por_feed(ticker: str, feeds: tuple[PorFeed, ...] | None = None) -> PorFeed | None:
    """Map NVDA / NVDAx / bNVDA (etc.) onto a published Chainlink PoR feed."""
    key = normalize_ticker(ticker)
    if not key:
        return None
    catalog = feeds if feeds is not None else BACKED_POR_FEEDS
    for feed in catalog:
        names = {feed.symbol.upper(), *feed.aliases}
        if key in names:
            return feed
    return None


def rpc_urls_for_chain(chain: str) -> list[str]:
    """Env URLs first, then public no-key fallbacks. Deduped, no secrets."""
    urls: list[str] = []
    for name in RPC_ENV_VARS.get(chain, ()):
        raw = os.getenv(name, "").strip()
        if raw:
            urls.append(raw.rstrip("/"))
    for fallback in PUBLIC_RPC_FALLBACKS.get(chain, ()):
        if fallback not in urls:
            urls.append(fallback)
    return urls


def eth_call_payload(to: str, data: str, *, request_id: int = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }


def parse_rpc_result(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        raise RuntimeError("RPC response was not a JSON object")
    if payload.get("error"):
        err = payload["error"]
        raise RuntimeError(f"RPC error: {err}")
    result = payload.get("result")
    if not isinstance(result, str) or not result or result == "0x":
        raise RuntimeError("RPC eth_call returned empty result")
    return result


class ChainlinkPorClient:
    """JSON-RPC reader for Chainlink PoR aggregators."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        timeout: float = RPC_TIMEOUT_SECONDS,
        rpc_urls: dict[str, list[str]] | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self._rpc_urls = rpc_urls

    def urls_for(self, chain: str) -> list[str]:
        if self._rpc_urls is not None:
            return list(self._rpc_urls.get(chain) or [])
        return rpc_urls_for_chain(chain)

    def eth_call(self, chain: str, to: str, data: str) -> tuple[str, str]:
        """Return ``(rpc_url, hex_result)``. Tries each configured URL."""
        urls = self.urls_for(chain)
        if not urls:
            raise RuntimeError(f"No RPC URL configured for chain {chain}")
        errors: list[str] = []
        payload = eth_call_payload(to, data)
        for url in urls:
            try:
                resp = self.session.post(
                    url,
                    json=payload,
                    timeout=self.timeout,
                    headers={"Content-Type": "application/json"},
                )
                status = int(getattr(resp, "status_code", 0) or 0)
                if status and not (200 <= status < 300):
                    raise RuntimeError(f"HTTP {status}: {(getattr(resp, 'text', '') or '')[:180]}")
                body = resp.json()
                return url, parse_rpc_result(body if isinstance(body, dict) else None)
            except Exception as exc:  # noqa: BLE001 — try next RPC
                errors.append(f"{url}: {exc}")
        raise RuntimeError("; ".join(errors))

    def latest_round(self, feed: PorFeed) -> tuple[str, LatestRound]:
        url, raw = self.eth_call(feed.chain, feed.proxy, LATEST_ROUND_DATA_SELECTOR)
        return url, decode_latest_round(raw)

    def token_total_supply(self, feed: PorFeed) -> float | None:
        if not feed.token_address:
            return None
        try:
            _, raw = self.eth_call(feed.chain, feed.token_address, TOTAL_SUPPLY_SELECTOR)
            return scale_answer(decode_uint256(raw, 0), feed.token_decimals)
        except Exception:  # noqa: BLE001 — supply is optional for the oracle path
            return None

    def read(self, ticker: str, *, feeds: tuple[PorFeed, ...] | None = None) -> PorReading:
        feed = resolve_por_feed(ticker, feeds)
        if feed is None:
            raise RuntimeError(f"No published Chainlink PoR feed for {ticker}")
        rpc_url, round_data = self.latest_round(feed)
        if round_data.answer < 0:
            raise RuntimeError(f"Chainlink PoR {feed.symbol} answer was negative")
        if round_data.updated_at <= 0:
            raise RuntimeError(f"Chainlink PoR {feed.symbol} updatedAt was {round_data.updated_at}")
        reserves = scale_answer(round_data.answer, feed.decimals)
        circulating = self.token_total_supply(feed)
        return PorReading(
            feed=feed,
            reserves=reserves,
            circulating=circulating,
            round_id=round_data.round_id,
            updated_at=round_data.updated_at,
            rpc_url=rpc_url,
        )


def probe_chainlink_por(
    *,
    poster: Any | None = None,
    timeout: float = 2.0,
    feed: PorFeed | None = None,
) -> bool:
    """True when the health feed's ``latestRoundData`` is readable. Never raises."""
    target = feed or HEALTH_POR_FEED
    post = poster or requests.post
    payload = eth_call_payload(target.proxy, LATEST_ROUND_DATA_SELECTOR)
    for url in rpc_urls_for_chain(target.chain):
        try:
            resp = post(
                url,
                json=payload,
                timeout=timeout,
                headers={"Content-Type": "application/json"},
            )
            status = int(getattr(resp, "status_code", 0) or 0)
            if status and not (200 <= status < 300):
                continue
            raw = parse_rpc_result(resp.json())
            decoded = decode_latest_round(raw)
            if decoded.answer >= 0 and decoded.updated_at > 0:
                return True
        except Exception:  # noqa: BLE001 — health must stay up
            continue
    return False
