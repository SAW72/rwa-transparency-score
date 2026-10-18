from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rwa_score.client import (
    CMCError,
    CMCPlanBlockedError,
    ENDPOINT_MAP,
    ENDPOINT_MARKET_PAIRS,
    FixtureClient,
    PLAN_BLOCKED_LABEL,
    canonical_asset_type,
    create_client,
    directory_has_more,
    env_flag,
    is_cmc_plan_blocked,
    is_market_pairs_plan_block,
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

def test_canonical_asset_type_maps_treasury_aliases() -> None:
    assert canonical_asset_type("government_security") == "government_security"
    assert canonical_asset_type("Government Security") == "government_security"
    assert canonical_asset_type("treasury") == "government_security"
    assert canonical_asset_type("treasuries") == "government_security"
    assert canonical_asset_type("fixed income") == "government_security"
    assert canonical_asset_type("stock") == "stock"
    assert canonical_asset_type("not-a-class") == ""
    parsed = parse_rwa_map_payload(
        {
            "rwa_assets": [
                {"symbol": "USTB", "rwa_id": 30, "asset_type": "treasury"},
                {"symbol": "OUSG", "rwa_id": 31, "assetType": "Government Security"},
                {"symbol": "TBILL", "rwa_id": 32, "asset_type": "etf"},
                {"symbol": "FOO", "rwa_id": 33, "asset_type": "not-a-class"},
            ]
        }
    )
    assert [row["asset_type"] for row in parsed["rwa_assets"]] == [
        "government_security",
        "government_security",
        "etf",
        "not-a-class",
    ]

def test_directory_has_more_uses_total_size_when_flag_omitted() -> None:
    assert directory_has_more(
        {"has_more": False, "total_size": 7805}, start=1, batch_len=250
    )
    assert directory_has_more({"has_more": True, "total_size": 2}, start=1, batch_len=1)
    assert not directory_has_more(
        {"has_more": False, "total_size": 2}, start=1, batch_len=2
    )
    assert not directory_has_more({}, start=1, batch_len=250)

def test_fixture_map_filters_asset_type() -> None:
    client = FixtureClient()
    gold = client.rwa_map(asset_type="commodity")
    assert {row["symbol"] for row in gold} == {"GOLD"}
    stocks = client.rwa_map(asset_type="stock")
    assert {"NVDA", "TSLA", "AAPL"} <= {row["symbol"] for row in stocks}

def test_is_cmc_plan_blocked_detects_1006_and_plan_403() -> None:
    assert is_cmc_plan_blocked(
        403, {"status": {"error_code": 1006, "error_message": "plan"}}
    )
    assert is_cmc_plan_blocked(
        403,
        {
            "status": {
                "error_code": 0,
                "error_message": "Your API Key subscription plan doesn't support this endpoint.",
            }
        },
    )
    assert not is_cmc_plan_blocked(500, {"status": {"error_code": 0}})
    assert is_market_pairs_plan_block(403, None, ENDPOINT_MARKET_PAIRS)
    assert not is_market_pairs_plan_block(403, None, ENDPOINT_MAP)

def test_fixture_market_pairs_by_symbol() -> None:
    client = FixtureClient()
    by_id = client.market_pairs(rwa_id=16)
    by_symbol = client.market_pairs(symbol="aapl")
    assert by_id["rwa_id"] == 16
    assert by_symbol["symbol"] == "AAPL"
    assert len(by_id["market_pairs"]) == 3
    unknown = client.market_pairs(symbol="ZZZZ")
    assert unknown["market_pairs"] == []
