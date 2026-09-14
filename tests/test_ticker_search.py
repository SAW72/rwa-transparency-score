"""Prefix ticker search + compact picker assign behavior."""

from __future__ import annotations

from pathlib import Path

from rwa_score.client import FixtureClient
from rwa_score.ticker_search import (
    SEARCH_MIN_CHARS,
    TickerOption,
    catalog_from_rwa_map,
    format_option,
    load_search_catalog,
    normalize_ticker,
    prefix_matches,
    resolve_assign_symbol,
    search_tickers,
)
from tests.conftest import RecordingClient


def _fixture_catalog() -> list[TickerOption]:
    return load_search_catalog(FixtureClient())


def test_normalize_ticker_strips_and_uppercases() -> None:
    assert normalize_ticker("  niv  ") == "NIV"
    assert normalize_ticker("nvda") == "NVDA"
    assert normalize_ticker("") == ""
    assert normalize_ticker(None) == ""  # type: ignore[arg-type]


def test_search_requires_min_prefix_length() -> None:
    catalog = _fixture_catalog()
    assert SEARCH_MIN_CHARS == 3
    assert search_tickers("ni", catalog) == []
    assert search_tickers("  ni", catalog) == []
    assert search_tickers("", catalog) == []


def test_niv_prefix_matches_nvda_nvidia_in_fixture_directory() -> None:
    catalog = _fixture_catalog()
    symbols = {opt.symbol for opt in catalog}
    assert {"NVDA", "TSLA", "AAPL", "META"} <= symbols
    nvidia = next(opt for opt in catalog if opt.symbol == "NVDA")
    assert nvidia.name.lower().startswith("nvidia")
    # Strict prefix of the name is NVI…; NIV is the adjacent swap Spencer types.
    assert prefix_matches("nvi", nvidia)
    assert prefix_matches("niv", nvidia)
    assert prefix_matches("NIV", nvidia)
    assert prefix_matches("Nvd", nvidia)

    hits = search_tickers("niv", catalog)
    assert [opt.symbol for opt in hits] == ["NVDA"]
    assert "nvidia" in hits[0].name.lower()
    assert "NVDA — " in format_option(hits[0])
    assert "Nvidia" in format_option(hits[0])


def test_prefix_matches_ticker_symbol_and_common_name() -> None:
    catalog = _fixture_catalog()
    assert [opt.symbol for opt in search_tickers("nvd", catalog)] == ["NVDA"]
    assert [opt.symbol for opt in search_tickers("tes", catalog)] == ["TSLA"]
    assert [opt.symbol for opt in search_tickers("aap", catalog)] == ["AAPL"]
    assert [opt.symbol for opt in search_tickers("meta", catalog)] == ["META"]
    assert [opt.symbol for opt in search_tickers("apple", catalog)] == ["AAPL"]
    assert search_tickers("zzz", catalog) == []


def test_catalog_uses_rwa_map_aliases_when_present() -> None:
    catalog = catalog_from_rwa_map(
        [
            {
                "symbol": "nvda",
                "name": "Nvidia Corp",
                "slug": "nvidia",
                "aliases": ["Nvidia"],
            },
            {"symbol": "", "name": "skip me"},
            {"symbol": "NVDA", "name": "duplicate ignored"},
        ]
    )
    assert len(catalog) == 1
    assert catalog[0].symbol == "NVDA"
    assert "nvidia" in {a.lower() for a in catalog[0].aliases}
    hits = search_tickers("nvid", catalog)
    assert [opt.symbol for opt in hits] == ["NVDA"]


def test_exact_symbol_ranks_ahead_of_name_prefix() -> None:
    catalog = catalog_from_rwa_map(
        [
            {"symbol": "NIV", "name": "Nivea Demo"},
            {"symbol": "NVDA", "name": "Nvidia Corp"},
        ]
    )
    hits = search_tickers("niv", catalog)
    assert [opt.symbol for opt in hits] == ["NIV", "NVDA"]


def test_picker_assign_uses_selected_match_then_first_match() -> None:
    catalog = _fixture_catalog()
    matches = search_tickers("NIV", catalog)
    assert matches[0].symbol == "NVDA"
    # Streamlit selectbox defaults to the first match — Assign must use it.
    assert resolve_assign_symbol("NIV", matches=matches) == "NVDA"
    assert resolve_assign_symbol("niv", matches=matches, selected_symbol="NVDA") == "NVDA"
    # User can still pick another row if the picker listed more than one.
    assert resolve_assign_symbol("niv", matches=matches, selected_symbol=" tsla ") == "TSLA"
    # No directory hits → legacy exact-assign of the typed ticker.
    assert resolve_assign_symbol("NIV", matches=[]) == "NIV"
    assert resolve_assign_symbol("  msft ", matches=None) == "MSFT"
    assert resolve_assign_symbol("   ", matches=[]) == ""


def test_load_search_catalog_survives_map_errors() -> None:
    class Boom:
        def rwa_map(self, symbol=None):
            raise RuntimeError("map down")

    assert load_search_catalog(Boom()) == []


def test_live_directory_is_whatever_rwa_map_already_loads() -> None:
    client = RecordingClient(
        assets=[
            {"symbol": "NVDA", "name": "Nvidia Corp", "rwa_id": 2, "slug": "nvidia"},
            {"symbol": "MSFT", "name": "Microsoft Corp", "rwa_id": 99},
        ]
    )
    catalog = load_search_catalog(client)
    assert client.calls["rwa_map"] == 1
    assert [opt.symbol for opt in search_tickers("niv", catalog)] == ["NVDA"]
    assert [opt.symbol for opt in search_tickers("mic", catalog)] == ["MSFT"]
    load_search_catalog(client)
    assert client.calls["rwa_map"] == 2


def test_app_picker_wires_directory_into_assign() -> None:
    import app as demo_app

    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert "search_tickers" in source
    assert "st.selectbox" in source
    assert "Matching tickers" in source
    assert demo_app.SEARCH_MIN_CHARS == 3
    assert demo_app.normalize_ticker is normalize_ticker

    catalog = demo_app._ticker_catalog(demo_app._cached_scorer(True))
    matches = demo_app.search_tickers("niv", catalog)
    assert [opt.symbol for opt in matches] == ["NVDA"]
    assert demo_app.resolve_assign_symbol("niv", matches=matches) == "NVDA"
    slots = demo_app.assign_ticker_to_slot(
        list(demo_app.DEFAULT_SLOTS),
        0,
        demo_app.resolve_assign_symbol("niv", matches=matches),
    )
    assert slots[0] == "NVDA"
    assert slots[1:] == ["TSLA", "AAPL", "META"]
