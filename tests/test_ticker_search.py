"""Prefix ticker search + compact picker assign behavior."""

from __future__ import annotations

from pathlib import Path

from rwa_score.client import FixtureClient
from rwa_score.ticker_search import (
    CATEGORIES,
    CATEGORY_DOC,
    SEARCH_MIN_CHARS,
    TickerOption,
    catalog_from_rwa_map,
    classify_categories,
    format_option,
    load_search_catalog,
    normalize_ticker,
    prefix_matches,
    resolve_assign_symbol,
    resolve_categories,
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
    assert "AI/Tech" in format_option(hits[0])


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
    # Click-to-place / Use strip still resolve the selected match, then the first hit.
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


def test_readme_documents_search_categories() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
    assert "| AI/Tech |" in text
    assert "| Oil/Energy |" in text
    assert "| Real Estate |" in text
    assert "`oil`" in text or "oil" in text
    assert "real estate" in text.lower()


def test_category_taxonomy_is_documented() -> None:
    labels = {cat.label for cat in CATEGORIES}
    assert {"AI/Tech", "Oil/Energy", "Real Estate", "Auto/EV", "Finance"} <= labels
    assert "oil" in CATEGORY_DOC.lower()
    assert "real estate" in CATEGORY_DOC.lower()
    assert "ai/tech" in CATEGORY_DOC.lower()
    assert resolve_categories("AI") == ("ai_tech",)
    assert resolve_categories("oil") == ("oil_energy",)
    assert resolve_categories("real estate") == ("real_estate",)
    assert resolve_categories("ENERGY") == ("oil_energy",)
    assert resolve_categories("reit") == ("real_estate",)
    assert resolve_categories("ni") == ()


def test_fixture_industries_map_to_categories() -> None:
    catalog = _fixture_catalog()
    by_symbol = {opt.symbol: opt for opt in catalog}
    assert "ai_tech" in by_symbol["NVDA"].categories
    assert "ai_tech" in by_symbol["AAPL"].categories
    assert "ai_tech" in by_symbol["META"].categories
    assert "auto_ev" in by_symbol["TSLA"].categories
    assert "oil_energy" in by_symbol["XOM"].categories
    assert "real_estate" in by_symbol["PLD"].categories
    assert by_symbol["XOM"].industry.lower().startswith("petroleum")
    assert "real estate" in by_symbol["PLD"].industry.lower()


def test_category_search_lists_matching_tickers() -> None:
    catalog = _fixture_catalog()
    assert {opt.symbol for opt in search_tickers("AI", catalog)} == {"NVDA", "AAPL", "META"}
    assert [opt.symbol for opt in search_tickers("oil", catalog)] == ["XOM"]
    assert [opt.symbol for opt in search_tickers("real estate", catalog)] == ["PLD"]
    assert [opt.symbol for opt in search_tickers("auto", catalog)] == ["TSLA"]
    assert search_tickers("finance", catalog) == []
    oil = search_tickers("oil", catalog)[0]
    assert "Oil/Energy" in format_option(oil)
    assert "XOM — " in format_option(oil)
    assert resolve_assign_symbol("oil", matches=[oil]) == "XOM"
    assert resolve_assign_symbol("real estate", matches=search_tickers("real estate", catalog)) == "PLD"


def test_category_hits_can_be_scored_from_fixtures(fixture_scorer) -> None:
    import app as demo_app

    catalog = demo_app._ticker_catalog(fixture_scorer)
    for query, symbol in (("oil", "XOM"), ("real estate", "PLD"), ("AI", "AAPL")):
        hits = demo_app.search_tickers(query, catalog)
        assert symbol in {opt.symbol for opt in hits}
        report = demo_app._score_one(fixture_scorer, symbol)
        assert report["ticker"] == symbol
        assert report["score"] >= 0


def test_classify_uses_industry_field_not_invented_tickers() -> None:
    cats = classify_categories(
        symbol="CVX",
        name="Chevron Corp",
        industry="Petroleum Refining",
    )
    assert cats == ("oil_energy",)
    assert classify_categories(symbol="ZZZ", name="Unknown Co", industry="") == ()


def test_app_picker_wires_continuous_category_search_compare() -> None:
    import app as demo_app

    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert "search_tickers" in source
    assert "search_match_" in source
    assert "place_search_match" in source
    assert "st.selectbox" in source
    assert "index=None" in source
    assert "on_change=_on_search_pick" not in source
    assert "Use {opt.symbol}" not in source and 'f"Use {opt.symbol}"' not in source
    assert "use_strip_" not in source
    assert "USE_STRIP_LIMIT" not in source
    assert "on_click=_auto_place" not in source
    assert "@st.fragment" not in source
    assert "_render_search_picker" in source
    assert "Next pick replaces slot" in source
    assert "Next pick fills slot" in source
    assert 'st.button("Assign"' not in source
    assert "assign_clicked" not in source
    assert "search-assign-anchor" not in source
    assert demo_app.SEARCH_MIN_CHARS == 3
    assert demo_app.CANDIDATE_STRIP_LIMIT == 4
    assert demo_app.SEARCH_MATCH_KEY == "search_match_pick"
    assert demo_app.SEARCH_FIELD_MAX == "17rem"
    assert "st.metric" in source
    assert "st.progress" not in source.split("def _render_compare_card", 1)[1].split(
        "def _render_slot_error", 1
    )[0]
    assert "_score_card_html" not in source
    assert "score-hero" not in source
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

    oil_hits = demo_app.search_tickers("oil", catalog)
    assert [opt.symbol for opt in oil_hits] == ["XOM"]
    slots = demo_app.assign_ticker_to_slot(
        list(demo_app.DEFAULT_SLOTS),
        1,
        demo_app.resolve_assign_symbol("oil", matches=oil_hits),
    )
    assert slots[1] == "XOM"

    picker = source.split("def _render_search_picker", 1)[1]
    assert picker.index("_clear_search") < picker.index("cat_chip_")
    assert picker.index("cat_chip_") < picker.index(
        'placeholder="Ticker, name, or category"'
    )
    assert picker.index("st.selectbox") < picker.index("Choose a ticker")
    assert "search_match_pick" in source
    chip_block = picker.split("st.text_input", 1)[0]
    assert "use_container_width=True" not in chip_block
    assert "use_container_width=False" in chip_block
    assert "st.selectbox" in picker
    assert "index=None" in picker
    assert "_auto_place(picked)" in picker
    assert "max-width" in source
    assert "stSelectbox" in source
    assert "z-index: 40" in source
    assert "z-index: 1000" in source
    body = source.split('st.subheader("Score / Compare")', 1)[1]
    assert "_render_search_picker(catalog, use_fixtures)" in body
    assert "search_match_" not in body.split("_render_search_picker", 1)[0]
    assert "cat_chip_" in source
    assert "Ticker, name, or category" in source
    assert 'placeholder="Ticker, name, or category"' in source
    assert 'st.text_input(' in source
    assert '"Search"' in source
    assert "Ticker search" not in source
    assert 'label_visibility="visible"' in source
    assert "stTextInput" in source
    assert "rgba(250, 250, 250, 0.2)" in source
    assert "stVerticalBlockBorderWrapper" not in source
    assert "border=True" not in source
    assert "Browse categories" not in source
    assert "chip_display_label" in source
    assert "Pick a ticker to compare here" in source
    assert demo_app.chip_display_label("AI/Tech") == "AI / Tech"
    assert demo_app.chip_display_label("Oil/Energy") == "Oil / Energy"
    assert demo_app.chip_display_label("Auto/EV") == "Auto / EV"
    assert demo_app.chip_display_label("Real Estate") == "Real Estate"
    assert demo_app.chip_query(CATEGORIES[0]) == "ai"
    assert demo_app.chip_query(CATEGORIES[1]) == "oil"
    assert "st.session_state.ticker_query = chip_query(cat)" in source
    assert 'st.session_state.ticker_query = ""' in source
    assert "_clear_search" in source
    assert "pending_ticker_query" not in source
    assert 'st.expander("Why this score?"' in source
    assert "st.form" not in source

    shown = demo_app.browse_categories(catalog)
    assert [cat.id for cat in shown] == ["ai_tech", "oil_energy", "real_estate", "auto_ev"]
    finance_row = TickerOption(
        symbol="JPM", name="JPMorgan", categories=("finance",)
    )
    expanded = demo_app.browse_categories([*catalog, finance_row])
    assert [cat.id for cat in expanded][-1] == "finance"

    assert demo_app.next_place_index(["", "TSLA", "AAPL", "META"], 2) == 0
    assert demo_app.next_place_index(["NVDA", "", "AAPL", "META"], 0) == 1
    assert demo_app.next_place_index(["NVDA", "TSLA", "AAPL", "META"], 2, "XOM") == 2
    assert demo_app.next_place_index(["NVDA", "TSLA", "AAPL", "META"], 0, "NVDA") == 0
    assert demo_app.default_active_slot(["NVDA", "TSLA", "AAPL", "META"]) == 0
    assert demo_app.default_active_slot(["NVDA", "", "AAPL", "META"]) == 1


def test_select_niv_match_fills_slot_same_path_as_use_chip() -> None:
    import app as demo_app

    catalog = demo_app._ticker_catalog(demo_app._cached_scorer(True))
    niv = demo_app.search_tickers("niv", catalog)
    assert [opt.symbol for opt in niv] == ["NVDA"]
    # Empty first slot — Search match and Use NVDA both fill it via place_search_match.
    from_match, next_active = demo_app.place_search_match(
        ["", "TSLA", "AAPL", "META"], 0, niv[0].symbol
    )
    from_use, use_next = demo_app.place_search_match(
        ["", "TSLA", "AAPL", "META"], 0, "NVDA"
    )
    assert from_match == from_use == ["NVDA", "TSLA", "AAPL", "META"]
    assert next_active == use_next == 1
    # Full default row: replace the active slot (0), then advance.
    replaced, nxt = demo_app.place_search_match(
        list(demo_app.DEFAULT_SLOTS), 0, niv[0].symbol
    )
    assert replaced == ["NVDA", "TSLA", "AAPL", "META"]
    assert nxt == 1
    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert "_auto_place(picked)" in source
    assert "use_strip_" not in source
    assert "on_click=_auto_place" not in source


def test_oil_typeahead_match_places_xom() -> None:
    import app as demo_app

    catalog = demo_app._ticker_catalog(demo_app._cached_scorer(True))
    oil = demo_app.search_tickers("oil", catalog)
    assert [opt.symbol for opt in oil] == ["XOM"]
    slots, active = demo_app.place_search_match(
        list(demo_app.DEFAULT_SLOTS), 1, oil[0].symbol
    )
    assert slots[1] == "XOM"
    assert slots[0] == "NVDA"
    assert active == 2


def test_full_row_replace_advances_cursor() -> None:
    import app as demo_app

    slots, active = demo_app.place_search_match(
        ["NVDA", "TSLA", "AAPL", "META"], 0, "XOM"
    )
    assert slots == ["XOM", "TSLA", "AAPL", "META"]
    assert active == 1
    slots, active = demo_app.place_search_match(slots, active, "PLD")
    assert slots[1] == "PLD"
    assert active == 2


def test_full_board_replace_follows_active_then_wraps() -> None:
    """QA: full NVDA/NVDA/AAPL/META + active 2 → oil/XOM replaces slot 3."""
    import app as demo_app

    board = ["NVDA", "NVDA", "AAPL", "META"]
    slots, active = demo_app.place_search_match(board, 2, "XOM")
    assert slots == ["NVDA", "NVDA", "XOM", "META"]
    assert active == 3
    slots, active = demo_app.place_search_match(slots, active, "PLD")
    assert slots == ["NVDA", "NVDA", "XOM", "PLD"]
    assert active == 0
    slots, active = demo_app.place_search_match(slots, 0, "TSLA")
    assert slots[0] == "TSLA"
    assert active == 1
    # Empty wins over a clicked filled slot.
    slots, active = demo_app.place_search_match(
        ["NVDA", "", "AAPL", ""], 3, "XOM"
    )
    assert slots[1] == "XOM"
    assert slots[0] == "NVDA"
    assert active == 3


def test_compare_card_helpers_short_name_mode_and_weakest() -> None:
    import app as demo_app

    assert demo_app.short_company_name("Nvidia Corp") == "Nvidia"
    assert demo_app.short_company_name("Apple Inc") == "Apple"
    assert demo_app.short_company_name("Meta Platforms Inc") == "Meta Platforms"
    assert demo_app.short_company_name("Exxon Mobil Corp") == "Exxon Mobil"
    catalog = demo_app._ticker_catalog(demo_app._cached_scorer(True))
    assert demo_app.catalog_company("NVDA", catalog) == "Nvidia"
    assert demo_app.catalog_company("XOM", catalog) == "Exxon Mobil"
    assert demo_app.mode_cue({"data_source": "fixture"}) == "FIXTURE"
    assert demo_app.mode_cue({"data_source": "live"}) == "LIVE"
    report = {
        "subscores": {
            "backing": 80,
            "reserves": 40,
            "redemption": 70,
            "price": 65,
            "disclosure": 75,
            "basis": 55,
        }
    }
    assert demo_app.weakest_pillar_line(report) == "Weakest: Proof of reserves 40"
    dots = demo_app.pillar_dots(report)
    assert len(dots) == 6
    assert "●" in dots
    assert "○" in dots or "◐" in dots


def test_place_and_auto_place_do_not_score(monkeypatch) -> None:
    """Use/match slot fill is session-only — no scorer or explainer on this path."""
    import inspect

    import app as demo_app

    def boom(*_args, **_kwargs):
        raise AssertionError("place_search_match / _auto_place must not score")

    monkeypatch.setattr(demo_app, "_score_one", boom)
    monkeypatch.setattr(demo_app, "_score_slots", boom)
    monkeypatch.setattr(demo_app, "_cached_explanation", boom)
    monkeypatch.setattr(demo_app, "explain_score", boom)

    place_src = inspect.getsource(demo_app.place_search_match)
    auto_src = inspect.getsource(demo_app._auto_place)
    assert "score" not in place_src.lower()
    assert "explain" not in place_src.lower()
    assert "score" not in auto_src.lower()
    assert "explain" not in auto_src.lower()

    slots, active = demo_app.place_search_match(
        list(demo_app.DEFAULT_SLOTS), 0, "XOM"
    )
    assert slots == ["XOM", "TSLA", "AAPL", "META"]
    assert active == 1

    class _FakeSS(dict):
        def __getattr__(self, name):
            try:
                return self[name]
            except KeyError as exc:
                raise AttributeError(name) from exc

        def __setattr__(self, name, value):
            self[name] = value

    monkeypatch.setattr(demo_app.st, "session_state", _FakeSS())
    demo_app._auto_place("XOM")
    assert demo_app.st.session_state.slots[0] == "XOM"
    assert demo_app.st.session_state.active_slot == 1
    assert demo_app.st.session_state["_clear_search"] is True
