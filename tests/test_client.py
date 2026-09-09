from __future__ import annotations

from typing import Any

import pytest
import requests

from rwa_score.client import CMCClient, CMCError, create_client


class _FakeResp:
    def __init__(self, payload: dict[str, Any], status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


def _session(handler) -> requests.Session:
    session = requests.Session()
    session.get = handler  # type: ignore[method-assign]
    return session


def test_cmc_client_requires_key(monkeypatch) -> None:
    monkeypatch.delenv("CMC_API_KEY", raising=False)
    with pytest.raises(CMCError, match="CMC_API_KEY"):
        CMCClient(api_key="")


def test_rwa_info_sends_rwa_id_not_id() -> None:
    captured: dict[str, Any] = {}

    def handler(url: str, params: dict | None = None, timeout: int | None = None):
        captured["url"] = url
        captured["params"] = params
        return _FakeResp({"data": {"rwa_assets": [{"rwa_id": 2, "cik": "0001045810"}]}})

    client = CMCClient(api_key="dummy-key", session=_session(handler))
    info = client.rwa_info(2)
    assert info["cik"] == "0001045810"
    assert captured["url"].endswith("/v5/real-world-assets/info")
    assert captured["params"] == {"rwa_id": 2}


def test_rwa_map_filters_stocks_when_unscoped() -> None:
    captured: dict[str, Any] = {}

    def handler(url: str, params: dict | None = None, timeout: int | None = None):
        captured["params"] = params
        return _FakeResp({"data": {"rwa_assets": []}})

    CMCClient(api_key="dummy-key", session=_session(handler)).rwa_map()
    assert captured["params"] == {"asset_type": "stock"}


def test_http_error_becomes_cmc_error() -> None:
    def handler(url: str, params: dict | None = None, timeout: int | None = None):
        return _FakeResp({}, status_code=401, text="unauthorized")

    client = CMCClient(api_key="dummy-key", session=_session(handler))
    with pytest.raises(CMCError, match="401"):
        client.rwa_map("NVDA")


def test_cmc_error_code_in_body() -> None:
    def handler(url: str, params: dict | None = None, timeout: int | None = None):
        return _FakeResp({"status": {"error_code": 1001, "error_message": "bad key"}, "data": {}})

    client = CMCClient(api_key="dummy-key", session=_session(handler))
    with pytest.raises(CMCError, match="1001"):
        client.issuers_list()


def test_crypto_quote_unwraps_id_key() -> None:
    def handler(url: str, params: dict | None = None, timeout: int | None = None):
        assert params == {"id": 36992, "convert": "USD"}
        return _FakeResp({"data": {"36992": {"quote": {"USD": {"price": 1}}}}})

    quote = CMCClient(api_key="dummy-key", session=_session(handler)).crypto_quote(36992)
    assert quote["quote"]["USD"]["price"] == 1


def test_create_client_live_uses_cmc(monkeypatch) -> None:
    monkeypatch.setenv("CMC_API_KEY", "dummy-key")
    client = create_client(use_fixtures=False)
    assert isinstance(client, CMCClient)
    assert client.source == "cmc-live"


def test_headers_do_not_echo_in_repr(monkeypatch) -> None:
    client = CMCClient(api_key="super-secret-key")
    assert "super-secret-key" not in repr(client)
