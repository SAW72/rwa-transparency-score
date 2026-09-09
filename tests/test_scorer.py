from __future__ import annotations

import pytest

from rwa_score.scorer import (
    PILLARS,
    WEIGHTS,
    ScoreError,
    TransparencyScorer,
    band_code,
    band_detail,
)
from tests.conftest import RecordingClient


def test_weights_sum_to_one() -> None:
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9
    assert set(WEIGHTS) == set(PILLARS)


@pytest.mark.parametrize(
    "score,code",
    [(75, "GREEN"), (74.9, "YELLOW"), (50, "YELLOW"), (49.9, "ORANGE"), (25, "ORANGE"), (24.9, "RED")],
)
def test_band_thresholds(score: float, code: str) -> None:
    assert band_code(score) == code
    assert band_detail(score).startswith(code)


def test_fixture_nvda_is_green(fixture_scorer: TransparencyScorer) -> None:
    report = fixture_scorer.score("nvda")
    assert report["ticker"] == "NVDA"
    assert report["data_source"] == "fixture"
    assert report["band"] == "GREEN"
    assert report["score"] >= 75
    assert report["issuer"] == "Backed Finance"
    assert report["heuristics"]["backed"] is True
    assert report["heuristics"]["audited"] is True
    assert report["heuristics"]["redeemable"] is True
    assert report["heuristics"]["labeled"] is True
    assert report["cik"]
    assert any("heuristic" in n.lower() for n in report["notes"])
    assert any("fixture" in n.lower() or "DEMO FIXTURE" in n for n in report["notes"])
    assert "backing" in report["explanations"]
    assert "heuristic" in report["explanations"]["backing"].lower()


def test_fixture_tsla_is_thin_wrapper(fixture_scorer: TransparencyScorer) -> None:
    report = fixture_scorer.score("TSLA")
    assert report["band"] in {"ORANGE", "RED"}
    assert report["heuristics"]["backed"] is False
    assert any("CIK" in f for f in report["flags"])
    assert any("proof of reserves" in f.lower() for f in report["flags"])
    assert report["price"]["available"] is True
    assert abs(report["price"]["percent_change_24h"]) > 20


def test_fixture_aapl_is_mid_tier(fixture_scorer: TransparencyScorer) -> None:
    report = fixture_scorer.score("AAPL")
    assert report["heuristics"]["backed"] is True
    assert report["heuristics"]["audited"] is False
    assert report["heuristics"]["redeemable"] is True
    assert report["band"] in {"YELLOW", "GREEN"}
    assert any("heuristic" in f.lower() for f in report["flags"])


def test_unknown_ticker_raises(fixture_scorer: TransparencyScorer) -> None:
    with pytest.raises(ScoreError, match="not found"):
        fixture_scorer.score("NOTATICKER")


def test_empty_info_raises() -> None:
    client = RecordingClient(info={2: {}})
    scorer = TransparencyScorer(client)
    with pytest.raises(ScoreError, match="No RWA info"):
        scorer.score("NVDA")


def test_issuer_list_cached_across_scores(recording_client: RecordingClient) -> None:
    scorer = TransparencyScorer(recording_client)
    scorer.score("NVDA")
    scorer.score("NVDA")
    assert recording_client.calls["issuers_list"] == 1
    assert recording_client.calls["issuer"] == 1
    assert recording_client.calls["rwa_map"] == 1


def test_price_error_is_surfaced() -> None:
    client = RecordingClient(quote_error=RuntimeError("boom"))
    report = TransparencyScorer(client).score("NVDA")
    assert any("Quote lookup failed" in f or "boom" in f for f in report["flags"])
    assert report["price"]["available"] is False
    assert report["subscores"]["price"] == 50.0
    assert "error" in report["explanations"]["price"].lower()


def test_missing_crypto_id_is_flagged() -> None:
    client = RecordingClient(
        issuer_details={"abc": {"name": "Backed Finance", "tokens": [{"rwa_id": 2}]}},
    )
    report = TransparencyScorer(client).score("NVDA")
    assert any("crypto_id" in f for f in report["flags"])
    assert report["subscores"]["price"] == 45.0


def test_map_failure_is_not_silent() -> None:
    class BrokenMap(RecordingClient):
        def rwa_map(self, symbol=None):
            raise RuntimeError("map down")

    with pytest.raises(ScoreError, match="RWA map"):
        TransparencyScorer(BrokenMap()).score("NVDA")
