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
    assert "basis" in WEIGHTS
    assert abs(WEIGHTS["basis"] - 0.15) < 1e-9
    assert WEIGHTS["backing"] == 0.20
    assert WEIGHTS["reserves"] == 0.20
    assert WEIGHTS["redemption"] == 0.15
    assert WEIGHTS["price"] == 0.15
    assert WEIGHTS["disclosure"] == 0.15


@pytest.mark.parametrize(
    "score,code",
    [(75, "GREEN"), (74.9, "YELLOW"), (50, "YELLOW"), (49.9, "ORANGE"), (25, "ORANGE"), (24.9, "RED")],
)
def test_band_thresholds(score: float, code: str) -> None:
    assert band_code(score) == code
    assert band_detail(score).startswith(code)


def test_band_detail_wording_is_exact() -> None:
    assert band_detail(75) == "GREEN — heuristic: stronger transparency signals (still verify)"
    assert band_detail(50) == "YELLOW — heuristic: mixed signals; verify before relying"
    assert band_detail(25) == "ORANGE — heuristic: weaker signals; elevated concern"
    assert band_detail(0) == (
        "RED — heuristic: opaque or thin signals (not a finding of fraud or illegality)"
    )


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
    assert "basis" in report["subscores"]
    assert report["verification"]["basis"]["source"] == "cmc_market_pairs"
    assert report["basis"]["available"] is True


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


def test_adversarial_issuer_name_is_not_treated_as_backed() -> None:
    client = RecordingClient(
        info={2: {"symbol": "NVDA", "cik": "0001045810", "issuer": {"name": "Not Backed At All"}}},
        issuers=[{"issuer_id": "abc", "name": "Not Backed At All"}],
        issuer_details={
            "abc": {
                "name": "Not Backed At All",
                "tokens": [{"rwa_id": 2, "crypto_id": 99}],
            }
        },
    )
    report = TransparencyScorer(client).score("NVDA")
    assert report["heuristics"]["backed"] is False
    assert report["heuristics"]["audited"] is False
    assert report["heuristics"]["redeemable"] is False
    assert report["subscores"]["backing"] == 35.0
    assert report["subscores"]["reserves"] == 30.0
    assert report["subscores"]["redemption"] == 25.0
    assert any("heuristic" in n.lower() for n in report["notes"])


def test_map_failure_is_not_silent() -> None:
    class BrokenMap(RecordingClient):
        def rwa_map(self, symbol=None):
            raise RuntimeError("map down")

    with pytest.raises(ScoreError, match="RWA map"):
        TransparencyScorer(BrokenMap()).score("NVDA")

def test_backed_live_por_scores_reserves_higher_than_unknown(monkeypatch) -> None:
    """Backed issuer with mocked Chainlink PoR outscores an unknown issuer on reserves."""
    from rwa_score.verifiers import VerificationLevel, build_default_verifiers
    from tests.test_verifiers import _bnvda_rpc

    backed_client = RecordingClient(
        assets=[{"symbol": "NVDA", "rwa_id": 2}],
        info={2: {"symbol": "NVDA", "cik": "0001045810", "issuer": {"name": "Backed Finance"}}},
        issuers=[{"issuer_id": "abc", "name": "Backed Finance"}],
        issuer_details={
            "abc": {"name": "Backed Finance", "tokens": [{"rwa_id": 2, "crypto_id": 99}]},
        },
    )
    unknown_client = RecordingClient(
        assets=[{"symbol": "TSLA", "rwa_id": 15}],
        info={15: {"symbol": "TSLA", "cik": None, "issuer": {"name": "NoteVault Demo Issuer"}}},
        issuers=[{"issuer_id": "nv", "name": "NoteVault Demo Issuer"}],
        issuer_details={
            "nv": {
                "name": "NoteVault Demo Issuer",
                "tokens": [{"rwa_id": 15, "crypto_id": 99}],
            }
        },
        quotes={99: {"quote": {"USD": {"percent_change_24h": 1.0, "price": 10.0}}}},
    )

    session = _bnvda_rpc()
    verifiers = build_default_verifiers(session=session)
    backed_report = TransparencyScorer(
        backed_client, verifiers=verifiers, use_live_verifiers=True
    ).score("NVDA")
    unknown_report = TransparencyScorer(
        unknown_client, verifiers=verifiers, use_live_verifiers=True
    ).score("TSLA")

    assert backed_report["verification"]["reserves"]["level"] == VerificationLevel.ON_CHAIN_POR.value
    assert backed_report["verification"]["reserves"]["source"] == "chainlink_por"
    assert "Chainlink PoR" in backed_report["verification"]["reserves"]["evidence"]
    assert backed_report["subscores"]["reserves"] >= 90.0
    assert unknown_report["verification"]["reserves"]["source"] == "heuristic_fallback"
    assert "heuristic fallback" in unknown_report["explanations"]["reserves"].lower()
    assert backed_report["subscores"]["reserves"] > unknown_report["subscores"]["reserves"]


def test_fixture_report_includes_verification_badges(fixture_scorer: TransparencyScorer) -> None:
    report = fixture_scorer.score("NVDA")
    assert "verification" in report
    for key in WEIGHTS:
        block = report["verification"][key]
        assert "level" in block
        assert "evidence" in block
        assert block["level"] in {
            "self-reported",
            "on-chain PoR",
            "attested",
            "examined",
        }
    # Fixture mode skips live verifiers → heuristic fallback on backing/reserves.
    assert report["verification"]["backing"]["source"] == "heuristic_fallback"
    assert any("heuristic fallback" in n.lower() for n in report["notes"])


def test_robinhood_live_path_uses_verifier_not_heuristic() -> None:
    client = RecordingClient(
        assets=[{"symbol": "AAPL", "rwa_id": 16}],
        info={16: {"symbol": "AAPL", "cik": "0000320193", "issuer": {"name": "Robinhood"}}},
        issuers=[{"issuer_id": "rh", "name": "Robinhood"}],
        issuer_details={
            "rh": {"name": "Robinhood", "tokens": [{"rwa_id": 16, "crypto_id": 99}]},
        },
    )
    report = TransparencyScorer(client, use_live_verifiers=True).score("AAPL")
    assert report["issuer"] == "Robinhood"
    assert report["subscores"]["backing"] == 55.0
    assert report["subscores"]["reserves"] == 40.0
    assert report["subscores"]["redemption"] == 35.0
    assert report["verification"]["backing"]["source"] == "robinhood"
    assert report["verification"]["reserves"]["source"] == "robinhood"
    assert report["verification"]["redemption"]["source"] == "robinhood"
    assert report["verification"]["backing"]["evidence"] == "Robinhood 1:1 claim, no public PoR"
    assert report["issuer_note"]
    assert "debt" in report["issuer_note"].lower()
    assert "Robinhood Assets Jersey" in report["issuer_note"]
    # 55*0.20 + 40*0.20 + 35*0.15 + price + disclosure + basis — low/mid is expected.
    expected = round(
        55 * 0.20
        + 40 * 0.20
        + 35 * 0.15
        + report["subscores"]["price"] * 0.15
        + report["subscores"]["disclosure"] * 0.15
        + report["subscores"]["basis"] * 0.15,
        1,
    )
    assert report["score"] == expected
    assert report["band"] in {"YELLOW", "ORANGE"}
    assert "basis" in report["subscores"]
    assert report["verification"]["basis"]["source"] == "cmc_market_pairs"


def test_backed_keeps_heuristic_redemption() -> None:
    from rwa_score.verifiers import BackedVerifier, VerificationLevel
    from tests.test_verifiers import _bnvda_rpc

    client = RecordingClient()
    verifiers = {"backed": BackedVerifier(session=_bnvda_rpc())}
    report = TransparencyScorer(client, verifiers=verifiers, use_live_verifiers=True).score("NVDA")
    assert report["verification"]["reserves"]["level"] == VerificationLevel.ON_CHAIN_POR.value
    assert report["verification"]["redemption"]["source"] == "heuristic_fallback"
    assert "heuristic fallback" in report["verification"]["redemption"]["evidence"].lower()


def test_failed_verifier_is_not_silently_dropped() -> None:
    from rwa_score.verifiers import BackedVerifier
    from tests.test_verifiers import FakeRpcSession

    client = RecordingClient()
    verifiers = {"backed": BackedVerifier(session=FakeRpcSession(RuntimeError("network down")))}
    report = TransparencyScorer(client, verifiers=verifiers, use_live_verifiers=True).score("NVDA")
    assert any("verifier failure" in f.lower() or "network down" in f.lower() for f in report["flags"])
    assert any("heuristic fallback" in n.lower() for n in report["notes"])
    assert report["verification"]["reserves"]["error"]
