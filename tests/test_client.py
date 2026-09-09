from __future__ import annotations

import json
from pathlib import Path

import pytest

from rwa_score.client import CMCClient, CMCError, FixtureClient, create_client, env_flag, use_fixtures
from rwa_score.fixtures import DEMO_FIXTURE_PATH


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
