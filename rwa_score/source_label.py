"""Honest fixture vs live market-data labels. Unknown never becomes LIVE."""

from __future__ import annotations

FIXTURE_SOURCES = frozenset({"fixture", "fixtures", "demo", "demo_fixture"})
LIVE_CMC_SOURCES = frozenset(
    {"live", "cmc", "coinmarketcap", "live_cmc", "coingecko", "live_coingecko"}
)
KEYLESS_CMC_SOURCES = frozenset({"cmc_keyless", "coinmarketcap_keyless"})


def source_kind(value: object) -> str:
    """Return ``fixture``, ``live``, or ``unknown``. Never guess live."""
    raw = str(value or "").strip().lower()
    if raw in FIXTURE_SOURCES:
        return "fixture"
    if raw in LIVE_CMC_SOURCES:
        return "live"
    return "unknown"


def is_fixture_source(value: object) -> bool:
    return source_kind(value) == "fixture"


def is_live_cmc_source(value: object) -> bool:
    return source_kind(value) == "live"


def source_honesty_label(value: object) -> str:
    """Short evidence label. Fixture/unknown never say live market data.

    The fixture string is locked by the published NVDA fixture hash.
    Live market data is CoinGecko. The keyless CMC quote fallback has its
    own label and is not described as CoinGecko.
    """
    raw = str(value or "").strip().lower()
    if raw in KEYLESS_CMC_SOURCES:
        return (
            "CoinMarketCap keyless public quotes "
            "(price, market cap, and volume only)"
        )
    kind = source_kind(value)
    if kind == "fixture":
        return "bundled DEMO FIXTURES — not live CoinMarketCap"
    if kind == "live":
        return "live CoinGecko API"
    return "data source not confirmed — not labeled as live CoinGecko"
