from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rwa_score.client import (
    BASE_URL,
    CMCClient,
    CMCError,
    ENDPOINT_ASSETS_LIST,
    ENDPOINT_MAP,
    FixtureClient,
    create_client,
    directory_has_more,
    env_flag,
    parse_market_pairs_payload,
    parse_rwa_map_payload,
    use_fixtures,
)
from rwa_score.fixtures import DEMO_FIXTURE_PATH
from rwa_score.scorer import TransparencyScorer


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict[str, Any] | None = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else text
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("No JSON body")
        return json.loads(json.dumps(self._payload))


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: float | None = None):
        self.calls.append((url, params))
        if not self._responses:
            raise AssertionError(f"unexpected GET {url} params={params}")
        return self._responses.pop(0)

    def paths(self) -> list[str]:
        return [url.removeprefix(BASE_URL) for url, _ in self.calls]


def cmc_ok(data: dict[str, Any]) -> FakeResponse:
    return FakeResponse(200, {"status": {"error_code": 0}, "data": data})


def cmc_429(
    *,
    retry_after: float | None = None,
    http_status: int = 429,
    error_code: int = 1008,
    message: str = (
        "You've exceeded your API Key's HTTP request rate limit. "
        "Rate limits reset every minute"
    ),
) -> FakeResponse:
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return FakeResponse(
        http_status,
        {"status": {"error_code": error_code, "error_message": message}},
        headers=headers,
    )


def _live(session: FakeSession, **kwargs: Any) -> CMCClient:
    return CMCClient(api_key="test-not-a-real-key", session=session, sleeper=lambda _s: None, **kwargs)


def test_fixture_file_is_labeled_demo() -> None:
    payload = json.loads(DEMO_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert payload["meta"]["kind"] == "demo_fixture"
    assert "not live" in payload["meta"]["label"].lower()


def test_fixture_client_requires_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CMC_API_KEY", raising=False)
    client = FixtureClient()
    assert client.source == "fixture"
    symbols = {a["symbol"] for a in client.rwa_map()}
    assert {"NVDA", "TSLA", "AAPL", "META"} <= symbols
    assert client.rwa_info(2)["cik"] == "0001045810"
    assert client.issuers_list()
    backed = client.issuer("6878977dcbbf471de3366e85")
    assert backed["name"] == "Backed Finance"
    quote = client.crypto_quote(36992)
    assert "USD" in quote["quote"]
    nvda_pairs = client.market_pairs(rwa_id=2)
    assert nvda_pairs["symbol"] == "NVDA"
    assert len(nvda_pairs["market_pairs"]) >= 2
    quotes = client.rwa_quotes(rwa_id=2)
    assert quotes["symbol"] == "NVDA"
    assert quotes["average_tokenized_price"] == 118.55
    assert any(tok["issuer_name"] == "Backed Finance" for tok in quotes["tokens"])
    listed = client.assets_list(asset_type="stock")
    assert {row["symbol"] for row in listed["rwa_assets"]} >= {"NVDA", "TSLA", "AAPL"}
    assert listed["rwa_assets"][0]["rwa_rank"] <= listed["rwa_assets"][-1]["rwa_rank"]
    log = client.call_log()
    assert log
    assert all(row["source"] == "fixture" for row in log)
    assert all(row["via"] == "fixture" for row in log)


def test_fixture_map_filters_symbol() -> None:
    client = FixtureClient()
    rows = client.rwa_map("nvda,aapl")
    assert {r["symbol"] for r in rows} == {"NVDA", "AAPL"}


def test_unlabeled_fixture_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(CMCError, match="demo_fixture"):
        FixtureClient(path=path)


def test_live_client_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CMC_API_KEY", raising=False)
    with pytest.raises(CMCError, match="CMC_API_KEY"):
        CMCClient()


def test_create_client_fixtures_flag() -> None:
    client = create_client(use_fixtures_mode=True)
    assert isinstance(client, FixtureClient)


def test_create_client_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RWA_USE_FIXTURES", "1")
    monkeypatch.delenv("CMC_API_KEY", raising=False)
    assert use_fixtures() is True
    assert isinstance(create_client(), FixtureClient)


def test_env_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RWA_USE_FIXTURES", "true")
    assert env_flag("RWA_USE_FIXTURES") is True
    monkeypatch.setenv("RWA_USE_FIXTURES", "0")
    assert env_flag("RWA_USE_FIXTURES") is False


def test_get_retries_http_429_then_succeeds() -> None:
    session = FakeSession(
        [
            cmc_429(retry_after=0),
            cmc_ok({"rwa_assets": [{"rwa_id": 2, "symbol": "NVDA", "cik": "0001045810"}]}),
        ]
    )
    info = _live(session).rwa_info(2)
    assert info["symbol"] == "NVDA"
    assert session.paths() == [
        "/v5/real-world-assets/info",
        "/v5/real-world-assets/info",
    ]


def test_get_retries_cmc_error_1008_on_http_200() -> None:
    session = FakeSession(
        [
            cmc_429(http_status=200, retry_after=0),
            cmc_ok({"rwa_assets": [{"rwa_id": 2, "cik": "1"}]}),
        ]
    )
    assert _live(session).rwa_info(2)["cik"] == "1"
    assert session.paths().count("/v5/real-world-assets/info") == 2


def test_get_honors_retry_after_header() -> None:
    slept: list[float] = []
    session = FakeSession(
        [
            cmc_429(retry_after=1.25),
            cmc_ok({"rwa_assets": [{"rwa_id": 2}]}),
        ]
    )
    client = CMCClient(api_key="test-not-a-real-key", session=session, sleeper=slept.append)
    client.rwa_info(2)
    assert slept == [1.25]


def test_get_rate_limit_exhausted_is_clear() -> None:
    session = FakeSession([cmc_429(), cmc_429(), cmc_429()])
    client = _live(session, max_retries=2)
    with pytest.raises(CMCError, match="rate limit") as excinfo:
        client.rwa_info(2)
    message = str(excinfo.value)
    assert "1008" in message
    assert "minute" in message.lower()
    assert "RWA_USE_FIXTURES" in message
    assert session.paths().count("/v5/real-world-assets/info") == 3


def test_non_rate_limit_http_error_is_not_retried() -> None:
    session = FakeSession([FakeResponse(500, text="upstream down")])
    with pytest.raises(CMCError, match="HTTP 500"):
        _live(session).rwa_info(2)
    assert len(session.calls) == 1


def test_issuers_list_and_detail_cached_for_process_lifetime() -> None:
    session = FakeSession(
        [
            cmc_ok({"issuers": [{"issuer_id": "abc", "name": "Backed Finance"}]}),
            cmc_ok({"name": "Backed Finance", "tokens": [{"rwa_id": 2}]}),
        ]
    )
    client = _live(session)
    first = client.issuers_list()
    second = client.issuers_list()
    assert first == second == [{"issuer_id": "abc", "name": "Backed Finance"}]
    assert client.issuer("abc")["name"] == "Backed Finance"
    assert client.issuer("abc")["name"] == "Backed Finance"
    assert session.paths() == [
        "/v5/real-world-assets/issuers/list",
        "/v5/real-world-assets/issuers",
    ]


def test_issuer_list_called_once_across_multiple_scores() -> None:
    """Rebuilding the scorer (Streamlit rerun) must not re-hit issuers/list."""
    session = FakeSession(
        [
            cmc_ok(
                {
                    "rwa_assets": [
                        {"symbol": "NVDA", "rwa_id": 2},
                        {"symbol": "AAPL", "rwa_id": 3},
                    ]
                }
            ),
            cmc_ok({"rwa_assets": [{"symbol": "NVDA", "rwa_id": 2, "cik": "0001045810"}]}),
            cmc_ok({"issuers": [{"issuer_id": "abc", "name": "Backed Finance"}]}),
            cmc_ok(
                {
                    "name": "Backed Finance",
                    "tokens": [
                        {"rwa_id": 2, "crypto_id": 99},
                        {"rwa_id": 3, "crypto_id": 100},
                    ],
                }
            ),
            cmc_ok(
                {
                    "rwa_assets": [
                        {
                            "rwa_id": 2,
                            "symbol": "NVDA",
                            "average_tokenized_price": 10.0,
                            "tokenized_market_cap": 1000.0,
                            "tokenized_volume_24h": 100.0,
                            "tokens": [
                                {
                                    "symbol": "NVDAx",
                                    "price": 10.0,
                                    "crypto_id": 99,
                                    "issuer_name": "Backed Finance",
                                }
                            ],
                            "tradfi_markets": [],
                        }
                    ]
                }
            ),
            cmc_ok(
                {
                    "rwa_id": 2,
                    "symbol": "NVDA",
                    "num_market_pairs": 0,
                    "market_pairs": [],
                }
            ),
            cmc_ok({"rwa_assets": [{"symbol": "AAPL", "rwa_id": 3, "cik": "0000320193"}]}),
            cmc_ok(
                {
                    "rwa_assets": [
                        {
                            "rwa_id": 3,
                            "symbol": "AAPL",
                            "average_tokenized_price": 20.0,
                            "tokenized_market_cap": 2000.0,
                            "tokenized_volume_24h": 200.0,
                            "tokens": [
                                {
                                    "symbol": "AAPLx",
                                    "price": 20.0,
                                    "crypto_id": 100,
                                    "issuer_name": "Backed Finance",
                                }
                            ],
                            "tradfi_markets": [],
                        }
                    ]
                }
            ),
            cmc_ok(
                {
                    "rwa_id": 3,
                    "symbol": "AAPL",
                    "num_market_pairs": 0,
                    "market_pairs": [],
                }
            ),
        ]
    )
    client = _live(session)
    nvda = TransparencyScorer(client).score("NVDA")
    aapl = TransparencyScorer(client).score("AAPL")
    assert nvda["ticker"] == "NVDA"
    assert aapl["ticker"] == "AAPL"
    paths = session.paths()
    assert paths.count("/v5/real-world-assets/issuers/list") == 1
    assert paths.count("/v5/real-world-assets/issuers") == 1
    assert paths.count("/v5/real-world-assets/map") == 1
    assert paths.count("/v5/real-world-assets/info") == 2
    assert paths.count("/v5/real-world-assets/market-pairs/list") == 2
    assert paths.count("/v5/real-world-assets/quotes/latest") == 2
    assert "/v2/cryptocurrency/quotes/latest" not in paths
    assert "basis" in nvda["subscores"]
    assert nvda["verification"]["price"]["source"] == "cmc_rwa_quotes"
    assert nvda["cmc_calls"]["live"] is True


def test_info_cache_short_ttl_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 100.0}
    monkeypatch.setattr("rwa_score.client.time.monotonic", lambda: clock["t"])
    session = FakeSession(
        [
            cmc_ok({"rwa_assets": [{"rwa_id": 2, "cik": "first"}]}),
            cmc_ok({"rwa_assets": [{"rwa_id": 2, "cik": "second"}]}),
        ]
    )
    client = _live(session, info_ttl=10.0)
    assert client.rwa_info(2)["cik"] == "first"
    clock["t"] = 109.0
    assert client.rwa_info(2)["cik"] == "first"
    clock["t"] = 110.1
    assert client.rwa_info(2)["cik"] == "second"
    assert session.paths().count("/v5/real-world-assets/info") == 2


def test_parse_market_pairs_payload_normalizes_empty() -> None:
    parsed = parse_market_pairs_payload(None)
    assert parsed["market_pairs"] == []
    assert parsed["num_market_pairs"] == 0
    assert parsed["symbol"] == ""
    assert parsed["rwa_id"] is None


def test_parse_market_pairs_payload_keeps_rows() -> None:
    parsed = parse_market_pairs_payload(
        {
            "rwa_id": "2",
            "symbol": "nvda",
            "num_market_pairs": "3",
            "market_pairs": [{"market_id": 1}, {"market_id": 2}],
            "has_more": True,
        }
    )
    assert parsed["rwa_id"] == 2
    assert parsed["symbol"] == "NVDA"
    assert parsed["num_market_pairs"] == 3
    assert len(parsed["market_pairs"]) == 2
    assert parsed["has_more"] is True


def test_live_market_pairs_requires_one_identifier() -> None:
    session = FakeSession([])
    client = _live(session)
    with pytest.raises(CMCError, match="rwa_id or symbol"):
        client.market_pairs()
    with pytest.raises(CMCError, match="only one"):
        client.market_pairs(rwa_id=2, symbol="NVDA")
    assert session.calls == []


def test_live_market_pairs_fetches_and_caches() -> None:
    session = FakeSession(
        [
            cmc_ok(
                {
                    "rwa_id": 2,
                    "name": "NVIDIA",
                    "symbol": "NVDA",
                    "num_market_pairs": 1,
                    "market_pairs": [
                        {
                            "market_pair": "NVDAX/USDT",
                            "market_pair_base": {"crypto_id": 36992, "symbol": "NVDAx"},
                            "quotes": [{"symbol": "USD", "price": 118.45}],
                        }
                    ],
                }
            )
        ]
    )
    client = _live(session)
    first = client.market_pairs(rwa_id=2)
    second = client.market_pairs(rwa_id=2)
    assert first == second
    assert first["symbol"] == "NVDA"
    assert first["market_pairs"][0]["market_pair"] == "NVDAX/USDT"
    assert session.paths() == ["/v5/real-world-assets/market-pairs/list"]
    assert session.calls[0][1]["rwa_id"] == 2


def test_parse_rwa_map_payload_paginates() -> None:
    parsed = parse_rwa_map_payload(
        {
            "rwa_assets": [{"symbol": "gold", "rwa_id": "1", "asset_type": "commodity"}],
            "total_size": 2,
            "has_more": True,
        }
    )
    assert parsed["rwa_assets"][0]["symbol"] == "GOLD"
    assert parsed["rwa_assets"][0]["rwa_id"] == 1
    assert parsed["has_more"] is True
    assert parsed["total_size"] == 2
    empty = parse_rwa_map_payload(None)
    assert empty["rwa_assets"] == []
    assert empty["has_more"] is False


def test_directory_has_more_uses_total_size_when_flag_omitted() -> None:
    assert directory_has_more(
        {"has_more": False, "total_size": 7805}, start=1, batch_len=250
    )
    assert directory_has_more({"has_more": True, "total_size": 2}, start=1, batch_len=1)
    assert not directory_has_more(
        {"has_more": False, "total_size": 2}, start=1, batch_len=2
    )
    assert not directory_has_more({}, start=1, batch_len=250)


def test_live_map_paginates_when_only_total_size_says_more() -> None:
    session = FakeSession(
        [
            cmc_ok(
                {
                    "total_size": 2,
                    "has_more": False,
                    "rwa_assets": [
                        {"symbol": "GOLD", "rwa_id": 1, "asset_type": "commodity"}
                    ],
                }
            ),
            cmc_ok(
                {
                    "total_size": 2,
                    "has_more": False,
                    "rwa_assets": [
                        {"symbol": "NVDA", "rwa_id": 2, "asset_type": "stock"}
                    ],
                }
            ),
        ]
    )
    client = _live(session, page_gap=0)
    rows = client.rwa_map()
    assert [row["symbol"] for row in rows] == ["GOLD", "NVDA"]
    assert session.paths() == [ENDPOINT_MAP, ENDPOINT_MAP]


def test_live_map_paginates_until_complete() -> None:
    session = FakeSession(
        [
            cmc_ok(
                {
                    "total_size": 2,
                    "has_more": True,
                    "rwa_assets": [
                        {"symbol": "GOLD", "rwa_id": 1, "asset_type": "commodity"}
                    ],
                }
            ),
            cmc_ok(
                {
                    "total_size": 2,
                    "has_more": False,
                    "rwa_assets": [
                        {"symbol": "NVDA", "rwa_id": 2, "asset_type": "stock"}
                    ],
                }
            ),
        ]
    )
    client = _live(session, page_gap=0)
    rows = client.rwa_map()
    assert [row["symbol"] for row in rows] == ["GOLD", "NVDA"]
    assert session.paths() == [ENDPOINT_MAP, ENDPOINT_MAP]
    assert session.calls[0][1]["start"] == 1
    assert session.calls[1][1]["start"] == 2
    again = client.rwa_map()
    assert [row["symbol"] for row in again] == ["GOLD", "NVDA"]
    assert session.paths() == [ENDPOINT_MAP, ENDPOINT_MAP]


def test_live_map_symbol_lookup_is_one_request() -> None:
    session = FakeSession(
        [cmc_ok({"rwa_assets": [{"symbol": "NVDA", "rwa_id": 2, "asset_type": "stock"}]})]
    )
    client = _live(session)
    rows = client.rwa_map("NVDA")
    assert rows[0]["symbol"] == "NVDA"
    assert session.paths() == [ENDPOINT_MAP]
    assert "start" not in (session.calls[0][1] or {})


def test_live_assets_list_all_paginates() -> None:
    session = FakeSession(
        [
            cmc_ok(
                {
                    "total_size": 2,
                    "has_more": True,
                    "rwa_assets": [
                        {"symbol": "GOLD", "rwa_id": 1, "asset_type": "commodity"}
                    ],
                }
            ),
            cmc_ok(
                {
                    "total_size": 2,
                    "has_more": False,
                    "rwa_assets": [
                        {"symbol": "NVDA", "rwa_id": 2, "asset_type": "stock"}
                    ],
                }
            ),
        ]
    )
    client = _live(session, page_gap=0)
    book = client.assets_list_all()
    assert [row["symbol"] for row in book["rwa_assets"]] == ["GOLD", "NVDA"]
    assert book["has_more"] is False
    assert session.paths() == [ENDPOINT_ASSETS_LIST, ENDPOINT_ASSETS_LIST]


def test_default_page_gap_is_zero_and_429_backoff_stays() -> None:
    from rwa_score.client import DEFAULT_PAGE_GAP_SECONDS, RATE_LIMIT_HTTP

    assert DEFAULT_PAGE_GAP_SECONDS == 0.0
    assert RATE_LIMIT_HTTP == 429
    session = FakeSession(
        [cmc_ok({"rwa_assets": [{"symbol": "NVDA", "rwa_id": 2, "asset_type": "stock"}]})]
    )
    client = _live(session)
    assert client.page_gap == 0.0
    source = Path("rwa_score/client.py").read_text(encoding="utf-8")
    assert "_is_rate_limited" in source
    assert "RATE_LIMIT_CMC_CODES" in source
    assert "def _backoff_delay" in source


def test_live_map_limit_is_one_page_not_full_walk() -> None:
    session = FakeSession(
        [
            cmc_ok(
                {
                    "total_size": 7805,
                    "has_more": True,
                    "rwa_assets": [
                        {"symbol": "USTB", "rwa_id": 30, "asset_type": "government_security"}
                    ],
                }
            ),
            cmc_ok(
                {
                    "rwa_assets": [
                        {"symbol": "SHOULD_NOT_FETCH", "rwa_id": 99, "asset_type": "stock"}
                    ]
                }
            ),
        ]
    )
    client = _live(session)
    rows = client.rwa_map(asset_type="government_security", start=1, limit=250)
    assert [row["symbol"] for row in rows] == ["USTB"]
    assert session.paths() == [ENDPOINT_MAP]
    assert session.calls[0][1]["asset_type"] == "government_security"
    assert session.calls[0][1]["limit"] == 250
    again = client.rwa_map(asset_type="government_security", start=1, limit=250)
    assert [row["symbol"] for row in again] == ["USTB"]
    assert session.paths() == [ENDPOINT_MAP]


def test_fixture_map_filters_asset_type() -> None:
    client = FixtureClient()
    gold = client.rwa_map(asset_type="commodity")
    assert {row["symbol"] for row in gold} == {"GOLD"}
    stocks = client.rwa_map(asset_type="stock")
    assert {"NVDA", "TSLA", "AAPL"} <= {row["symbol"] for row in stocks}


def test_fixture_market_pairs_by_symbol() -> None:
    client = FixtureClient()
    by_id = client.market_pairs(rwa_id=16)
    by_symbol = client.market_pairs(symbol="aapl")
    assert by_id["rwa_id"] == 16
    assert by_symbol["symbol"] == "AAPL"
    assert len(by_id["market_pairs"]) == 3
    unknown = client.market_pairs(symbol="ZZZZ")
    assert unknown["market_pairs"] == []
