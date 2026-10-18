"""RWA quotes/latest + assets/list scoring, search, and call-log honesty."""

from __future__ import annotations

from rwa_score.client import (
    ENDPOINT_ASSETS_LIST,
    ENDPOINT_QUOTES,
    FixtureClient,
    parse_assets_list_payload,
    parse_rwa_quotes_payload,
    summarize_call_log,
)
from rwa_score.scorer import (
    DEFAULT_PRICE_SCORE,
    ZERO_VOLUME_PRICE_CAP,
    TransparencyScorer,
    merge_basis_wrappers,
    price_score_from_deviation,
    wrappers_from_rwa_tokens,
)
from rwa_score.ticker_search import load_search_catalog, search_tickers
from rwa_score.coingecko_client import NOT_AVAILABLE, CoinGeckoClient
from tests.conftest import RecordingClient


def test_parse_rwa_quotes_payload_keeps_missing_numeric_none() -> None:
    parsed = parse_rwa_quotes_payload(
        {
            "rwa_id": "2",
            "symbol": "nvda",
            "tokens": [{"symbol": "NVDAx", "price": None, "crypto_id": 36992}],
            "tradfi_markets": [
                {
                    "exchange": {"name": "Nasdaq", "slug": "nasdaq"},
                    "ticker": "NVDA",
                    "market_url": "https://example.test/nvda",
                }
            ],
        }
    )
    assert parsed["rwa_id"] == 2
    assert parsed["symbol"] == "NVDA"
    assert parsed["average_tokenized_price"] is None
    assert parsed["tokens"][0]["price"] is None
    assert parsed["tradfi_markets"][0]["ticker"] == "NVDA"
    assert "price" not in parsed["tradfi_markets"][0]


def test_parse_assets_list_payload_normalizes_rows() -> None:
    parsed = parse_assets_list_payload(
        {
            "rwa_assets": [
                {
                    "symbol": "gold",
                    "rwa_id": "1",
                    "asset_type": "commodity",
                    "rwa_rank": "1",
                    "average_tokenized_price": "12.5",
                }
            ],
            "has_more": True,
        }
    )
    assert parsed["rwa_assets"][0]["symbol"] == "GOLD"
    assert parsed["rwa_assets"][0]["rwa_id"] == 1
    assert parsed["rwa_assets"][0]["average_tokenized_price"] == 12.5
    assert parsed["has_more"] is True


class _QuietSession:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        raise AssertionError(f"unexpected network {url}")


def test_live_rwa_quotes_fetches_and_caches() -> None:
    """Second quote read is a cache hit. Venue names are not invented."""
    from tests.test_coingecko_client import FakeResponse, FakeSession, _market

    session = FakeSession(
        [
            FakeResponse(
                200,
                [_market(row["coingecko_id"], row["symbol"], price=100.0, market_cap=1_000.0, supply=10.0)
                 for row in __import__("rwa_score.coingecko_client", fromlist=["load_allowlist"]).load_allowlist()],
            )
        ]
    )
    client = CoinGeckoClient(session=session, sleeper=lambda _s: None, api_key="")
    first = client.rwa_quotes(symbol="PAXG")
    second = client.rwa_quotes(symbol="PAXG")
    assert first["symbol"] == second["symbol"] == "PAXG"
    assert first["average_tokenized_price"] == 100.0
    assert first["tokens"] == []
    assert first["tradfi_markets_status"] == NOT_AVAILABLE
    assert len(session.calls) == 1
    log = client.call_log()
    assert log[0]["source"] == "live" and log[0]["via"] == "network"
    assert any(row["via"] == "cache" and row["cached"] is True for row in log)


def test_live_assets_list_rejects_unknown_type() -> None:
    client = CoinGeckoClient(session=_QuietSession(), sleeper=lambda _s: None, api_key="")
    try:
        client.assets_list(asset_type="spaceship")
    except Exception as exc:
        assert "asset_type" in str(exc)
    else:
        raise AssertionError("expected an asset_type error")


def test_live_assets_list_class_is_not_available() -> None:
    client = CoinGeckoClient(session=_QuietSession(), sleeper=lambda _s: None, api_key="")
    # Class filter is answered from the allowlist before a market call.
    # Every allowlist class is unknown, so stock is not covered.
    mapped = client.rwa_map(asset_type="stock")
    assert mapped == []
    assert client.class_coverage_note == NOT_AVAILABLE


def test_price_prefers_rwa_quotes_over_crypto() -> None:
    client = RecordingClient(
        rwa_quotes={
            2: {
                "rwa_id": 2,
                "symbol": "NVDA",
                "average_tokenized_price": 100.0,
                "tokenized_market_cap": 1000.0,
                "tokenized_volume_24h": 50.0,
                "tokens": [
                    {
                        "symbol": "NVDAx",
                        "price": 101.0,
                        "crypto_id": 99,
                        "issuer_name": "Backed Finance",
                    }
                ],
            }
        }
    )
    report = TransparencyScorer(client, use_live_verifiers=False).score("NVDA")
    assert report["verification"]["price"]["source"] == "cmc_rwa_quotes"
    assert report["price"]["available"] is True
    assert abs(report["price"]["max_deviation_pct"] - 1.0) < 1e-9
    assert report["subscores"]["price"] == round(price_score_from_deviation(1.0), 1)
    assert client.calls["crypto_quote"] == 0
    assert client.calls["rwa_quotes"] == 1
    assert report["cmc_calls"]["live"] is True
    assert any(
        row["endpoint"] == ENDPOINT_QUOTES for row in report["cmc_calls"]["endpoints"]
    )


def test_zero_tokenized_volume_caps_price_score() -> None:
    client = RecordingClient(
        rwa_quotes={
            2: {
                "average_tokenized_price": 100.0,
                "tokenized_volume_24h": 0,
                "tokens": [{"symbol": "NVDAx", "price": 100.0, "crypto_id": 99}],
            }
        }
    )
    report = TransparencyScorer(client, use_live_verifiers=False).score("NVDA")
    assert report["subscores"]["price"] == ZERO_VOLUME_PRICE_CAP
    assert any("tokenized_volume_24h" in f for f in report["flags"])


def test_rwa_quotes_without_priced_tokens_falls_back_to_crypto() -> None:
    client = RecordingClient(
        rwa_quotes={2: {"average_tokenized_price": 100.0, "tokens": []}},
    )
    report = TransparencyScorer(client, use_live_verifiers=False).score("NVDA")
    assert report["price"]["source"] == "cmc_rwa_quotes"
    assert report["price"]["available"] is False
    assert report["subscores"]["price"] == DEFAULT_PRICE_SCORE
    assert client.calls["crypto_quote"] == 0


def test_rwa_quotes_error_falls_back_to_crypto_and_is_flagged() -> None:
    client = RecordingClient(rwa_quotes_error=RuntimeError("quotes down"))
    report = TransparencyScorer(client, use_live_verifiers=False).score("NVDA")
    assert any("quotes down" in f for f in report["flags"])
    assert report["price"]["source"] == "cmc_crypto_quote"
    assert report["price"]["fallback"] is True
    assert report["price"]["available"] is True
    assert client.calls["crypto_quote"] == 1


def test_basis_uses_quotes_when_market_pairs_fail() -> None:
    client = RecordingClient(
        rwa_quotes={
            2: {
                "tokens": [
                    {"symbol": "NVDAx", "price": 100.0, "crypto_id": 99, "issuer_name": "Backed"},
                    {"symbol": "NVDAon", "price": 102.0, "crypto_id": 100, "issuer_name": "Ondo"},
                ]
            }
        },
        market_pairs_error=RuntimeError("pairs down"),
    )
    report = TransparencyScorer(client, use_live_verifiers=False).score("NVDA")
    assert report["basis"]["available"] is True
    assert report["basis"]["source"] == "cmc_rwa_quotes"
    assert report["verification"]["basis"]["source"] == "cmc_rwa_quotes"
    assert report["basis"]["wrapper_count"] == 2
    assert any("pairs down" in f for f in report["flags"])


def test_merge_basis_wrappers_unions_by_crypto_id() -> None:
    quotes = wrappers_from_rwa_tokens(
        [{"symbol": "NVDAx", "price": 100.0, "crypto_id": 1, "issuer_name": "Backed"}]
    )
    pairs = [{"crypto_id": 1, "symbol": "NVDAx", "price": 99.0, "volume_24h": 10.0, "venues": 2}]
    merged = merge_basis_wrappers(quotes, pairs)
    assert len(merged) == 1
    assert merged[0]["issuer"] == "Backed"
    assert merged[0]["price"] == 100.0
    assert merged[0]["venues"] == 2
    assert merged[0]["source"] == "cmc_rwa_quotes+market_pairs"


def test_fixture_call_log_never_claims_live() -> None:
    client = FixtureClient()
    client.begin_run()
    client.rwa_quotes(rwa_id=2)
    client.assets_list()
    block = summarize_call_log(client.call_log(), client_source=client.source)
    assert block["live"] is False
    assert block["source"] == "fixture"
    assert "not live" in block["label"].lower()
    assert all(row["source"] == "fixture" for row in block["endpoints"])


def test_search_stock_asset_type_lists_fixture_equities() -> None:
    catalog = load_search_catalog(FixtureClient())
    hits = search_tickers("stock", catalog)
    symbols = {opt.symbol for opt in hits}
    assert {"NVDA", "AAPL", "TSLA"} & symbols
    nvidia = next(opt for opt in catalog if opt.symbol == "NVDA")
    assert nvidia.asset_type == "stock"
    assert nvidia.rwa_rank == 2
    assert "stock" in nvidia.categories


def test_summarize_call_log_forces_fixture_when_client_is_fixture() -> None:
    sneaky = [{"endpoint": ENDPOINT_QUOTES, "source": "live", "via": "network"}]
    block = summarize_call_log(sneaky, client_source="fixture")
    assert block["live"] is False
    assert block["endpoints"][0]["source"] == "fixture"
    assert block["endpoints"][0]["via"] == "fixture"
