"""Prefix ticker search + compact picker assign behavior."""

from __future__ import annotations

from pathlib import Path

from rwa_score.chainlink_por import BACKED_POR_FEEDS
from rwa_score.client import FixtureClient
from rwa_score.ticker_search import (
    BACKED_SEARCH_SOURCE,
    CATEGORIES,
    CATEGORY_BY_ID,
    CATEGORY_DOC,
    CRYPTO_ID,
    NATIVE_CRYPTO_SYMBOLS,
    RWA_CLASS_CATEGORIES,
    RWA_CLASS_IDS,
    SEARCH_MIN_CHARS,
    TREASURY_CLASS,
    TREASURY_PROBE_SYMBOLS,
    TickerOption,
    cached_class_catalog,
    catalog_from_por_feeds,
    catalog_from_rwa_map,
    classes_for_query,
    classify_categories,
    format_option,
    is_native_crypto,
    is_treasury_like,
    load_class_catalog,
    load_search_catalog,
    merge_search_catalog,
    normalize_ticker,
    prefix_matches,
    resolve_assign_symbol,
    resolve_categories,
    search_tickers,
)
from tests.conftest import RecordingClient

BACKED_BTOKEN_SYMBOLS = {"bNVDA", "bIB01", "bCSPX", "bC3M", "bIBTA"}


def _fixture_catalog() -> list[TickerOption]:
    return load_search_catalog(FixtureClient())


def test_normalize_ticker_strips_and_uppercases() -> None:
    assert normalize_ticker("  niv  ") == "NIV"
    assert normalize_ticker("nvda") == "NVDA"
    assert normalize_ticker("bnvda") == "bNVDA"
    assert normalize_ticker("BNVDA") == "bNVDA"
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
    nvd = [opt.symbol for opt in search_tickers("nvd", catalog)]
    assert nvd[0] == "NVDA"
    assert "bNVDA" in nvd
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

    catalog = load_search_catalog(Boom())
    assert {opt.symbol for opt in catalog} == BACKED_BTOKEN_SYMBOLS
    assert all(opt.source == BACKED_SEARCH_SOURCE for opt in catalog)


def test_live_directory_is_whatever_rwa_map_already_loads() -> None:
    client = RecordingClient(
        assets=[
            {"symbol": "NVDA", "name": "Nvidia Corp", "rwa_id": 2, "slug": "nvidia"},
            {"symbol": "MSFT", "name": "Microsoft Corp", "rwa_id": 99},
        ]
    )
    catalog = load_search_catalog(client)
    assert client.calls["rwa_map"] >= 1
    assert [opt.symbol for opt in search_tickers("niv", catalog)] == ["NVDA"]
    assert [opt.symbol for opt in search_tickers("mic", catalog)] == ["MSFT"]
    before = client.calls["rwa_map"]
    load_search_catalog(client)
    assert client.calls["rwa_map"] == before


def test_readme_documents_search_categories() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
    assert "| Stocks |" in text
    assert "| Commodities |" in text
    assert "| Treasuries |" in text
    assert "| ETFs |" in text
    assert "| Real Estate |" in text
    assert "| Currencies |" in text
    assert "`stock`" in text
    assert "government_security" in text
    assert "docs/CMC_RWA_COVERAGE.md" in text
    assert "bNVDA" in text
    assert "BACKED_POR_FEEDS" in text
    assert "first-class Matches" in text or "first-class" in text
    assert "exactly** the six" in text or "exactly the six" in text.lower()
    assert "| Crypto / Digital Assets |" not in text
    assert "not** chips on that bar" in text or "not chips on that bar" in text.lower()


def test_category_taxonomy_is_documented() -> None:
    labels = {cat.label for cat in CATEGORIES}
    assert {"Stocks", "Commodities", "Treasuries", "ETFs", "Real Estate", "Currencies"} <= labels
    assert {"AI/Tech", "Oil/Energy", "Auto/EV", "Finance"} <= labels
    assert "Crypto / Digital Assets" in labels
    assert tuple(cat.id for cat in RWA_CLASS_CATEGORIES) == RWA_CLASS_IDS
    assert "oil" in CATEGORY_DOC.lower()
    assert "real estate" in CATEGORY_DOC.lower()
    assert "ai/tech" in CATEGORY_DOC.lower()
    assert "government_security" in CATEGORY_DOC.lower()
    assert resolve_categories("AI") == ("ai_tech",)
    assert resolve_categories("oil") == ("oil_energy",)
    assert resolve_categories("real estate") == ("real_estate",)
    assert resolve_categories("ENERGY") == ("oil_energy",)
    assert resolve_categories("reit") == ("real_estate",)
    assert resolve_categories("stock") == ("stock",)
    assert resolve_categories("treasury") == ("government_security",)
    assert resolve_categories("ni") == ()
    assert resolve_categories("crypto") == ()
    assert resolve_categories("digital assets") == ()


def test_fixture_industries_map_to_categories() -> None:
    catalog = _fixture_catalog()
    by_symbol = {opt.symbol: opt for opt in catalog}
    assert "ai_tech" in by_symbol["NVDA"].categories
    assert "ai_tech" in by_symbol["AAPL"].categories
    assert "ai_tech" in by_symbol["META"].categories
    assert "auto_ev" in by_symbol["TSLA"].categories
    assert "oil_energy" in by_symbol["XOM"].categories
    assert "real_estate" in by_symbol["PLD"].categories
    assert "stock" in by_symbol["NVDA"].categories
    assert "commodity" in by_symbol["GOLD"].categories
    assert "government_security" in by_symbol["USTB"].categories
    assert "etf" in by_symbol["SPY"].categories
    assert "currency" in by_symbol["EUR"].categories
    assert "real_estate" in by_symbol["HOME"].categories
    assert by_symbol["XOM"].industry.lower().startswith("petroleum")
    assert "real estate" in by_symbol["PLD"].industry.lower()


def test_category_search_lists_matching_tickers() -> None:
    catalog = _fixture_catalog()
    assert {opt.symbol for opt in search_tickers("AI", catalog)} == {"NVDA", "AAPL", "META"}
    assert "GOLD" not in {opt.symbol for opt in search_tickers("AI", catalog)}
    assert [opt.symbol for opt in search_tickers("oil", catalog)] == ["XOM"]
    assert {opt.symbol for opt in search_tickers("real estate", catalog)} == {"HOME", "PLD"}
    assert [opt.symbol for opt in search_tickers("auto", catalog)] == ["TSLA"]
    assert search_tickers("finance", catalog) == []
    assert "GOLD" in {opt.symbol for opt in search_tickers("commodity", catalog)}
    assert "USTB" in {opt.symbol for opt in search_tickers("treasury", catalog)}
    oil = search_tickers("oil", catalog)[0]
    assert "Oil/Energy" in format_option(oil)
    assert "XOM — " in format_option(oil)
    assert resolve_assign_symbol("oil", matches=[oil]) == "XOM"
    estate = search_tickers("real estate", catalog)
    assert {opt.symbol for opt in estate} == {"HOME", "PLD"}
    assert resolve_assign_symbol("real estate", matches=estate) in {"HOME", "PLD"}


def test_category_hits_can_be_scored_from_fixtures(fixture_scorer) -> None:
    import app as demo_app

    catalog = demo_app._ticker_catalog(fixture_scorer)
    for query, symbol in (
        ("oil", "XOM"),
        ("real estate", "PLD"),
        ("AI", "AAPL"),
        ("commodity", "GOLD"),
        ("treasury", "USTB"),
    ):
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
    assert demo_app.matches_limit_for_query("stock") == demo_app.CLASS_PAGE_LIMIT
    assert demo_app.matches_limit_for_query("NVD") == 4
    assert demo_app.matches_limit_for_query("MSAI") == 4
    assert demo_app.LIVE_UNAVAILABLE_BANNER == "Live data unavailable"
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
    assert picker.index("_clear_search") < picker.index("rat-cat-row")
    assert picker.index("rat-cat-pill") < picker.index(
        'placeholder="Ticker, name, or category"'
    )
    assert picker.index("st.selectbox") < picker.index("Choose a ticker")
    assert "search_match_pick" in source
    chip_block = picker.split("st.text_input", 1)[0]
    assert "rat-cat-row" in chip_block
    assert "flex-direction: row" in source or "rat-cat-row" in source
    assert "rwa_class_" in chip_block
    assert "st.button(" in chip_block
    assert "st.columns(" in chip_block
    assert "st.selectbox" in picker
    assert "index=None" in picker
    assert "_auto_place(picked)" in picker
    assert "_install_search_typeahead" in picker
    assert "on_change=_on_search_query_change" in picker
    assert picker.index("st.text_input") < picker.index("_install_search_typeahead")
    assert picker.index("_install_search_typeahead") < picker.index("search_matches")
    assert "st.container(height=" not in picker
    assert "st.radio(" not in picker
    # Class browse is one compact ticker dropdown, not a selectbox menu,
    # not paged buttons, and not a full-class radio (Aw Snap on the pill rerun).
    browse_at = picker.index("_render_class_browse(")
    select_at = picker.index("st.selectbox")
    assert browse_at < select_at
    assert "is_class_browse_query(query)" in picker[:browse_at]
    assert 'key="rwa_class_browse"' in source
    assert "MATCHES_PAGE_SIZE" not in source
    assert "class_match_window" not in source
    assert "rwa_match_prev" not in source
    assert "rwa_match_next" not in source
    assert "classBrowse" in demo_app.SEARCH_TYPEAHEAD_JS
    assert "st-key-rwa_class_browse" in demo_app.SEARCH_TYPEAHEAD_JS
    assert "removeTickerDropdown" in demo_app.SEARCH_TYPEAHEAD_JS
    assert "MutationObserver" not in demo_app.TICKER_DROPDOWN_JS
    assert "data-testid=\"stRadio\"" not in demo_app.SEARCH_TYPEAHEAD_JS
    assert demo_app.SEARCH_TYPEAHEAD_JS.index("menuOpen") < demo_app.SEARCH_TYPEAHEAD_JS.index(
        "classBrowse"
    )
    assert "attachSoon" in demo_app.SEARCH_TYPEAHEAD_JS
    assert "keepFocus()" in demo_app.SEARCH_TYPEAHEAD_JS
    assert "max-width" in source
    assert "stSelectbox" in source
    assert "z-index: 40" in source
    assert "z-index: 1000" in source
    body = source.split('st.subheader("Score / Compare")', 1)[1]
    assert "_render_search_picker(catalog, use_fixtures, client=scorer.client, scorer=scorer)" in body
    assert picker.index("catalog_for_active_query") < picker.index("search_matches")
    assert picker.index("st.text_input") < picker.index("catalog_for_active_query")
    assert "on_click=_on_class_pill" in chip_block
    assert "search_match_" not in body.split("_render_search_picker", 1)[0]
    assert "rat-cat-pill" in source
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
    assert demo_app.chip_query(RWA_CLASS_CATEGORIES[0]) == "stock"
    assert demo_app.chip_query(RWA_CLASS_CATEGORIES[1]) == "commodity"
    assert demo_app.chip_query(CATEGORY_BY_ID["government_security"]) == (
        "government_security"
    )
    assert "st.session_state.ticker_query = chip_query(cat)" in source
    assert 'st.session_state.ticker_query = ""' in source
    assert "_clear_search" in source
    assert "pending_ticker_query" not in source
    assert "Why this score?" in source
    assert "open_expander" in source
    assert "st.form" not in source
    assert demo_app.SEARCH_TYPEAHEAD_DEBOUNCE_MS == 150
    assert "addEventListener(\"input\"" in demo_app.SEARCH_TYPEAHEAD_JS
    assert "addEventListener(\"keyup\"" in demo_app.SEARCH_TYPEAHEAD_JS
    assert "keypress" in demo_app.SEARCH_TYPEAHEAD_JS
    assert "keydown" in demo_app.SEARCH_TYPEAHEAD_JS
    assert "applyChipQuery" not in demo_app.SEARCH_TYPEAHEAD_JS
    assert "_install_search_typeahead" in source
    assert "search_typeahead_script" in source

    shown = demo_app.browse_categories(catalog)
    assert [cat.id for cat in shown] == list(RWA_CLASS_IDS)
    assert {cat.label for cat in shown} == {
        "Stocks",
        "Commodities",
        "Treasuries",
        "ETFs",
        "Real Estate",
        "Currencies",
    }
    assert "ai_tech" not in {cat.id for cat in shown}
    assert "crypto_digital" not in {cat.id for cat in shown}
    finance_row = TickerOption(
        symbol="JPM", name="JPMorgan", categories=("finance",)
    )
    crypto_row = TickerOption(
        symbol="BTC", name="Bitcoin", categories=("crypto_digital",)
    )
    expanded = demo_app.browse_categories([*catalog, finance_row, crypto_row])
    assert [cat.id for cat in expanded] == list(RWA_CLASS_IDS)
    empty = demo_app.browse_categories([])
    assert [cat.id for cat in empty] == list(RWA_CLASS_IDS)

    assert demo_app.next_place_index(["", "TSLA", "AAPL", "META"], 2) == 0
    assert demo_app.next_place_index(["NVDA", "", "AAPL", "META"], 0) == 1
    assert demo_app.next_place_index(["NVDA", "TSLA", "AAPL", "META"], 2, "XOM") == 2
    assert demo_app.next_place_index(["NVDA", "TSLA", "AAPL", "META"], 0, "NVDA") == 0
    assert demo_app.default_active_slot(["NVDA", "TSLA", "AAPL", "META"]) == 0
    assert demo_app.default_active_slot(["NVDA", "", "AAPL", "META"]) == 1


def test_search_typeahead_commits_without_enter_and_keeps_category_chips() -> None:
    """Matches must mount from typed 3+ chars — no category click, no Enter."""
    import app as demo_app

    catalog = demo_app._ticker_catalog(demo_app._cached_scorer(True))
    for query in ("NVD", "NVDA", "nvd", "nvda"):
        hits = demo_app.search_tickers(query, catalog)
        assert hits[0].symbol == "NVDA", query
        assert "Nvidia" in demo_app.format_option(hits[0])
        assert "bNVDA" in {opt.symbol for opt in hits}

    # Empty-state copy is reserved for a real miss, not a pending category click.
    assert demo_app.search_tickers("zzz", catalog) == []
    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    picker = source.split("def _render_search_picker", 1)[1]
    assert "No directory matches — type a ticker or tap a category." in picker
    assert "len((query or \"\").strip()) >= SEARCH_MIN_CHARS" in picker
    assert "if matches:" in picker
    assert "rat-cat-pill" not in picker.split("st.text_input", 1)[1].split("if matches", 1)[0]

    script = demo_app.search_typeahead_script()
    assert str(demo_app.SEARCH_TYPEAHEAD_DEBOUNCE_MS) in script
    assert "__DEBOUNCE_MS__" not in script
    assert "addEventListener(\"input\"" in script
    assert "addEventListener(\"keyup\"" in script
    assert "Enter" in script
    assert "applyChipQuery" not in script
    assert "height=1" in picker or 'height=1' in source
    assert "st.form" not in source

    # Category pills still pre-fill Search with the documented keyword.
    assert demo_app.chip_query(RWA_CLASS_CATEGORIES[0]) == "stock"
    assert demo_app.chip_query(CATEGORY_BY_ID["government_security"]) == (
        "government_security"
    )
    assert "st.session_state.ticker_query = chip_query(cat)" in source


def test_search_query_change_clears_stale_match_pick(monkeypatch) -> None:
    import app as demo_app

    class _FakeSS(dict):
        def __getattr__(self, name):
            try:
                return self[name]
            except KeyError as exc:
                raise AttributeError(name) from exc

        def __setattr__(self, name, value):
            self[name] = value

    monkeypatch.setattr(
        demo_app.st,
        "session_state",
        _FakeSS({demo_app.SEARCH_MATCH_KEY: "NVDA"}),
    )
    demo_app._on_search_query_change()
    assert demo_app.SEARCH_MATCH_KEY not in demo_app.st.session_state


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
    assert demo_app.mode_cue({"data_source": "cmc"}) == "LIVE"
    assert demo_app.mode_cue({}) == "UNCONFIRMED"
    assert "live CoinMarketCap" not in demo_app.data_source_caption(
        {"data_source": "fixture"}
    ).lower()
    assert "not a live" in demo_app.data_source_caption({"data_source": "fixture"}).lower()
    live_cap = demo_app.data_source_caption({"data_source": "live"})
    assert live_cap.startswith("Live CoinMarketCap directory/quotes")
    assert "self-reported" in live_cap
    assert "heuristic fallback" in live_cap
    assert "not labeled as live" in demo_app.data_source_caption({})
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


def test_backed_btokens_are_first_class_search_rows() -> None:
    catalog = _fixture_catalog()
    by_symbol = {opt.symbol: opt for opt in catalog}
    assert BACKED_BTOKEN_SYMBOLS <= set(by_symbol)
    assert {f.symbol for f in BACKED_POR_FEEDS} == BACKED_BTOKEN_SYMBOLS
    for symbol in BACKED_BTOKEN_SYMBOLS:
        opt = by_symbol[symbol]
        assert opt.source == BACKED_SEARCH_SOURCE
        assert "Chainlink PoR" in opt.name
        assert "Backed" in opt.aliases
        assert "finance" not in opt.categories

    bnv = [opt.symbol for opt in search_tickers("bNV", catalog)]
    assert bnv == ["bNVDA"]
    assert "bNVDA" in format_option(search_tickers("bNVDA", catalog)[0])
    assert "Chainlink PoR" in format_option(search_tickers("bnvda", catalog)[0])

    nvdax = [opt.symbol for opt in search_tickers("NVDAx", catalog)]
    assert nvdax == ["bNVDA"]
    nvda = [opt.symbol for opt in search_tickers("NVDA", catalog)]
    assert nvda[0] == "NVDA"
    assert "bNVDA" in nvda

    # Empty XSTOCKS_POR_FEEDS must not inject invented xStock rows.
    extra = catalog_from_por_feeds(())
    assert extra == []
    merged = merge_search_catalog(catalog, catalog_from_por_feeds())
    assert {opt.symbol for opt in merged if opt.symbol in BACKED_BTOKEN_SYMBOLS} == (
        BACKED_BTOKEN_SYMBOLS
    )


def test_backed_btoken_typeahead_places_slot() -> None:
    import app as demo_app

    catalog = demo_app._ticker_catalog(demo_app._cached_scorer(True))
    hits = demo_app.search_tickers("bNV", catalog)
    assert [opt.symbol for opt in hits] == ["bNVDA"]
    slots, active = demo_app.place_search_match(
        list(demo_app.DEFAULT_SLOTS), 0, hits[0].symbol
    )
    assert slots[0] == "bNVDA"
    assert active == 1
    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert "bNVDA" in source
    assert "published Backed bToken" in source
    assert "bNV → bNVDA" in source


def test_fixture_backed_symbol_scores_with_published_por_label(fixture_scorer) -> None:
    import app as demo_app

    catalog = demo_app._ticker_catalog(fixture_scorer)
    assert "bNVDA" in {opt.symbol for opt in demo_app.search_tickers("bNVDA", catalog)}
    report = demo_app._score_one(fixture_scorer, "bNVDA")
    assert report["ticker"] == "bNVDA"
    assert report["data_source"] == "fixture"
    reserves = report["verification"]["reserves"]
    assert reserves["source"] == "heuristic_fallback"
    assert reserves["level"] != "on-chain PoR"
    assert reserves["meta"]["published_por_feed"] == "bNVDA"
    assert reserves["meta"]["por_path"] == "fixture_labeled_skip"
    assert "live RPC skipped" in reserves["evidence"]
    badge, evidence = demo_app._verification_badge_label("reserves", report)
    assert "published Chainlink PoR" in badge
    assert "bNVDA" in badge
    assert "fixture/offline skip" in badge
    assert "live RPC skipped" in evidence
    # xStocks without a proxy stay on the unlabeled-heuristic path.
    tsla = fixture_scorer.score("TSLA")
    assert tsla["verification"]["reserves"]["source"] == "heuristic_fallback"
    assert not (tsla["verification"]["reserves"].get("meta") or {}).get("published_por_feed")


def test_btc_eth_are_crypto_not_rwa() -> None:
    assert NATIVE_CRYPTO_SYMBOLS == frozenset({"BTC", "ETH", "WBTC", "WETH"})
    assert is_native_crypto(symbol="BTC")
    assert is_native_crypto(symbol="eth")
    assert classify_categories(symbol="BTC", name="Bitcoin") == (CRYPTO_ID,)
    assert classify_categories(symbol="ETH", name="Ethereum") == (CRYPTO_ID,)
    assert "stock" not in classify_categories(symbol="BTC", name="Bitcoin", asset_type="stock")
    catalog = catalog_from_rwa_map(
        [
            {"symbol": "BTC", "name": "Bitcoin", "asset_type": "stock"},
            {"symbol": "NVDA", "name": "Nvidia Corp", "asset_type": "stock"},
        ]
    )
    by_symbol = {opt.symbol: opt for opt in catalog}
    assert by_symbol["BTC"].categories == (CRYPTO_ID,)
    assert "stock" in by_symbol["NVDA"].categories
    assert CRYPTO_ID not in by_symbol["NVDA"].categories
    # Crypto is not an RWA browse class — no pill / no "crypto" bucket.
    assert search_tickers("crypto", catalog) == []
    assert [opt.symbol for opt in search_tickers("BTC", catalog)] == ["BTC"]


def test_horizontal_category_pills_are_not_column_blocks() -> None:
    import app as demo_app

    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert ".rat-cat-row" in source
    assert "flex-direction: row" in source
    assert "flex-wrap: wrap" in source
    assert "rat-cat-pill" in source
    picker = source.split("def _render_search_picker", 1)[1]
    assert 'class="rat-cat-row"' in picker
    assert "cat_chip_" not in picker
    assert "rwa_class_" in picker
    assert "category_pill_href" not in picker
    assert 'href="?rwa_cat' not in picker


class _StockScopedLiveClient:
    """Live-shaped client whose unfiltered book is stocks (+ leaked BTC).

    Typed ``map`` / ``assets/list`` still return the official CMC classes.
    This is the #39 live QA failure mode: default listing hid non-stock
    tickers while industry / crypto extras still classified.
    """

    source = "live"

    _BOOK: dict[str, list[dict]] = {
        "stock": [
            {"symbol": "NVDA", "name": "Nvidia Corp", "rwa_id": 2, "asset_type": "stock"},
            {"symbol": "TSLA", "name": "Tesla Inc", "rwa_id": 15, "asset_type": "stock"},
        ],
        "commodity": [
            {"symbol": "GOLD", "name": "Gold", "rwa_id": 1, "asset_type": "commodity"},
        ],
        "government_security": [
            {"symbol": "USTB", "name": "US Treasury Bill", "rwa_id": 30, "asset_type": "government_security"},
            {"symbol": "OUSG", "name": "Ondo Short-Term US Treasuries", "rwa_id": 31, "asset_type": "government_security"},
        ],
        "etf": [
            {"symbol": "SPY", "name": "SPDR S&P 500 ETF", "rwa_id": 40, "asset_type": "etf"},
        ],
        "real_estate": [
            {"symbol": "HOME", "name": "Tokenized Home", "rwa_id": 50, "asset_type": "real_estate"},
        ],
        "currency": [
            {"symbol": "EUR", "name": "Euro", "rwa_id": 60, "asset_type": "currency"},
        ],
    }
    _LEAK = {"symbol": "BTC", "name": "Bitcoin", "rwa_id": 99, "asset_type": "stock"}

    def __init__(self, *, collide_bnvda: bool = False) -> None:
        self.collide_bnvda = collide_bnvda
        self.calls = {"rwa_map": 0, "assets_list": 0, "assets_list_all": 0}

    def _typed(self, kind: str) -> list[dict]:
        rows = [dict(row) for row in self._BOOK.get(kind, ())]
        if kind == "stock" and self.collide_bnvda:
            rows.append(
                {
                    "symbol": "BNVDA",
                    "name": "Colliding CMC row",
                    "rwa_id": 77,
                    "asset_type": "stock",
                }
            )
        return rows

    def rwa_map(self, symbol=None, *, asset_type: str | None = None, **_kwargs):
        self.calls["rwa_map"] += 1
        kind = (asset_type or "").strip().lower()
        if symbol:
            wanted = {part.strip().upper() for part in str(symbol).split(",") if part.strip()}
            found = []
            for rows in self._BOOK.values():
                for row in rows:
                    if (row.get("symbol") or "").upper() in wanted:
                        found.append(dict(row))
            return found
        if kind:
            return self._typed(kind)
        # Unfiltered live listing: stocks + leaked BTC only.
        return [*self._typed("stock"), dict(self._LEAK)]

    def assets_list(self, *, asset_type: str | None = None, **_kwargs):
        self.calls["assets_list"] += 1
        kind = (asset_type or "").strip().lower()
        rows = self._typed(kind) if kind else [*self._typed("stock"), dict(self._LEAK)]
        return {"rwa_assets": rows, "total_size": len(rows), "has_more": False}

    def assets_list_all(self, *, asset_type: str | None = None, **kwargs):
        self.calls["assets_list_all"] += 1
        return self.assets_list(asset_type=asset_type, **kwargs)


def test_rwa_bar_pills_are_exactly_six_cmc_classes() -> None:
    import app as demo_app

    catalog = _fixture_catalog()
    shown = demo_app.browse_categories(catalog)
    assert [cat.label for cat in shown] == [
        "Stocks",
        "Commodities",
        "Treasuries",
        "ETFs",
        "Real Estate",
        "Currencies",
    ]
    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert "exactly the six CMC" in source
    assert "Crypto / Digital Assets stay off this bar" in source


def test_live_directory_fills_non_stock_cmc_classes_and_backed() -> None:
    """Unfiltered live map/list is stocks-only; typed fills must still wire CMC."""
    import app as demo_app

    client = _StockScopedLiveClient()
    catalog = load_search_catalog(client)
    by_symbol = {opt.symbol: opt for opt in catalog}
    assert {"NVDA", "GOLD", "USTB", "OUSG", "SPY", "EUR", "HOME"} <= set(by_symbol)
    assert BACKED_BTOKEN_SYMBOLS <= set(by_symbol)
    assert by_symbol["GOLD"].asset_type == "commodity"
    assert by_symbol["USTB"].asset_type == "government_security"
    assert by_symbol["SPY"].asset_type == "etf"
    assert by_symbol["bNVDA"].source == BACKED_SEARCH_SOURCE

    assert "GOLD" in {opt.symbol for opt in search_tickers("commodity", catalog)}
    assert "USTB" in {opt.symbol for opt in search_tickers("treasury", catalog)}
    assert "OUSG" in {opt.symbol for opt in search_tickers("treasuries", catalog)}
    assert {opt.symbol for opt in search_tickers("GOLD", catalog)} >= {"GOLD"}
    assert {opt.symbol for opt in search_tickers("SPY", catalog)} >= {"SPY"}
    assert {opt.symbol for opt in search_tickers("USTB", catalog)} >= {"USTB"}
    assert {opt.symbol for opt in search_tickers("OUSG", catalog)} >= {"OUSG"}
    assert [opt.symbol for opt in search_tickers("bNVDA", catalog)][0] == "bNVDA"
    assert [opt.symbol for opt in search_tickers("bNV", catalog)] == ["bNVDA"]

    shown = demo_app.browse_categories(catalog)
    assert [cat.id for cat in shown] == list(RWA_CLASS_IDS)
    assert "crypto_digital" not in {cat.id for cat in shown}
    assert "ai_tech" not in {cat.id for cat in shown}


def test_live_backed_typeahead_survives_cmc_symbol_collision() -> None:
    client = _StockScopedLiveClient(collide_bnvda=True)
    catalog = load_search_catalog(client)
    bnv = [opt for opt in catalog if opt.symbol.upper() == "BNVDA"]
    assert len(bnv) == 1
    assert bnv[0].symbol == "bNVDA"
    assert bnv[0].source == BACKED_SEARCH_SOURCE
    hits = search_tickers("bNVDA", catalog, limit=4)
    assert hits[0].symbol == "bNVDA"
    assert hits[0].source == BACKED_SEARCH_SOURCE


def test_live_symbol_lookup_wires_cmc_ticker_not_in_assembled_book() -> None:
    """If the directory missed GOLD, map?symbol=GOLD still surfaces CMC's row."""

    class _LookupOnly:
        source = "live"

        def rwa_map(self, symbol=None, *, asset_type: str | None = None, **_kwargs):
            if symbol and str(symbol).upper() == "GOLD":
                return [
                    {
                        "symbol": "GOLD",
                        "name": "Gold",
                        "rwa_id": 1,
                        "asset_type": "commodity",
                    }
                ]
            if asset_type == "stock" or not asset_type:
                return [
                    {
                        "symbol": "NVDA",
                        "name": "Nvidia Corp",
                        "rwa_id": 2,
                        "asset_type": "stock",
                    }
                ]
            return []

    catalog = load_search_catalog(_LookupOnly())
    assert "GOLD" not in {opt.symbol for opt in catalog}
    assert search_tickers("GOLD", catalog) == []
    hits = search_tickers("GOLD", catalog, client=_LookupOnly())
    assert [opt.symbol for opt in hits] == ["GOLD"]
    assert hits[0].asset_type == "commodity"
    # Category miss must not invent a ticker.
    assert search_tickers("commodity", catalog, client=_LookupOnly()) == []


def test_classes_for_query_is_lazy_not_full_book() -> None:
    assert classes_for_query("") == ()
    assert classes_for_query("treasury") == ("government_security",)
    assert classes_for_query("treasuries") == ("government_security",)
    assert classes_for_query("government_security") == ("government_security",)
    assert classes_for_query("stock") == ("stock",)
    assert classes_for_query("commodity") == ("commodity",)
    assert classes_for_query("oil") == ("stock",)
    assert classes_for_query("bNV") == ()
    assert classes_for_query("bNVDA") == ()
    assert classes_for_query("NVD") == ("stock",)
    assert classes_for_query("ni") == ()


def test_load_class_catalog_is_one_source_not_dual_walk() -> None:
    client = _StockScopedLiveClient()
    rows = load_class_catalog(client, "government_security", first_page_only=True)
    assert {opt.symbol for opt in rows} == {"USTB", "OUSG"}
    assert all(opt.asset_type == "government_security" for opt in rows)
    assert client.calls["rwa_map"] == 1
    assert client.calls["assets_list"] == 0
    assert client.calls["assets_list_all"] == 0
    # Map already filled the class — no assets/list fallback.
    load_class_catalog(client, "government_security", first_page_only=True)
    assert client.calls["assets_list"] == 0
    assert client.calls["assets_list_all"] == 0


class _MapIgnoresTreasuryTypeClient(_StockScopedLiveClient):
    """#42 live miss: typed map returns the stock-scoped page; list has Treasuries."""

    def rwa_map(self, symbol=None, *, asset_type: str | None = None, **kwargs):
        kind = (asset_type or "").strip().lower()
        if kind == "government_security" and not symbol:
            self.calls["rwa_map"] += 1
            return self._typed("stock")
        return super().rwa_map(symbol, asset_type=asset_type, **kwargs)


class _EmptyTreasuryMapClient(_StockScopedLiveClient):
    """Typed map first page is empty; assets/list still lists CMC treasuries."""

    def rwa_map(self, symbol=None, *, asset_type: str | None = None, **kwargs):
        kind = (asset_type or "").strip().lower()
        if kind == "government_security" and not symbol:
            self.calls["rwa_map"] += 1
            return []
        return super().rwa_map(symbol, asset_type=asset_type, **kwargs)


class _UntypedTreasuryMapClient(_StockScopedLiveClient):
    """Typed map returns USTB/OUSG but omits asset_type (classification miss)."""

    def rwa_map(self, symbol=None, *, asset_type: str | None = None, **kwargs):
        kind = (asset_type or "").strip().lower()
        if kind == "government_security" and not symbol:
            self.calls["rwa_map"] += 1
            return [
                {"symbol": "USTB", "name": "US Treasury Bill", "rwa_id": 30},
                {
                    "symbol": "OUSG",
                    "name": "Ondo Short-Term US Treasuries",
                    "rwa_id": 31,
                },
            ]
        return super().rwa_map(symbol, asset_type=asset_type, **kwargs)


class _AliasTreasuryMapClient(_StockScopedLiveClient):
    """Live display alias instead of the official government_security enum."""

    def rwa_map(self, symbol=None, *, asset_type: str | None = None, **kwargs):
        kind = (asset_type or "").strip().lower()
        if kind == "government_security" and not symbol:
            self.calls["rwa_map"] += 1
            return [
                {
                    "symbol": "USTB",
                    "name": "US Treasury Bill",
                    "rwa_id": 30,
                    "asset_type": "treasury",
                },
                {
                    "symbol": "OUSG",
                    "name": "Ondo Short-Term US Treasuries",
                    "rwa_id": 31,
                    "asset_type": "Government Security",
                },
            ]
        return super().rwa_map(symbol, asset_type=asset_type, **kwargs)


def test_treasuries_falls_back_when_map_is_stock_scoped() -> None:
    """Typed map ignored asset_type — do not keep stocks; list must supply USTB."""
    client = _MapIgnoresTreasuryTypeClient()
    rows = load_class_catalog(client, "government_security", first_page_only=True)
    assert {opt.symbol for opt in rows} == {"USTB", "OUSG"}
    assert all(opt.asset_type == "government_security" for opt in rows)
    assert client.calls["rwa_map"] == 1
    assert client.calls["assets_list"] == 1
    assert client.calls["assets_list_all"] == 0
    query = "government_security"
    assert "USTB" in {opt.symbol for opt in search_tickers(query, rows)}
    assert "OUSG" in {opt.symbol for opt in search_tickers("treasuries", rows)}
    assert "NVDA" not in {opt.symbol for opt in rows}


def test_treasuries_falls_back_when_map_first_page_empty() -> None:
    client = _EmptyTreasuryMapClient()
    rows = load_class_catalog(client, "government_security", first_page_only=True)
    assert {opt.symbol for opt in rows} == {"USTB", "OUSG"}
    assert client.calls["rwa_map"] == 1
    assert client.calls["assets_list"] == 1
    assert client.calls["assets_list_all"] == 0


def test_treasuries_stamps_untyped_map_rows_without_list_walk() -> None:
    client = _UntypedTreasuryMapClient()
    rows = load_class_catalog(client, "government_security", first_page_only=True)
    assert {opt.symbol for opt in rows} == {"USTB", "OUSG"}
    assert all(opt.asset_type == "government_security" for opt in rows)
    assert "government_security" in rows[0].categories
    assert client.calls["rwa_map"] == 1
    assert client.calls["assets_list"] == 0
    assert client.calls["assets_list_all"] == 0
    assert {opt.symbol for opt in search_tickers("treasury", rows)} == {"USTB", "OUSG"}


def test_treasuries_maps_live_asset_type_aliases() -> None:
    client = _AliasTreasuryMapClient()
    rows = load_class_catalog(client, "government_security", first_page_only=True)
    assert {opt.symbol for opt in rows} == {"USTB", "OUSG"}
    assert all(opt.asset_type == "government_security" for opt in rows)
    assert client.calls["assets_list"] == 0
    assert classify_categories(symbol="USTB", asset_type="treasury") == (
        "government_security",
    )


def test_treasuries_does_not_invent_tickers_when_cmc_lists_none() -> None:
    class _EmptyBoth(_StockScopedLiveClient):
        def rwa_map(self, symbol=None, *, asset_type: str | None = None, **kwargs):
            kind = (asset_type or "").strip().lower()
            if symbol:
                self.calls["rwa_map"] += 1
                return []
            if kind == "government_security":
                self.calls["rwa_map"] += 1
                return []
            return super().rwa_map(symbol, asset_type=asset_type, **kwargs)

        def assets_list(self, *, asset_type: str | None = None, **kwargs):
            kind = (asset_type or "").strip().lower()
            if kind == "government_security":
                self.calls["assets_list"] += 1
                return {"rwa_assets": [], "total_size": 0, "has_more": False}
            return super().assets_list(asset_type=asset_type, **kwargs)

    client = _EmptyBoth()
    rows = load_class_catalog(client, "government_security", first_page_only=True)
    assert rows == []
    assert client.calls["assets_list_all"] == 0


class _EtfLabeledTreasuryClient(_StockScopedLiveClient):
    """#43 miss: government_security pages empty; CMC lists USTB/OUSG as etf."""

    def rwa_map(self, symbol=None, *, asset_type: str | None = None, **kwargs):
        self.calls["rwa_map"] += 1
        kind = (asset_type or "").strip().lower()
        if symbol:
            return []
        if kind == "etf":
            return [
                {
                    "symbol": "SPY",
                    "name": "SPDR S&P 500 ETF",
                    "rwa_id": 40,
                    "asset_type": "etf",
                },
                {
                    "symbol": "USTB",
                    "name": (
                        "Superstate Short Duration U.S. Government "
                        "Securities Fund (USTB)"
                    ),
                    "rwa_id": 30,
                    "asset_type": "etf",
                },
                {
                    "symbol": "OUSG",
                    "name": "OUSG",
                    "rwa_id": 31,
                    "asset_type": "etf",
                },
            ]
        if kind == "government_security":
            return []
        return self._typed("stock")

    def assets_list(self, *, asset_type: str | None = None, **kwargs):
        self.calls["assets_list"] += 1
        kind = (asset_type or "").strip().lower()
        if kind == "government_security":
            return {"rwa_assets": [], "total_size": 0, "has_more": False}
        return super().assets_list(asset_type=asset_type, **kwargs)


class _SymbolOnlyTreasuryClient(_StockScopedLiveClient):
    """Typed government_security filter empty; map?symbol= still lists them."""

    def rwa_map(self, symbol=None, *, asset_type: str | None = None, **kwargs):
        self.calls["rwa_map"] += 1
        if symbol:
            wanted = {
                part.strip().upper()
                for part in str(symbol).split(",")
                if part.strip()
            }
            rows = []
            if "USTB" in wanted:
                rows.append(
                    {
                        "symbol": "USTB",
                        "name": "USTB",
                        "rwa_id": 30,
                        "asset_type": "etf",
                    }
                )
            if "OUSG" in wanted:
                rows.append({"symbol": "OUSG", "name": "OUSG", "rwa_id": 31})
            return rows
        return []

    def assets_list(self, *, asset_type: str | None = None, **kwargs):
        self.calls["assets_list"] += 1
        return {"rwa_assets": [], "total_size": 0, "has_more": False}


class _MixedLiveTypeTreasuryClient(_StockScopedLiveClient):
    """Filter-ignored map mixes stocks with etf / untyped treasury rows."""

    def rwa_map(self, symbol=None, *, asset_type: str | None = None, **kwargs):
        kind = (asset_type or "").strip().lower()
        if kind == "government_security" and not symbol:
            self.calls["rwa_map"] += 1
            return [
                {
                    "symbol": "NVDA",
                    "name": "Nvidia Corp",
                    "rwa_id": 2,
                    "asset_type": "stock",
                },
                {"symbol": "USTB", "name": "US Treasury Bill", "rwa_id": 30},
                {
                    "symbol": "OUSG",
                    "name": "Ondo Short-Term US Treasuries",
                    "rwa_id": 31,
                    "asset_type": "etf",
                },
            ]
        return super().rwa_map(symbol, asset_type=asset_type, **kwargs)


def test_classify_treasuries_when_cmc_labels_etf_or_omits_type() -> None:
    cats = classify_categories(
        symbol="USTB",
        name="Superstate Short Duration U.S. Government Securities Fund (USTB)",
        asset_type="etf",
    )
    assert "etf" in cats
    assert TREASURY_CLASS in cats
    assert TREASURY_CLASS in classify_categories(
        symbol="OUSG", name="OUSG", asset_type=""
    )
    assert TREASURY_CLASS not in classify_categories(
        symbol="SPY", name="SPDR S&P 500 ETF", asset_type="etf"
    )
    assert TREASURY_CLASS not in classify_categories(
        symbol="TWE", name="Treasury Wine Estates", asset_type="stock"
    )
    assert is_treasury_like(symbol="USTB", name="USTB", asset_type="etf")
    assert TREASURY_PROBE_SYMBOLS == ("USTB", "OUSG")


def test_treasuries_keeps_etf_labeled_cmc_rows() -> None:
    client = _EtfLabeledTreasuryClient()
    rows = load_class_catalog(client, "government_security", first_page_only=True)
    assert {opt.symbol for opt in rows} == {"USTB", "OUSG"}
    assert "SPY" not in {opt.symbol for opt in rows}
    assert {opt.asset_type for opt in rows} == {"etf"}
    assert all(TREASURY_CLASS in opt.categories for opt in rows)
    assert client.calls["assets_list_all"] == 0
    assert {opt.symbol for opt in search_tickers("treasuries", rows)} == {
        "USTB",
        "OUSG",
    }


def test_treasuries_uses_symbol_probe_when_typed_filters_empty() -> None:
    client = _SymbolOnlyTreasuryClient()
    rows = load_class_catalog(client, "government_security", first_page_only=True)
    assert {opt.symbol for opt in rows} == {"USTB", "OUSG"}
    ustb = next(opt for opt in rows if opt.symbol == "USTB")
    ousg = next(opt for opt in rows if opt.symbol == "OUSG")
    assert ustb.asset_type == "etf"
    assert ousg.asset_type == ""
    assert client.calls["assets_list_all"] == 0
    query = "government_security"
    assert {opt.symbol for opt in search_tickers(query, rows)} == {"USTB", "OUSG"}


def test_treasuries_keeps_mixed_etf_and_untyped_rows() -> None:
    client = _MixedLiveTypeTreasuryClient()
    rows = load_class_catalog(client, "government_security", first_page_only=True)
    assert {opt.symbol for opt in rows} == {"USTB", "OUSG"}
    assert "NVDA" not in {opt.symbol for opt in rows}
    assert client.calls["rwa_map"] == 1
    assert client.calls["assets_list"] == 0
    assert client.calls["assets_list_all"] == 0


def test_app_treasuries_pill_lists_etf_labeled_cmc_rows() -> None:
    import app as demo_app
    from rwa_score.scorer import TransparencyScorer

    client = _EtfLabeledTreasuryClient()
    scorer = TransparencyScorer(client)
    query = demo_app.chip_query(CATEGORY_BY_ID["government_security"])
    catalog = demo_app._ticker_catalog(scorer, query=query)
    hits = demo_app.search_tickers(query, catalog)
    assert {opt.symbol for opt in hits} >= {"USTB", "OUSG"}
    assert "SPY" not in {opt.symbol for opt in hits}
    assert "NVDA" not in {opt.symbol for opt in hits}
    assert client.calls["assets_list_all"] == 0


def test_cached_class_catalog_hits_on_second_call() -> None:
    client = _StockScopedLiveClient()
    first = cached_class_catalog(client, "government_security")
    assert {opt.symbol for opt in first} == {"USTB", "OUSG"}
    assert client.calls["rwa_map"] == 1
    second = cached_class_catalog(client, "government_security")
    assert [opt.symbol for opt in second] == [opt.symbol for opt in first]
    assert client.calls["rwa_map"] == 1
    assert client.calls["assets_list_all"] == 0
    cached_class_catalog(client, "commodity")
    assert client.calls["rwa_map"] == 2
    cached_class_catalog(client, "commodity")
    assert client.calls["rwa_map"] == 2


def test_load_search_catalog_skips_assets_list_when_typed_map_fills() -> None:
    client = _StockScopedLiveClient()
    catalog = load_search_catalog(client)
    by_symbol = {opt.symbol: opt for opt in catalog}
    assert {"NVDA", "GOLD", "USTB", "OUSG", "SPY", "EUR", "HOME"} <= set(by_symbol)
    assert client.calls["assets_list"] == 0
    assert client.calls["assets_list_all"] == 0
    before = dict(client.calls)
    load_search_catalog(client)
    assert client.calls["rwa_map"] == before["rwa_map"]
    assert client.calls["assets_list"] == 0
    assert client.calls["assets_list_all"] == 0


def test_app_live_initial_catalog_is_por_only_no_directory_walk() -> None:
    import app as demo_app
    from rwa_score.scorer import TransparencyScorer

    client = _StockScopedLiveClient()
    scorer = TransparencyScorer(client)
    catalog = demo_app._ticker_catalog(scorer)
    assert {opt.symbol for opt in catalog} == BACKED_BTOKEN_SYMBOLS
    assert client.calls["rwa_map"] == 0
    assert client.calls["assets_list"] == 0
    assert client.calls["assets_list_all"] == 0
    # bNVDA typeahead does not need a CMC walk.
    assert [opt.symbol for opt in demo_app.search_tickers("bNV", catalog)] == ["bNVDA"]
    assert [opt.symbol for opt in demo_app.search_tickers("bNVDA", catalog)] == [
        "bNVDA"
    ]


def test_app_lazy_loads_treasuries_and_warm_path_does_not_rewalk() -> None:
    import app as demo_app
    from rwa_score.scorer import TransparencyScorer

    client = _StockScopedLiveClient()
    scorer = TransparencyScorer(client)
    catalog = demo_app._ticker_catalog(scorer, query="treasury")
    symbols = {opt.symbol for opt in catalog}
    assert {"USTB", "OUSG"} <= symbols
    assert BACKED_BTOKEN_SYMBOLS <= symbols
    assert "NVDA" not in symbols
    assert client.calls["rwa_map"] == 1
    assert client.calls["assets_list"] == 0
    assert client.calls["assets_list_all"] == 0
    assert "USTB" in {opt.symbol for opt in demo_app.search_tickers("treasury", catalog)}
    assert "OUSG" in {
        opt.symbol for opt in demo_app.search_tickers("treasuries", catalog)
    }

    before = dict(client.calls)
    again = demo_app._ticker_catalog(scorer, query="treasury")
    assert {opt.symbol for opt in again} >= {"USTB", "OUSG"}
    assert client.calls["rwa_map"] == before["rwa_map"]
    assert client.calls["assets_list"] == 0
    assert client.calls["assets_list_all"] == 0

    # Stocks tap is a different class — still not a full dual walk.
    stocks = demo_app._ticker_catalog(scorer, query="stock")
    assert "NVDA" in {opt.symbol for opt in stocks}
    assert "USTB" in {opt.symbol for opt in stocks}  # prior shard stays
    assert client.calls["assets_list_all"] == 0

    nvd = demo_app._ticker_catalog(scorer, query="NVD")
    assert "NVDA" in {opt.symbol for opt in nvd}
    assert [opt.symbol for opt in demo_app.search_tickers("NVD", nvd)][0] == "NVDA"


def test_app_treasuries_pill_loads_cmc_class_when_map_is_stock_scoped() -> None:
    """Treasuries pill → government_security even if typed map is stock-scoped."""
    import app as demo_app
    from rwa_score.scorer import TransparencyScorer

    client = _MapIgnoresTreasuryTypeClient()
    scorer = TransparencyScorer(client)
    query = demo_app.chip_query(CATEGORY_BY_ID["government_security"])
    assert query == "government_security"
    assert classes_for_query(query) == ("government_security",)
    catalog = demo_app._ticker_catalog(scorer, query=query)
    symbols = {opt.symbol for opt in catalog}
    assert {"USTB", "OUSG"} <= symbols
    assert "NVDA" not in symbols
    hits = demo_app.search_tickers(query, catalog)
    assert {opt.symbol for opt in hits} >= {"USTB", "OUSG"}
    assert client.calls["rwa_map"] == 1
    assert client.calls["assets_list"] == 1
    assert client.calls["assets_list_all"] == 0


def test_app_search_uses_cache_data_and_pending_query() -> None:
    import app as demo_app

    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert "@st.cache_data" in source
    assert "CATALOG_CACHE_TTL_SECONDS" in source
    assert "_cached_class_catalog_data" in source
    assert "pending_search_query" in source
    assert "_ticker_catalog(scorer, query=" in source
    assert "catalog_shards" in source
    assert "first_page_only=True" in source
    assert "load_search_catalog" not in source
    assert demo_app.CATALOG_CACHE_TTL_SECONDS == 1800.0
    assert demo_app.pending_search_query() == ""
    assert demo_app.classes_for_query is classes_for_query
    assert demo_app.LIVE_UNAVAILABLE_BANNER == "Live data unavailable"
    assert demo_app.CLASS_REMAINDER_CAPTION == (
        "Showing first 250 live results — type a ticker for the rest."
    )
    assert "[role=\"listbox\"]" in source
    assert "overflow-y: auto" in source
    picker = source.split("def _render_search_picker", 1)[1]
    assert "FixtureClient(" not in picker
    assert "create_client(" not in picker
    assert "st.error(LIVE_UNAVAILABLE_BANNER)" in picker


def _ranked_stock_client():
    """Live map with six ranked stocks plus an obscure symbol only on lookup."""

    class _Client:
        source = "live"

        def __init__(self) -> None:
            self.calls = {"rwa_map": 0, "assets_list": 0, "assets_list_all": 0}
            self.symbol_lookups: list[str] = []
            self.class_pages: list[str] = []
            self.stocks = [
                {
                    "symbol": f"S{index}",
                    "name": f"Share {index}",
                    "rwa_id": index,
                    "asset_type": "stock",
                    "rwa_rank": index,
                }
                for index in range(1, 7)
            ]

        def rwa_map(self, symbol=None, *, asset_type: str | None = None, **kwargs):
            self.calls["rwa_map"] += 1
            if symbol:
                self.symbol_lookups.append(str(symbol).upper())
                if str(symbol).upper() == "MSAI":
                    return [
                        {
                            "symbol": "MSAI",
                            "name": "Obscure Token",
                            "rwa_id": 99,
                            "asset_type": "stock",
                            "rwa_rank": 4000,
                        }
                    ]
                return []
            kind = (asset_type or "").strip().lower()
            self.class_pages.append(kind)
            limit = kwargs.get("limit")
            rows = list(self.stocks) if kind in {"", "stock"} else []
            if limit is not None:
                rows = rows[: int(limit)]
            self._directory_page = {
                "truncated": False,
                "total_size": len(self.stocks) if kind in {"", "stock"} else 0,
            }
            return rows

        def assets_list(self, **_kwargs):
            self.calls["assets_list"] += 1
            return {"rwa_assets": [], "total_size": 0, "has_more": False}

        def assets_list_all(self, **kwargs):
            self.calls["assets_list_all"] += 1
            return self.assets_list(**kwargs)

    return _Client()


def test_category_browse_scrolls_past_four_on_the_live_map() -> None:
    """Stocks tap and MSAI typeahead share rwa_map. Category Matches is not 4."""
    import app as demo_app
    from rwa_score.scorer import TransparencyScorer

    client = _ranked_stock_client()
    scorer = TransparencyScorer(client)
    query = demo_app.chip_query(CATEGORY_BY_ID["stock"])
    catalog = demo_app._ticker_catalog(scorer, query=query)
    hits = demo_app.search_matches(query, catalog, client)
    assert [opt.symbol for opt in hits] == ["S1", "S2", "S3", "S4", "S5", "S6"]
    assert len(hits) > demo_app.CANDIDATE_STRIP_LIMIT
    assert all(opt.source == "rwa_map" for opt in hits)
    assert client.class_pages == ["stock"]
    assert client.calls["assets_list_all"] == 0
    assert not demo_app.live_unavailable_banner(False, client, query)

    # Same client family: obscure ticker is map?symbol=, not a fixture stub.
    typed = demo_app._ticker_catalog(scorer, query="MSAI")
    found = demo_app.search_matches("MSAI", typed, client)
    assert [opt.symbol for opt in found] == ["MSAI"]
    assert "MSAI" in client.symbol_lookups
    assert client.source == "live"
    # Prefix strip stays short when many names share a prefix.
    prefix = demo_app.search_matches("SHA", typed, client)
    assert len(prefix) == demo_app.CANDIDATE_STRIP_LIMIT
    assert [opt.symbol for opt in prefix] == ["S1", "S2", "S3", "S4"]


def test_full_class_page_sets_honest_remainder_caption() -> None:
    import app as demo_app
    from rwa_score.ticker_search import CLASS_PAGE_LIMIT, class_shard_status

    class _FullPage:
        source = "live"

        def rwa_map(self, symbol=None, *, asset_type: str | None = None, **_kwargs):
            if symbol:
                return []
            rows = [
                {
                    "symbol": f"T{index}",
                    "name": f"Token {index}",
                    "rwa_id": index,
                    "asset_type": "stock",
                    "rwa_rank": index,
                }
                for index in range(1, CLASS_PAGE_LIMIT + 1)
            ]
            self._directory_page = {"truncated": True, "total_size": 7900}
            return rows

    client = _FullPage()
    rows = load_class_catalog(client, "stock", first_page_only=True)
    assert len(rows) == CLASS_PAGE_LIMIT
    assert class_shard_status(client, "stock").truncated is True
    assert class_shard_status(client, "stock").unavailable is False
    assert demo_app.class_browse_truncated(client, "stock") is True
    assert "250" in demo_app.CLASS_REMAINDER_CAPTION
    assert "type a ticker for the rest" in demo_app.CLASS_REMAINDER_CAPTION


def test_class_browse_dropdown_is_one_continuous_scroll() -> None:
    """The class shard is one overlay list — no Prev/Next, no radio, no nested height."""
    import app as demo_app

    rows = [
        TickerOption(symbol=f"T{index}", name=f"Token {index}", asset_type="stock")
        for index in range(250)
    ]
    items = demo_app.ticker_dropdown_items(rows)
    assert len(items) == 250
    assert items[0]["symbol"] == "T0"
    assert items[-1]["symbol"] == "T249"
    assert "Token 249" in items[-1]["label"]
    assert demo_app.class_dropdown_label(rows, "") == "Choose a ticker"
    assert demo_app.class_dropdown_label(rows, "T3") == items[3]["label"]

    script = demo_app.ticker_dropdown_script(items, label=items[3]["label"], truncated=True)
    assert '"T0"' in script and '"T249"' in script
    assert script.count('"symbol"') == 250
    assert "T3" in script
    assert "rwa-ticker-dd-scroll" in script
    assert 'role": "listbox"' in script or '"listbox"' in script
    assert "overflow-y: auto" in Path(demo_app.__file__).read_text(encoding="utf-8")
    assert "Previous" not in script
    assert "rwa_match_next" not in script
    assert "MutationObserver" not in script
    assert "st.radio" not in script
    assert "st.container" not in script
    assert "Showing first 250" in script
    assert demo_app.CLASS_PICK_LABEL in script
    assert "commitPick" in script
    assert "win.KeyboardEvent" in demo_app.TICKER_DROPDOWN_JS
    assert "win.InputEvent" in demo_app.TICKER_DROPDOWN_JS
    assert "position: fixed" in Path(demo_app.__file__).read_text(encoding="utf-8")

    browse = Path(demo_app.__file__).read_text(encoding="utf-8").split(
        "def _render_class_browse", 1
    )[1].split("def _render_search_picker", 1)[0]
    assert "st.radio(" not in browse
    assert "st.container(height=" not in browse
    assert "place_search_match(" in Path(demo_app.__file__).read_text(encoding="utf-8").split(
        "def _on_class_ticker_pick", 1
    )[1].split("def _install_search_typeahead", 1)[0]
    assert 'key="rwa_class_browse"' in browse
    assert "rwa-ticker-dd-anchor" in demo_app.TICKER_DROPDOWN_JS
    assert "CLASS_REMAINDER_CAPTION" in browse


def _isolate_live_catalog_memos() -> None:
    """Drop process, class, and session shards before a live-failure case."""
    import app as demo_app
    from rwa_score.ticker_search import clear_catalog_cache

    clear_catalog_cache()
    demo_app._catalog_shard_memo.clear()
    try:
        import streamlit as st

        store = st.session_state.get("catalog_shards")
        if isinstance(store, dict):
            store.clear()
    except Exception:
        pass


def test_stale_catalog_reloads_before_live_banner() -> None:
    """A pill click must banner even when the outer catalog used the old query.

    ``pending_search_query()`` runs before the pill ``if`` body. That frame
    used to search an empty live catalog and show "No directory matches"
    instead of "Live data unavailable".
    """
    import app as demo_app
    from rwa_score.client import CMCError
    from rwa_score.scorer import TransparencyScorer
    from rwa_score.ticker_search import class_shard_status

    _isolate_live_catalog_memos()

    class _Down:
        source = "live"

        def __init__(self) -> None:
            self.calls = {"rwa_map": 0, "assets_list": 0}

        def rwa_map(self, *_args, **_kwargs):
            self.calls["rwa_map"] += 1
            raise CMCError("/v5/real-world-assets/map -> HTTP 401: unauthorized")

        def assets_list(self, **_kwargs):
            self.calls["assets_list"] += 1
            raise CMCError("/v5/real-world-assets/map -> HTTP 401: unauthorized")

    client = _Down()
    scorer = TransparencyScorer(client)
    stale = demo_app._ticker_catalog(scorer, query="")
    assert client.calls["rwa_map"] == 0
    assert demo_app.live_unavailable_banner(False, client, "stock") is False
    catalog = demo_app.catalog_for_active_query(scorer, stale, "stock")
    assert client.calls["rwa_map"] == 1
    assert class_shard_status(client, "stock").unavailable is True
    assert demo_app.search_matches("stock", catalog, client) == []
    assert demo_app.live_unavailable_banner(False, client, "stock") is True
    assert demo_app.live_unavailable_banner(True, client, "stock") is False
    symbols = {opt.symbol for opt in catalog}
    assert "TSLA" not in symbols
    assert "META" not in symbols


def test_live_directory_failure_is_empty_not_fixture_swap() -> None:
    """401 / 429 / 1008 leave Matches empty and do not build a FixtureClient."""
    import time

    import app as demo_app
    from rwa_score.client import CMCError
    from rwa_score.scorer import TransparencyScorer
    from rwa_score.ticker_search import (
        DIRECTORY_FAILURE_RETRY_SECONDS,
        ClassShardStatus,
        _CLASS_CATALOG_MEMO,
        _CLASS_STATUS,
        _status_key,
        class_shard_status,
        client_shard_token,
    )

    _isolate_live_catalog_memos()
    errors = (
        CMCError("/v5/real-world-assets/map -> HTTP 401: unauthorized"),
        CMCError("map hit CoinMarketCap rate limit (HTTP 429, error_code 1008)"),
        CMCError("map -> CMC error 1008: rate limit"),
    )
    for exc in errors:
        _isolate_live_catalog_memos()

        class _Down:
            source = "live"

            def __init__(self) -> None:
                self.calls = {"rwa_map": 0, "assets_list": 0}
                self.exc = exc

            def rwa_map(self, *_args, **_kwargs):
                self.calls["rwa_map"] += 1
                raise self.exc

            def assets_list(self, **_kwargs):
                self.calls["assets_list"] += 1
                raise self.exc

        client = _Down()
        scorer = TransparencyScorer(client)
        catalog = demo_app._ticker_catalog(scorer, query="stock")
        assert class_shard_status(client, "stock").unavailable is True
        assert demo_app.search_matches("stock", catalog, client) == []
        assert demo_app.live_unavailable_banner(False, client, "stock") is True
        assert demo_app.live_unavailable_banner(True, client, "stock") is False
        assert client.calls["assets_list"] == 0
        assert client.calls["rwa_map"] == 1
        assert client.source == "live"
        symbols = {opt.symbol for opt in catalog}
        assert "TSLA" not in symbols
        assert "META" not in symbols
        token = client_shard_token(client)
        assert not any(key[0] == token for key in _CLASS_CATALOG_MEMO)
        store = demo_app._shard_store()
        assert not any(
            str(key).startswith(f"live:{token}:") and not rows
            for key, rows in store.items()
        )
        # Second browse inside the retry window does not hammer the endpoint.
        again = demo_app._ticker_catalog(scorer, query="stock")
        assert demo_app.search_matches("stock", again, client) == []
        assert client.calls["rwa_map"] == 1

    # After the retry window a recovered directory must be fetched again.
    # A pinned failed ``[]`` used to keep rwa_map at 1.
    _isolate_live_catalog_memos()

    class _Recover:
        source = "live"

        def __init__(self) -> None:
            self.calls = {"rwa_map": 0, "assets_list": 0}
            self.down = True

        def rwa_map(self, *_args, **_kwargs):
            self.calls["rwa_map"] += 1
            if self.down:
                raise CMCError("/v5/real-world-assets/map -> HTTP 401: unauthorized")
            return [
                {
                    "symbol": "S1",
                    "name": "Recovered Stock",
                    "rwa_id": 1,
                    "asset_type": "stock",
                    "rwa_rank": 1,
                }
            ]

        def assets_list(self, **_kwargs):
            self.calls["assets_list"] += 1
            raise CMCError("/v5/real-world-assets/map -> HTTP 401: unauthorized")

    client = _Recover()
    scorer = TransparencyScorer(client)
    failed = demo_app._ticker_catalog(scorer, query="stock")
    assert class_shard_status(client, "stock").unavailable is True
    assert demo_app.search_matches("stock", failed, client) == []
    assert client.calls["rwa_map"] == 1
    demo_app._ticker_catalog(scorer, query="stock")
    assert client.calls["rwa_map"] == 1
    # A failed empty pinned in either store must not survive the retry window.
    token = client_shard_token(client)
    _CLASS_CATALOG_MEMO[(token, "stock", True)] = []
    demo_app._shard_store()[f"live:{token}:stock"] = []
    demo_app._catalog_shard_memo[f"live:{token}:stock"] = []
    status_key = _status_key(client, "stock", True)
    _CLASS_STATUS[status_key] = ClassShardStatus(
        unavailable=True,
        truncated=False,
        failed_at=time.monotonic() - DIRECTORY_FAILURE_RETRY_SECONDS - 5,
    )
    client.down = False
    recovered = demo_app._ticker_catalog(scorer, query="stock")
    assert client.calls["rwa_map"] == 2
    assert class_shard_status(client, "stock").unavailable is False
    assert [opt.symbol for opt in demo_app.search_matches("stock", recovered, client)] == [
        "S1"
    ]

