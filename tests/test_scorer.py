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
    assert report["verification"]["basis"]["source"] == "cmc_rwa_quotes+market_pairs"
    assert report["basis"]["available"] is True
    assert report["verification"]["price"]["source"] == "cmc_rwa_quotes"
    assert report["price"]["source"] == "cmc_rwa_quotes"
    assert report["cmc_calls"]["source"] == "fixture"
    assert report["cmc_calls"]["live"] is False
    endpoints = {row["endpoint"] for row in report["cmc_calls"]["endpoints"]}
    assert "/v5/real-world-assets/quotes/latest" in endpoints
    assert all(row["source"] == "fixture" for row in report["cmc_calls"]["endpoints"])


def test_fixture_tsla_is_thin_wrapper(fixture_scorer: TransparencyScorer) -> None:
    report = fixture_scorer.score("TSLA")
    assert report["band"] in {"ORANGE", "RED"}
    assert report["heuristics"]["backed"] is False
    assert any("CIK" in f for f in report["flags"])
    assert any("proof of reserves" in f.lower() for f in report["flags"])
    assert report["price"]["available"] is True
    assert report["price"]["source"] == "cmc_rwa_quotes"
    assert report["price"]["max_deviation_pct"] is not None
    assert report["price"]["max_deviation_pct"] > 5


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


def test_backed_live_redemption_uses_docs_hook() -> None:
    from rwa_score.verifiers import (
        BACKED_INKIND_DOCS_URL,
        BACKED_REDEMPTION_DOCS_URL,
        INKIND_REDEMPTION_SCORE,
        BackedVerifier,
        VerificationLevel,
    )
    from tests.test_verifiers import (
        BACKED_INKIND_MD,
        BACKED_OVERVIEW_MD,
        FakeResponse,
        FakeSession,
        _bnvda_rpc,
    )

    class CombinedSession:
        def __init__(self) -> None:
            self.rpc = _bnvda_rpc()
            self.http = FakeSession(
                {
                    BACKED_REDEMPTION_DOCS_URL: FakeResponse(200, text=BACKED_OVERVIEW_MD),
                    BACKED_INKIND_DOCS_URL: FakeResponse(200, text=BACKED_INKIND_MD),
                }
            )

        def post(self, *args, **kwargs):
            return self.rpc.post(*args, **kwargs)

        def get(self, *args, **kwargs):
            return self.http.get(*args, **kwargs)

    client = RecordingClient()
    verifiers = {"backed": BackedVerifier(session=CombinedSession())}
    report = TransparencyScorer(client, verifiers=verifiers, use_live_verifiers=True).score("NVDA")
    assert report["verification"]["reserves"]["level"] == VerificationLevel.ON_CHAIN_POR.value
    assert report["verification"]["redemption"]["source"] == "backed_redemption_docs"
    assert report["subscores"]["redemption"] == INKIND_REDEMPTION_SCORE
    assert "heuristic fallback" not in report["verification"]["redemption"]["evidence"].lower()
    assert "Live redemption check" in report["explanations"]["redemption"]


def test_dinari_live_redemption_uses_docs_hook() -> None:
    from rwa_score.verifiers import (
        CASH_REDEMPTION_SCORE,
        DINARI_DSHARE_DOCS_URL,
        DINARI_DSHARES_URL,
        DinariVerifier,
    )
    from tests.test_verifiers import DINARI_DSHARE_MD, FakeResponse, FakeSession

    html = """
    <html><body>
    <p>dShares are backed 1:1 by the underlying securities.</p>
    <p>Custody lives at Alpaca Securities LLC.</p>
    <p>Reserve audits are performed by an independent Big 4 accounting firm.</p>
    </body></html>
    """
    session = FakeSession(
        {
            DINARI_DSHARES_URL: FakeResponse(200, text=html),
            DINARI_DSHARE_DOCS_URL: FakeResponse(200, text=DINARI_DSHARE_MD),
        }
    )
    client = RecordingClient(
        assets=[{"symbol": "AAPL", "rwa_id": 16}],
        info={16: {"symbol": "AAPL", "cik": "0000320193", "issuer": {"name": "Dinari"}}},
        issuers=[{"issuer_id": "dn", "name": "Dinari"}],
        issuer_details={"dn": {"name": "Dinari", "tokens": [{"rwa_id": 16, "crypto_id": 99}]}},
    )
    report = TransparencyScorer(
        client, verifiers={"dinari": DinariVerifier(session=session)}, use_live_verifiers=True
    ).score("AAPL")
    assert report["verification"]["redemption"]["source"] == "dinari_redemption_docs"
    assert report["subscores"]["redemption"] == CASH_REDEMPTION_SCORE
    assert report["verification"]["reserves"]["source"] == "dinari_dshares"
    assert "Live redemption check" in report["explanations"]["redemption"]


def test_backed_redemption_docs_error_is_not_silent() -> None:
    from rwa_score.verifiers import BACKED_REDEMPTION_DOCS_URL, BackedVerifier
    from tests.test_verifiers import FakeResponse, FakeSession, _bnvda_rpc

    class CombinedSession:
        def __init__(self) -> None:
            self.rpc = _bnvda_rpc()
            self.http = FakeSession(
                {BACKED_REDEMPTION_DOCS_URL: FakeResponse(503, text="down")}
            )

        def post(self, *args, **kwargs):
            return self.rpc.post(*args, **kwargs)

        def get(self, *args, **kwargs):
            return self.http.get(*args, **kwargs)

    report = TransparencyScorer(
        RecordingClient(),
        verifiers={"backed": BackedVerifier(session=CombinedSession())},
        use_live_verifiers=True,
    ).score("NVDA")
    assert report["verification"]["redemption"]["source"] == "heuristic_fallback"
    assert report["verification"]["redemption"]["error"]
    assert any("verifier failure" in f.lower() or "503" in f for f in report["flags"])


def test_fixture_scorer_never_calls_http() -> None:
    class BoomSession:
        def get(self, *args, **kwargs):
            raise AssertionError("fixture path must not HTTP GET")

        def post(self, *args, **kwargs):
            raise AssertionError("fixture path must not HTTP POST")

    from rwa_score.client import FixtureClient

    scorer = TransparencyScorer(FixtureClient(), session=BoomSession())
    report = scorer.score("NVDA")
    assert report["data_source"] == "fixture"
    assert report["live_verifiers"] is False
    assert report["verification_mode"] == "offline_heuristic"
    assert report["verification"]["backing"]["source"] == "heuristic_fallback"
    assert report["verification"]["reserves"]["source"] == "heuristic_fallback"
    assert report["verification"]["redemption"]["source"] == "heuristic_fallback"


def test_fixture_client_blocks_live_verifiers_unless_opted_in() -> None:
    from rwa_score.client import FixtureClient

    blocked = TransparencyScorer(FixtureClient(), use_live_verifiers=True)
    assert blocked.use_live_verifiers is False
    assert blocked._fixture_live_blocked is True
    report = blocked.score("AAPL")
    assert report["live_verifiers"] is False
    assert any("blocked live verifiers" in n for n in report["notes"])

    allowed = TransparencyScorer(
        FixtureClient(), use_live_verifiers=True, allow_live_on_fixtures=True
    )
    assert allowed.use_live_verifiers is True
    assert allowed._fixture_live_blocked is False


def test_remaining_heuristic_paths_label_self_reported() -> None:
    from rwa_score.scorer import remaining_heuristic_paths

    report = TransparencyScorer(RecordingClient(), use_live_verifiers=False).score("NVDA")
    rows = remaining_heuristic_paths(
        report["verification"],
        data_source=report["data_source"],
        live_verifiers=False,
    )
    kinds = {row["kind"] for row in rows}
    assert "heuristic_fallback" in kinds
    assert "self-reported" in kinds
    assert "offline_skip" in kinds


def test_failed_verifier_is_not_silently_dropped() -> None:
    from rwa_score.verifiers import BackedVerifier
    from tests.test_verifiers import FakeRpcSession

    client = RecordingClient()
    verifiers = {"backed": BackedVerifier(session=FakeRpcSession(RuntimeError("network down")))}
    report = TransparencyScorer(client, verifiers=verifiers, use_live_verifiers=True).score("NVDA")
    assert any("verifier failure" in f.lower() or "network down" in f.lower() for f in report["flags"])
    assert any("heuristic fallback" in n.lower() for n in report["notes"])
    assert report["verification"]["reserves"]["error"]


def test_live_bnvda_reaches_on_chain_por_badge() -> None:
    """Picking the Backed bToken (not the CMC NVDA row) hits Chainlink PoR."""
    from rwa_score.verifiers import VerificationLevel, build_default_verifiers
    from tests.test_verifiers import _bnvda_rpc

    client = RecordingClient()
    session = _bnvda_rpc()
    verifiers = build_default_verifiers(session=session)
    report = TransparencyScorer(
        client, verifiers=verifiers, use_live_verifiers=True
    ).score("bNVDA")
    assert report["ticker"] == "bNVDA"
    assert report["issuer"] == "Backed Finance"
    assert report["verification"]["reserves"]["level"] == VerificationLevel.ON_CHAIN_POR.value
    assert report["verification"]["backing"]["level"] == VerificationLevel.ON_CHAIN_POR.value
    assert report["verification"]["reserves"]["source"] == "chainlink_por"
    assert report["verification"]["reserves"]["meta"]["symbol"] == "bNVDA"
    assert "Chainlink PoR" in report["verification"]["reserves"]["evidence"]
    assert any("underlying" in n.lower() and "NVDA" in n for n in report["notes"])


def test_live_bnvda_uses_backed_verifier_even_if_map_issuer_is_not_backed() -> None:
    from rwa_score.verifiers import VerificationLevel, build_default_verifiers
    from tests.test_verifiers import _bnvda_rpc

    client = RecordingClient(
        assets=[{"symbol": "NVDA", "rwa_id": 2}],
        info={2: {"symbol": "NVDA", "cik": "0001045810", "issuer": {"name": "Ondo"}}},
        issuers=[{"issuer_id": "ondo", "name": "Ondo"}],
        issuer_details={
            "ondo": {"name": "Ondo", "tokens": [{"rwa_id": 2, "crypto_id": 99}]},
        },
    )
    verifiers = build_default_verifiers(session=_bnvda_rpc())
    report = TransparencyScorer(
        client, verifiers=verifiers, use_live_verifiers=True
    ).score("BNVDA")
    assert report["ticker"] == "bNVDA"
    assert report["issuer"] == "Backed Finance"
    assert report["verification"]["reserves"]["level"] == VerificationLevel.ON_CHAIN_POR.value


def test_fixture_catalog_only_btoken_stays_labeled_heuristic(
    fixture_scorer: TransparencyScorer,
) -> None:
    """bIB01 is searchable; fixtures have no IB01 row — do not invent CMC metrics."""
    report = fixture_scorer.score("bIB01")
    assert report["ticker"] == "bIB01"
    assert report["rwa_id"] is None
    assert report["verification"]["reserves"]["source"] == "heuristic_fallback"
    assert report["verification"]["reserves"]["level"] == "self-reported"
    assert report["verification"]["reserves"]["meta"]["published_por_feed"] == "bIB01"
    assert "live RPC skipped" in report["verification"]["reserves"]["evidence"]
    assert report["subscores"]["disclosure"] == 20.0
    assert any("BACKED_POR_FEEDS" in n for n in report["notes"])


def test_live_catalog_only_btoken_hits_on_chain_por() -> None:
    from rwa_score.chainlink_por import BACKED_POR_FEEDS
    from rwa_score.verifiers import VerificationLevel, build_default_verifiers
    from tests.test_verifiers import FakeRpcSession, encode_latest_round

    bib01 = next(feed for feed in BACKED_POR_FEEDS if feed.symbol == "bIB01")
    session = FakeRpcSession(
        {bib01.proxy.lower(): encode_latest_round(answer=80 * 10**bib01.decimals)}
    )
    client = RecordingClient(assets=[{"symbol": "NVDA", "rwa_id": 2}])
    verifiers = build_default_verifiers(session=session)
    report = TransparencyScorer(
        client, verifiers=verifiers, use_live_verifiers=True
    ).score("bIB01")
    assert report["ticker"] == "bIB01"
    assert report["rwa_id"] is None
    assert report["verification"]["reserves"]["level"] == VerificationLevel.ON_CHAIN_POR.value
    assert report["verification"]["reserves"]["source"] == "chainlink_por"
    assert report["verification"]["reserves"]["meta"]["symbol"] == "bIB01"
