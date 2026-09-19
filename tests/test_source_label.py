"""Fixture vs live source labels. Unknown never becomes LIVE."""

from rwa_score.source_label import (
    is_fixture_source,
    is_live_cmc_source,
    source_honesty_label,
    source_kind,
)


def test_source_kind_maps_known_aliases() -> None:
    assert source_kind("fixture") == "fixture"
    assert source_kind("DEMO_FIXTURE") == "fixture"
    assert source_kind("live") == "live"
    assert source_kind("CMC") == "live"
    assert source_kind("coinmarketcap") == "live"
    assert source_kind("") == "unknown"
    assert source_kind(None) == "unknown"
    assert source_kind("mystery") == "unknown"


def test_unknown_is_never_live() -> None:
    assert not is_live_cmc_source("")
    assert not is_live_cmc_source("unknown")
    assert not is_live_cmc_source("nope")
    assert is_fixture_source("fixture")
    assert is_live_cmc_source("cmc")
    assert "not live" in source_honesty_label("fixture").lower()
    assert source_honesty_label("live") == "live CoinMarketCap API"
    assert "not labeled as live" in source_honesty_label("").lower()
    assert "live CoinMarketCap API" not in source_honesty_label("mystery")
