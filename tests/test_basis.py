"""Unit tests for cross-issuer basis (wrapper spread) math and scoring."""

from __future__ import annotations

from rwa_score.scorer import (
    BASIS_ERROR_SCORE,
    BASIS_SCORE_FLOOR,
    MISSING_BASIS_SCORE,
    SINGLE_WRAPPER_BASIS_SCORE,
    TransparencyScorer,
    basis_score_from_spread,
    group_wrapper_quotes,
    pair_usd_price,
    percent_spread,
)
from tests.conftest import RecordingClient


def _pair(crypto_id: int, symbol: str, price: float, volume: float = 0.0) -> dict:
    return {
        "market_pair": f"{symbol}/USDT",
        "market_pair_base": {"crypto_id": crypto_id, "symbol": symbol},
        "quotes": [{"symbol": "USD", "price": price, "volume_24h": volume}],
    }


def test_percent_spread_needs_two_prices() -> None:
    assert percent_spread([]) is None
    assert percent_spread([10.0]) is None
    assert percent_spread([0.0, 5.0]) is None


def test_percent_spread_is_range_over_mid() -> None:
    # (102 - 100) / 101 * 100 ≈ 1.980
    spread = percent_spread([100.0, 102.0])
    assert spread is not None
    assert abs(spread - (2.0 / 101.0 * 100.0)) < 1e-9


def test_basis_score_from_spread_formula() -> None:
    assert basis_score_from_spread(0.0) == 100.0
    assert basis_score_from_spread(0.5) == 95.0
    assert basis_score_from_spread(5.0) == 50.0
    assert basis_score_from_spread(8.5) == BASIS_SCORE_FLOOR
    assert basis_score_from_spread(20.0) == BASIS_SCORE_FLOOR
    assert basis_score_from_spread(-1.0) == 90.0


def test_pair_usd_price_reads_quotes_and_reported() -> None:
    assert pair_usd_price({"quotes": [{"symbol": "USD", "price": "12.5"}]}) == 12.5
    assert (
        pair_usd_price({"exchange_reported_quotes": [{"symbol": "USD", "price": 9.0}]})
        == 9.0
    )
    assert pair_usd_price({"quote": {"USD": {"price": 3.25}}}) == 3.25
    assert pair_usd_price({"quotes": [{"symbol": "EUR", "price": 1.0}]}) is None


def test_group_wrapper_quotes_collapses_venues() -> None:
    pairs = [
        _pair(1, "NVDAx", 100.0, 80.0),
        _pair(1, "NVDAx", 110.0, 20.0),  # same wrapper, second venue
        _pair(2, "NVDAon", 105.0, 50.0),
    ]
    wrappers = group_wrapper_quotes(pairs)
    assert [w["crypto_id"] for w in wrappers] == [1, 2]
    # VWAP for crypto 1: (100*80 + 110*20) / 100 = 102
    by_id = {w["crypto_id"]: w for w in wrappers}
    assert abs(by_id[1]["price"] - 102.0) < 1e-9
    assert by_id[1]["venues"] == 2
    assert by_id[2]["price"] == 105.0


def test_group_wrapper_quotes_skips_unpriced_and_unidentified() -> None:
    pairs = [
        {"market_pair_base": {"symbol": "NOID"}, "quotes": [{"symbol": "USD", "price": 1}]},
        _pair(3, "BAD", 0.0, 10.0),
        {"market_pair_base": {"crypto_id": 4, "symbol": "OK"}},
    ]
    assert group_wrapper_quotes(pairs) == []


def test_fixture_nvda_has_tight_basis(fixture_scorer: TransparencyScorer) -> None:
    report = fixture_scorer.score("NVDA")
    assert "basis" in report["subscores"]
    assert "basis" in report["verification"]
    assert "basis" in report["explanations"]
    assert "basis" in report["weights"]
    assert report["verification"]["basis"]["level"] == "self-reported"
    assert report["verification"]["basis"]["source"] == "cmc_market_pairs"
    assert "CMC market-pairs" in report["verification"]["basis"]["evidence"]
    assert report["basis"]["available"] is True
    assert report["basis"]["wrapper_count"] == 3
    assert report["subscores"]["basis"] >= 90
    assert report["band"] == "GREEN"


def test_fixture_tsla_wide_basis_is_flagged(fixture_scorer: TransparencyScorer) -> None:
    report = fixture_scorer.score("TSLA")
    assert report["basis"]["available"] is True
    assert report["basis"]["wrapper_count"] == 2
    assert report["subscores"]["basis"] == BASIS_SCORE_FLOOR
    assert any("wrapper spread" in f.lower() for f in report["flags"])


def test_scorer_includes_sixth_pillar_when_pairs_missing() -> None:
    report = TransparencyScorer(RecordingClient()).score("NVDA")
    assert set(report["subscores"]) == set(report["weights"])
    assert "basis" in report["subscores"]
    assert report["subscores"]["basis"] == MISSING_BASIS_SCORE
    assert report["basis"]["available"] is False
    assert report["verification"]["basis"]["source"] == "cmc_market_pairs"
    assert any("market-pairs" in f.lower() for f in report["flags"])


def test_single_wrapper_uses_labeled_default() -> None:
    client = RecordingClient(
        market_pairs={
            2: {
                "rwa_id": 2,
                "symbol": "NVDA",
                "market_pairs": [_pair(99, "NVDAx", 100.0, 10.0)],
            }
        }
    )
    report = TransparencyScorer(client).score("NVDA")
    assert report["subscores"]["basis"] == SINGLE_WRAPPER_BASIS_SCORE
    assert report["basis"]["available"] is False
    assert report["basis"]["wrapper_count"] == 1
    assert "single wrapper" in report["explanations"]["basis"].lower()


def test_two_wrappers_score_from_spread() -> None:
    client = RecordingClient(
        market_pairs={
            2: {
                "rwa_id": 2,
                "symbol": "NVDA",
                "market_pairs": [
                    _pair(99, "NVDAx", 100.0, 10.0),
                    _pair(100, "NVDAon", 101.0, 10.0),
                ],
            }
        }
    )
    report = TransparencyScorer(client).score("NVDA")
    expected_spread = percent_spread([100.0, 101.0])
    assert expected_spread is not None
    assert report["basis"]["available"] is True
    assert abs(report["basis"]["percent_spread"] - expected_spread) < 1e-9
    assert report["subscores"]["basis"] == round(basis_score_from_spread(expected_spread), 1)
    assert report["verification"]["basis"]["ok"] is True


def test_market_pairs_error_is_not_silent() -> None:
    client = RecordingClient(market_pairs_error=RuntimeError("pairs down"))
    report = TransparencyScorer(client, use_live_verifiers=False).score("NVDA")
    assert report["subscores"]["basis"] == BASIS_ERROR_SCORE
    assert any("pairs down" in f for f in report["flags"])
    assert "error" in report["explanations"]["basis"].lower()
    # Other pillars still score — heuristic path unchanged.
    assert report["subscores"]["backing"] == 90.0
    assert report["subscores"]["reserves"] == 90.0
    assert report["subscores"]["redemption"] == 85.0
