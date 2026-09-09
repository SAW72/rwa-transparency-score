from tests.conftest import MockClient
from rwa_score.scorer import WEIGHTS, TransparencyScorer, price_integrity


def test_weights_sum_to_one() -> None:
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def test_price_integrity_bounds() -> None:
    assert price_integrity(None) == 60
    assert price_integrity(0) == 100
    assert price_integrity(10) == 80
    assert price_integrity(-40) == 20
    assert price_integrity(100) == 20


def _client(*, issuer: str, cik: str | None, pct: float, rwa_id: int = 2) -> MockClient:
    return MockClient(
        assets=[{"symbol": "NVDA", "rwa_id": rwa_id}],
        info={rwa_id: {"name": "Nvidia Corp", "cik": cik, "primary_exchange": "Nasdaq"}},
        issuers=[{"issuer_id": "abc", "name": issuer}],
        issuer_detail={
            "abc": {
                "name": issuer,
                "issuer_id": "abc",
                "tokens": [{"rwa_id": rwa_id, "crypto_id": 9, "symbol": "NVDAX", "name": "nvda tok"}],
            }
        },
        quotes={9: {"quote": {"USD": {"price": 200.0, "volume_24h": 1_000, "percent_change_24h": pct}}}},
    )


def test_high_transparency_lands_green() -> None:
    scorer = TransparencyScorer(_client(issuer="Backed Assets", cik="0001045810", pct=1.0))
    report = scorer.score("nvda")
    assert report["ticker"] == "NVDA"
    assert report["band_key"] == "green"
    assert report["score"] >= 75
    assert report["flags"] == []
    assert report["cik"] == "0001045810"
    assert report["classification"]["audited"] is True


def test_thin_wrapper_lands_orange_with_flags() -> None:
    scorer = TransparencyScorer(_client(issuer="ThinWrap Labs", cik=None, pct=40.0))
    report = scorer.score("NVDA")
    assert report["band_key"] == "orange"
    assert report["score"] < 50
    assert any("CIK" in f for f in report["flags"])
    assert any("reserves" in f.lower() for f in report["flags"])
    assert any("redemption" in f.lower() for f in report["flags"])
    assert any("drifting" in f.lower() for f in report["flags"])


def test_unknown_ticker_lists_known_symbols() -> None:
    scorer = TransparencyScorer(_client(issuer="Backed Assets", cik="1", pct=0))
    try:
        scorer.score("MSFT")
    except KeyError as exc:
        assert "MSFT" in str(exc)
        assert "NVDA" in str(exc)
    else:
        raise AssertionError("expected KeyError")


def test_score_many_keeps_going_on_errors() -> None:
    scorer = TransparencyScorer(_client(issuer="Backed Assets", cik="1", pct=0))
    out = scorer.score_many(["NVDA", "NOPE"])
    assert "score" in out[0]
    assert out[1]["error"]


def test_quote_failure_does_not_crash() -> None:
    client = _client(issuer="Backed Assets", cik="0001", pct=0)

    def boom(_crypto_id: int) -> dict:
        raise RuntimeError("timeout")

    client.crypto_quote = boom  # type: ignore[method-assign]
    report = TransparencyScorer(client).score("NVDA")
    assert report["subscores"]["price"] == 60
    assert report["score"] >= 75


def test_issuer_index_is_built_once() -> None:
    client = _client(issuer="xStocks", cik="0001318605", pct=6.4)
    scorer = TransparencyScorer(client)
    scorer.score("NVDA")
    scorer.score("NVDA")
    assert client.calls.count("issuers_list") == 1
    assert client.calls.count("issuer:abc") == 1
