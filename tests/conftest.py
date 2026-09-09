from __future__ import annotations

from typing import Any

import pytest

from rwa_score.fixtures import FixtureClient
from rwa_score.scorer import TransparencyScorer


class MockClient:
    source = "mock"

    def __init__(
        self,
        *,
        assets: list[dict[str, Any]] | None = None,
        info: dict[int, dict[str, Any]] | None = None,
        issuers: list[dict[str, Any]] | None = None,
        issuer_detail: dict[str, dict[str, Any]] | None = None,
        quotes: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        self.assets = assets or []
        self.info = info or {}
        self.issuers = issuers or []
        self.issuer_detail = issuer_detail or {}
        self.quotes = quotes or {}
        self.calls: list[str] = []

    def rwa_map(self, symbol: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(f"rwa_map:{symbol}")
        if not symbol:
            return list(self.assets)
        wanted = {s.strip().upper() for s in symbol.split(",")}
        return [a for a in self.assets if a.get("symbol") in wanted]

    def rwa_info(self, rwa_id: int) -> dict[str, Any]:
        self.calls.append(f"rwa_info:{rwa_id}")
        return dict(self.info.get(rwa_id, {}))

    def issuers_list(self) -> list[dict[str, Any]]:
        self.calls.append("issuers_list")
        return list(self.issuers)

    def issuer(self, issuer_id: str) -> dict[str, Any]:
        self.calls.append(f"issuer:{issuer_id}")
        return dict(self.issuer_detail.get(issuer_id, {"name": "", "tokens": []}))

    def crypto_quote(self, crypto_id: int) -> dict[str, Any]:
        self.calls.append(f"crypto_quote:{crypto_id}")
        return dict(self.quotes.get(crypto_id, {}))


@pytest.fixture
def fixture_client() -> FixtureClient:
    return FixtureClient()


@pytest.fixture
def fixture_scorer(fixture_client: FixtureClient) -> TransparencyScorer:
    return TransparencyScorer(fixture_client)
