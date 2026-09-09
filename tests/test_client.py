from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rwa_score.client import (
    BASE_URL,
    CMCClient,
    CMCError,
    FixtureClient,
    create_client,
    env_flag,
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
            cmc_ok({"99": {"quote": {"USD": {"percent_change_24h": 1.0, "price": 10.0}}}}),
            cmc_ok({"rwa_assets": [{"symbol": "AAPL", "rwa_id": 3, "cik": "0000320193"}]}),
            cmc_ok({"100": {"quote": {"USD": {"percent_change_24h": 2.0, "price": 20.0}}}}),
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
