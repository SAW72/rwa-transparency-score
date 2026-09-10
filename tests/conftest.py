from __future__ import annotations

from typing import Any

import pytest

from rwa_score.client import FixtureClient, create_client
from rwa_score.scorer import TransparencyScorer


@pytest.fixture
def fixture_client() -> FixtureClient:
    return FixtureClient()


@pytest.fixture
def fixture_scorer(fixture_client: FixtureClient) -> TransparencyScorer:
    return TransparencyScorer(fixture_client)


class RecordingClient:
    """Minimal live-shaped client for cache / error-path unit tests."""

    source = "live"

    def __init__(
        self,
        *,
        assets: list[dict[str, Any]] | None = None,
        info: dict[int, dict[str, Any]] | None = None,
        issuers: list[dict[str, Any]] | None = None,
        issuer_details: dict[str, dict[str, Any]] | None = None,
        quotes: dict[int, dict[str, Any]] | None = None,
        quote_error: Exception | None = None,
        market_pairs: dict[int | str, dict[str, Any]] | None = None,
        market_pairs_error: Exception | None = None,
    ) -> None:
        self.assets = assets or [
            {"symbol": "NVDA", "rwa_id": 2},
        ]
        self.info = info or {
            2: {"symbol": "NVDA", "cik": "0001045810", "issuer": {"name": "Backed Finance"}},
        }
        self.issuers = issuers or [{"issuer_id": "abc", "name": "Backed Finance"}]
        self.issuer_details = issuer_details or {
            "abc": {
                "name": "Backed Finance",
                "tokens": [{"rwa_id": 2, "crypto_id": 99}],
            }
        }
        self.quotes = quotes or {
            99: {"quote": {"USD": {"percent_change_24h": 1.0, "price": 100.0}}},
        }
        self.quote_error = quote_error
        self.pairs = market_pairs or {}
        self.market_pairs_error = market_pairs_error
        self.calls = {
            "rwa_map": 0,
            "rwa_info": 0,
            "issuers_list": 0,
            "issuer": 0,
            "crypto_quote": 0,
            "market_pairs": 0,
        }

    def rwa_map(self, symbol: str | None = None) -> list[dict[str, Any]]:
        self.calls["rwa_map"] += 1
        return list(self.assets)

    def rwa_info(self, rwa_id: int) -> dict[str, Any]:
        self.calls["rwa_info"] += 1
        return dict(self.info.get(rwa_id) or {})

    def issuers_list(self) -> list[dict[str, Any]]:
        self.calls["issuers_list"] += 1
        return list(self.issuers)

    def issuer(self, issuer_id: str) -> dict[str, Any]:
        self.calls["issuer"] += 1
        return dict(self.issuer_details.get(issuer_id) or {})

    def crypto_quote(self, crypto_id: int) -> dict[str, Any]:
        self.calls["crypto_quote"] += 1
        if self.quote_error:
            raise self.quote_error
        return dict(self.quotes.get(crypto_id) or {})

    def market_pairs(
        self,
        *,
        rwa_id: int | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        self.calls["market_pairs"] += 1
        if self.market_pairs_error:
            raise self.market_pairs_error
        if rwa_id is not None:
            hit = self.pairs.get(int(rwa_id))
            if hit is None:
                hit = self.pairs.get(str(int(rwa_id)))
            return dict(hit or {})
        if symbol:
            wanted = symbol.strip().upper()
            for payload in self.pairs.values():
                if (payload.get("symbol") or "").upper() == wanted:
                    return dict(payload)
        return {}


@pytest.fixture
def recording_client() -> RecordingClient:
    return RecordingClient()


@pytest.fixture
def factory_client() -> FixtureClient:
    return create_client(use_fixtures_mode=True)
