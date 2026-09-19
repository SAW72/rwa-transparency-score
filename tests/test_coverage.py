"""CMC RWA coverage matrix — fixture catalog is complete and labeled."""

from __future__ import annotations

from pathlib import Path

from rwa_score.client import ASSET_TYPES, FixtureClient
from rwa_score.coverage import (
    WIRED_ENDPOINTS,
    coverage_from_client,
    coverage_rows,
    missing_rwa_classes,
)
from rwa_score.ticker_search import RWA_CLASS_IDS, load_search_catalog


def test_fixture_catalog_covers_every_cmc_rwa_class() -> None:
    client = FixtureClient()
    catalog = load_search_catalog(client)
    assert missing_rwa_classes(catalog) == ()
    by_class = {cid: [] for cid in RWA_CLASS_IDS}
    for opt in catalog:
        for cid in opt.categories:
            if cid in by_class:
                by_class[cid].append(opt.symbol)
    assert "NVDA" in by_class["stock"]
    assert "GOLD" in by_class["commodity"]
    assert "USTB" in by_class["government_security"]
    assert "SPY" in by_class["etf"]
    assert "HOME" in by_class["real_estate"]
    assert "EUR" in by_class["currency"]


def test_coverage_matrix_never_claims_live_on_fixtures() -> None:
    client = FixtureClient()
    catalog = load_search_catalog(client)
    rows = coverage_rows(catalog, client_source=client.source)
    assert rows
    assert all(row["status"] in {"fixture", "labeled_por_catalog"} for row in rows)
    assert not any(row["status"] == "live" for row in rows)
    text = coverage_from_client(client)
    assert "not live" in text.lower()
    assert "`stock`" in text
    for endpoint in WIRED_ENDPOINTS:
        assert endpoint in text
    assert "GOLD" in text
    assert "USTB" in text


def test_coverage_doc_matches_fixture_matrix() -> None:
    text = Path("docs/CMC_RWA_COVERAGE.md").read_text(encoding="utf-8")
    for kind in ASSET_TYPES:
        assert f"`{kind}`" in text
    assert "GOLD" in text
    assert "not live" in text.lower()
    assert "/v5/real-world-assets/map" in text
    assert "BTC" in text and "not" in text.lower()
