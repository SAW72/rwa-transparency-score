"""Honest fixture vs live-CMC labels. Unknown never becomes LIVE."""

from __future__ import annotations

FIXTURE_SOURCES = frozenset({"fixture", "fixtures", "demo", "demo_fixture"})
LIVE_CMC_SOURCES = frozenset({"live", "cmc", "coinmarketcap", "live_cmc"})


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
    """Short evidence label. Fixture/unknown never say live CoinMarketCap."""
    kind = source_kind(value)
    if kind == "fixture":
        return "bundled DEMO FIXTURES — not live CoinMarketCap"
    if kind == "live":
        return "live CoinMarketCap API"
    return "data source not confirmed — not labeled as live CoinMarketCap"
