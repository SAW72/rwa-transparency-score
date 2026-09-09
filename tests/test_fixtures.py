from rwa_score.client import create_client
from rwa_score.fixtures import FixtureClient
from rwa_score.scorer import TransparencyScorer


def test_fixture_map_filters_by_symbol() -> None:
    client = FixtureClient()
    only = client.rwa_map("NVDA")
    assert [a["symbol"] for a in only] == ["NVDA"]
    assert client.rwa_map("nvda,tsla")  # case-insensitive


def test_fixture_info_and_quote_shapes() -> None:
    client = FixtureClient()
    info = client.rwa_info(2)
    assert info["cik"] == "0001045810"
    assert info["primary_exchange"] == "Nasdaq"
    quote = client.crypto_quote(36992)
    assert quote["quote"]["USD"]["percent_change_24h"] == 1.2


def test_fixture_unknown_info_is_empty() -> None:
    assert FixtureClient().rwa_info(999) == {}


def test_offline_demo_scores_three_bands(fixture_scorer: TransparencyScorer) -> None:
    nvda = fixture_scorer.score("NVDA")
    tsla = fixture_scorer.score("TSLA")
    aapl = fixture_scorer.score("AAPL")
    assert nvda["source"] == "fixtures"
    assert nvda["score"] > tsla["score"] > aapl["score"]
    assert nvda["band_key"] == "green"
    assert tsla["band_key"] == "yellow"
    assert aapl["band_key"] == "orange"
    assert aapl["cik"] is None
    assert nvda["issuer"] == "Backed Assets"
    assert tsla["issuer"] == "xStocks"
    assert aapl["issuer"] == "ThinWrap Labs"


def test_create_client_defaults_to_fixtures_without_key(monkeypatch) -> None:
    monkeypatch.delenv("CMC_API_KEY", raising=False)
    monkeypatch.delenv("USE_FIXTURES", raising=False)
    client = create_client()
    assert isinstance(client, FixtureClient)


def test_create_client_honors_use_fixtures_flag(monkeypatch) -> None:
    monkeypatch.setenv("CMC_API_KEY", "should-not-be-used")
    monkeypatch.setenv("USE_FIXTURES", "1")
    client = create_client()
    assert client.source == "fixtures"
