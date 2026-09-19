from __future__ import annotations

from typing import Any

import pytest

from rwa_score.client import (
    ENDPOINT_ASSETS_LIST,
    ENDPOINT_CRYPTO_QUOTE,
    ENDPOINT_INFO,
    ENDPOINT_ISSUERS,
    ENDPOINT_ISSUERS_LIST,
    ENDPOINT_MAP,
    ENDPOINT_MARKET_PAIRS,
    ENDPOINT_QUOTES,
    FixtureClient,
    create_client,
    parse_assets_list_payload,
    parse_rwa_quotes_payload,
)
from rwa_score.scorer import TransparencyScorer


@pytest.fixture(autouse=True)
def _clear_search_catalog_memo() -> None:
    """Isolate lazy class-shard memos across tests."""
    from rwa_score.ticker_search import clear_catalog_cache

    clear_catalog_cache()
    try:
        import app as demo_app

        demo_app._catalog_shard_memo.clear()
    except Exception:
        pass
    yield
    clear_catalog_cache()
    try:
        import app as demo_app

        demo_app._catalog_shard_memo.clear()
    except Exception:
        pass


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
        rwa_quotes: dict[int | str, dict[str, Any]] | None = None,
        rwa_quotes_error: Exception | None = None,
        assets_list: list[dict[str, Any]] | None = None,
        assets_list_error: Exception | None = None,
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
        self.rwa_quote_rows = rwa_quotes or {}
        self.rwa_quotes_error = rwa_quotes_error
        self.listed_assets = assets_list
        self.assets_list_error = assets_list_error
        self.pairs = market_pairs or {}
        self.market_pairs_error = market_pairs_error
        self._journal: list[dict[str, Any]] = []
        self.calls = {
            "rwa_map": 0,
            "rwa_info": 0,
            "issuers_list": 0,
            "issuer": 0,
            "crypto_quote": 0,
            "rwa_quotes": 0,
            "assets_list": 0,
            "market_pairs": 0,
        }

    def begin_run(self) -> None:
        self._journal = []

    def call_log(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._journal]

    def _record(self, endpoint: str) -> None:
        self._journal.append(
            {
                "endpoint": endpoint,
                "source": "live" if self.source != "fixture" else "fixture",
                "via": "network",
                "cached": False,
            }
        )

    def rwa_map(
        self,
        symbol: str | None = None,
        *,
        asset_type: str | None = None,
        start: int = 1,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self.calls["rwa_map"] += 1
        self._record(ENDPOINT_MAP)
        rows = list(self.assets)
        kind = (asset_type or "").strip().lower()
        if kind:
            rows = [row for row in rows if (row.get("asset_type") or "").lower() == kind]
        if symbol:
            wanted = {part.strip().upper() for part in symbol.split(",") if part.strip()}
            rows = [row for row in rows if (row.get("symbol") or "").upper() in wanted]
        if limit is not None:
            size = max(1, int(limit))
            offset = max(0, int(start) - 1)
            rows = rows[offset : offset + size]
        return rows

    def rwa_info(self, rwa_id: int) -> dict[str, Any]:
        self.calls["rwa_info"] += 1
        self._record(ENDPOINT_INFO)
        return dict(self.info.get(rwa_id) or {})

    def issuers_list(self) -> list[dict[str, Any]]:
        self.calls["issuers_list"] += 1
        self._record(ENDPOINT_ISSUERS_LIST)
        return list(self.issuers)

    def issuer(self, issuer_id: str) -> dict[str, Any]:
        self.calls["issuer"] += 1
        self._record(ENDPOINT_ISSUERS)
        return dict(self.issuer_details.get(issuer_id) or {})

    def crypto_quote(self, crypto_id: int) -> dict[str, Any]:
        self.calls["crypto_quote"] += 1
        self._record(ENDPOINT_CRYPTO_QUOTE)
        if self.quote_error:
            raise self.quote_error
        return dict(self.quotes.get(crypto_id) or {})

    def rwa_quotes(
        self,
        *,
        rwa_id: int | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        self.calls["rwa_quotes"] += 1
        self._record(ENDPOINT_QUOTES)
        if self.rwa_quotes_error:
            raise self.rwa_quotes_error
        if rwa_id is not None:
            hit = self.rwa_quote_rows.get(int(rwa_id))
            if hit is None:
                hit = self.rwa_quote_rows.get(str(int(rwa_id)))
            return parse_rwa_quotes_payload(hit or {})
        if symbol:
            wanted = symbol.strip().upper()
            for payload in self.rwa_quote_rows.values():
                if (payload.get("symbol") or "").upper() == wanted:
                    return parse_rwa_quotes_payload(payload)
        return parse_rwa_quotes_payload({})

    def assets_list(
        self,
        *,
        asset_type: str | None = None,
        start: int = 1,
        limit: int = 100,
        sort: str = "rwa_rank",
        sort_dir: str = "asc",
    ) -> dict[str, Any]:
        self.calls["assets_list"] += 1
        self._record(ENDPOINT_ASSETS_LIST)
        if self.assets_list_error:
            raise self.assets_list_error
        rows = list(self.listed_assets or [])
        kind = (asset_type or "").strip().lower()
        if kind:
            rows = [row for row in rows if (row.get("asset_type") or "").lower() == kind]
        _ = (start, limit, sort, sort_dir)
        return parse_assets_list_payload(
            {"rwa_assets": rows, "total_size": len(rows), "has_more": False}
        )

    def assets_list_all(
        self,
        *,
        asset_type: str | None = None,
        sort: str = "rwa_rank",
        sort_dir: str = "asc",
    ) -> dict[str, Any]:
        return self.assets_list(
            asset_type=asset_type, start=1, limit=250, sort=sort, sort_dir=sort_dir
        )

    def market_pairs(
        self,
        *,
        rwa_id: int | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        self.calls["market_pairs"] += 1
        self._record(ENDPOINT_MARKET_PAIRS)
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
