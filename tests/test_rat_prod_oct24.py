"""Reproductions for production findings RAT-1, RAT-3, RAT-4, RAT-5, RAT-6, RAT-7.

Each test states the behavior the score must have. On unfixed main the assertion fails.
"""

from __future__ import annotations

import json
import time

import pytest

from rwa_score import chainlink_por as por_mod
from rwa_score.chainlink_por import (
    XSTOCKS_POR_FEEDS,
    ChainlinkPorClient,
    probe_chainlink_por,
    resolve_por_feed,
)
from rwa_score.health import build_health_payload
from rwa_score.issuer_registry import issuer_note
from rwa_score.scorer import TransparencyScorer, band_code
from rwa_score.verifiers import (
    BACKED_INKIND_DOCS_URL,
    BACKED_REDEMPTION_DOCS_URL,
    CASH_REDEMPTION_SCORE,
    DINARI_DSHARES_URL,
    INKIND_REDEMPTION_SCORE,
    DinariVerifier,
    VerificationLevel,
    VerificationResult,
    parse_backed_redemption_docs,
)
from tests.conftest import RecordingClient
from tests.test_verifiers import (
    BACKED_INKIND_MD,
    FakeResponse,
    FakeRpcSession,
    FakeSession,
    encode_latest_round,
)


def _score_with_pillar_value(pillar: float, *, cik: str | None) -> dict:
    """Score NVDA with every variable pillar forced to ``pillar`` and disclosure from CIK."""
    client = RecordingClient(
        info={2: {"symbol": "NVDA", "cik": cik, "issuer": {"name": "Example Issuer"}}}
    )
    scorer = TransparencyScorer(client, use_live_verifiers=False)
    scorer._verify_pillar = lambda _pillar, *, ticker, issuer_name: VerificationResult(  # type: ignore[method-assign]
        score=pillar,
        level=VerificationLevel.SELF_REPORTED,
        evidence="stub",
        source="stub",
        ok=True,
    )
    scorer._price_score = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
        pillar,
        {"available": False, "source": "cmc_rwa_quotes"},
        [],
        "price",
    )
    scorer._basis_score = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
        pillar,
        {"available": False, "wrapper_count": 0, "source": "cmc_market_pairs"},
        [],
        "basis",
    )
    return scorer.score("NVDA")


@pytest.mark.parametrize(
    "raw,printed,band",
    [
        (74.96, 75.0, "GREEN"),
        (49.96, 50.0, "YELLOW"),
        (24.96, 25.0, "ORANGE"),
    ],
)
def test_rat1_band_matches_printed_score(raw: float, printed: float, band: str) -> None:
    """Disclosure is 20 with no CIK, so the other five pillars reconstruct ``raw``."""
    pillar = (raw - 3.0) / 0.85
    report = _score_with_pillar_value(pillar, cik=None)
    assert report["score"] == printed
    assert report["band"] == band
    assert report["band_label"].startswith(band)
    assert band_code(report["score"]) == report["band"]


def test_rat3_big4_does_not_match_they() -> None:
    html = "<p>They hold the assets 1:1 at Alpaca Securities.</p>"
    session = FakeSession({DINARI_DSHARES_URL: FakeResponse(200, text=html)})
    result = DinariVerifier(session=session).verify_backing(
        ticker="AAPL", issuer_name="Dinari"
    )
    assert result.level != VerificationLevel.ATTESTED
    assert result.score != 85.0

    named = (
        "<p>Audited by EY. Custody at Alpaca. The tokens are backed 1:1.</p>"
    )
    ey = DinariVerifier(
        session=FakeSession({DINARI_DSHARES_URL: FakeResponse(200, text=named)})
    ).verify_reserves(ticker="AAPL", issuer_name="Dinari")
    assert ey.level == VerificationLevel.ATTESTED
    assert ey.score == 85.0
    assert ey.meta["audit_firm"] == "EY"


_PHRASE_ONLY_MD = """
# Issuance and Redemption
Holders may redeem tokens on the primary market.
The issuer returns the corresponding underlying shares.
"""


def test_rat4_loose_share_phrase_is_not_in_kind() -> None:
    parsed = parse_backed_redemption_docs(_PHRASE_ONLY_MD)
    assert parsed["documented"] is True
    assert parsed["has_in_kind_heading"] is False
    assert parsed["in_kind_to_shares"] is False

    still = parse_backed_redemption_docs(BACKED_INKIND_MD)
    assert still["has_in_kind_heading"] is True
    assert still["in_kind_to_shares"] is True

    session = FakeSession(
        {
            BACKED_REDEMPTION_DOCS_URL: FakeResponse(200, text=_PHRASE_ONLY_MD),
            BACKED_INKIND_DOCS_URL: FakeResponse(404, text="missing"),
        }
    )
    from rwa_score.verifiers import BackedVerifier

    result = BackedVerifier(session=session).verify_redemption(
        ticker="AAPL", issuer_name="xStocks"
    )
    assert result.source == "backed_redemption_docs"
    assert result.score == CASH_REDEMPTION_SCORE
    assert result.score != INKIND_REDEMPTION_SCORE
    assert result.meta["in_kind_to_shares"] is False


def test_rat5_stale_or_incomplete_round_is_not_verified() -> None:
    feed = resolve_por_feed("NVDA")
    assert feed is not None
    stale_answer = 1000 * 10**feed.decimals
    session = FakeRpcSession(
        {
            feed.proxy.lower(): encode_latest_round(
                round_id=7,
                answer=stale_answer,
                updated_at=1_700_000_000,
                answered_in_round=7,
            )
        }
    )
    client = ChainlinkPorClient(
        session=session,
        rpc_urls={"polygon": ["https://rpc.example.test"]},
    )
    with pytest.raises(RuntimeError, match="not verified"):
        client.read("NVDA")

    fresh = int(time.time())
    incomplete = FakeRpcSession(
        {
            feed.proxy.lower(): encode_latest_round(
                round_id=9,
                answer=stale_answer,
                updated_at=fresh,
                answered_in_round=8,
            )
        }
    )
    incomplete_client = ChainlinkPorClient(
        session=incomplete,
        rpc_urls={"polygon": ["https://rpc.example.test"]},
    )
    with pytest.raises(RuntimeError, match="not verified"):
        incomplete_client.read("NVDA")

    good = FakeRpcSession(
        {
            feed.proxy.lower(): encode_latest_round(
                round_id=9,
                answer=stale_answer,
                updated_at=fresh,
                answered_in_round=9,
            )
        }
    )
    good_client = ChainlinkPorClient(
        session=good,
        rpc_urls={"polygon": ["https://rpc.example.test"]},
    )
    reading = good_client.read("NVDA")
    assert reading.reserves == 1000.0
    limit = getattr(por_mod, "POR_MAX_AGE_SECONDS", None)
    assert isinstance(limit, int) and limit > 0
    assert fresh - reading.updated_at <= limit

    def post(_url: str, json=None, timeout: float | None = None, **_kwargs):
        _ = json, timeout
        return FakeResponse(
            200,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": encode_latest_round(
                    answer=100,
                    updated_at=1_700_000_000,
                    round_id=4,
                    answered_in_round=4,
                ),
            },
        )

    assert probe_chainlink_por(poster=post, feed=feed) is False


def test_rat6_health_mode_matches_sidebar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Health fixtures follow the sidebar. Key values never appear.

    On this branch live mode is CoinGecko. An empty demo key is keyless
    live access, not fixture mode. RWA_USE_FIXTURES=1 still forces fixtures.
    """
    monkeypatch.setenv("RWA_USE_FIXTURES", "0")
    monkeypatch.delenv("CMC_API_KEY", raising=False)
    monkeypatch.delenv("COINGECKO_DEMO_API_KEY", raising=False)

    def boom(_url: str, json=None, timeout: float | None = None, **_kwargs):
        raise OSError("offline")

    payload = build_health_payload(poster=boom)
    assert payload["fixtures"] is False
    assert payload["verifiers_live"] is True
    assert payload["market_data_provider"] == "coingecko"
    assert payload["coingecko_access"] == "keyless"
    blob = json.dumps(payload)
    assert "CMC_API_KEY" not in blob
    assert "COINGECKO_DEMO_API_KEY" not in blob

    monkeypatch.setenv("COINGECKO_DEMO_API_KEY", "configured")
    live = build_health_payload(poster=boom)
    assert live["fixtures"] is False
    assert live["verifiers_live"] is True
    assert live["coingecko_access"] == "demo"
    assert "configured" not in json.dumps(live)

    monkeypatch.setenv("RWA_USE_FIXTURES", "1")
    forced = build_health_payload(poster=boom)
    assert forced["fixtures"] is True
    assert forced["verifiers_live"] is False
    assert forced["coingecko_access"] == "demo"
    assert "configured" not in json.dumps(forced)


def test_rat7_xstocks_note_matches_empty_feed_list() -> None:
    assert XSTOCKS_POR_FEEDS == ()
    note = issuer_note("xStocks")
    assert note is not None
    lowered = note.lower()
    assert "on-chain proof of reserves" not in lowered
    assert "no published" in lowered
    assert "proof-of-reserves feed" in lowered
