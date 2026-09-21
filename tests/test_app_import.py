"""Smoke-import the Streamlit entrypoint without launching the server."""

from pathlib import Path

import pytest

from tests.conftest import RecordingClient

README_ONELINER = (
    "RAT Score (RWA Transparency Score) — an AI-assisted risk radar for tokenized stocks. "
    "Rates issuers 0–100 on backing, proof of reserves, redemption, price integrity, "
    "disclosure, and cross-issuer basis using CoinMarketCap’s RWA API."
)

EXPECTED_DISCLAIMER = (
    "Informational and educational hackathon demo only. Not financial, investment, "
    "legal, or tax advice. Not an offer, solicitation, or recommendation to buy, "
    "sell, or hold any security, digital asset, tokenized stock, or other instrument. "
    "Scores are automated heuristics (including issuer-name matching) plus third-party "
    "CoinMarketCap data or bundled demo fixtures — not audited attestations, not legal "
    "or audit opinions, and not a substitute for issuer filings, prospectuses, offering "
    "documents, or your own independent research. Data may be incomplete, delayed, "
    "inaccurate, or outdated. Nothing here guarantees accuracy, completeness, or fitness "
    "for any purpose. Past or present scores are not indicative of future results. This "
    "demo is not provided by a broker-dealer, exchange, ATS, funding portal, or registered "
    "investment adviser, and it does not create any advisory or fiduciary relationship. "
    "Do your own research. Use at your own risk."
)


def test_brand_copy_replaces_old_page_title() -> None:
    import app as demo_app

    assert demo_app.PAGE_TITLE == "RAT Score | RWA Transparency Score"
    assert demo_app.BRAND_H1 == "RAT Score"
    assert demo_app.BRAND_SUB == "RWA Transparency Score"
    assert demo_app.TAGLINE == (
        "Risk radar for tokenized stocks · CoinMarketCap Build-a-thon"
    )
    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert 'page_title="RWA Transparency Score"' not in source
    assert "st.title(" not in source
    assert "st.header(BRAND_H1)" in source
    assert 'Mode: {mode_label}' in source or "Mode:" in source
    assert "RTS" not in demo_app.PAGE_TITLE
    assert "RTS" not in demo_app.BRAND_H1


def test_readme_brand_oneliner() -> None:
    readme = Path(__file__).resolve().parents[1] / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert text.startswith("# RAT Score\n")
    assert README_ONELINER in text
    assert not any(line.strip() == "# RWA Transparency Score" for line in text.splitlines())


def test_brand_assets_exist() -> None:
    import app as demo_app

    assert demo_app.FAVICON_PATH.is_file()
    assert demo_app.MONOGRAM_PATH.is_file()
    assert demo_app.MONOGRAM_PATH.name == "rat-monogram.png"
    assert (demo_app.ASSETS_DIR / "rat-icon-192.png").is_file()
    assert (demo_app.ASSETS_DIR / "rat-monogram-on-dark.png").is_file()
    assert (demo_app.ASSETS_DIR / "rat-monogram.svg").is_file()
    assert (demo_app.ASSETS_DIR / "fonts" / "Inter-Regular.ttf").is_file()
    assert (demo_app.ASSETS_DIR / "fonts" / "Inter-Bold.ttf").is_file()
    uri = demo_app._asset_data_uri(demo_app.MONOGRAM_PATH)
    assert uri is not None and uri.startswith("data:image/png;base64,")


def test_app_module_exports_helpers() -> None:
    import app as demo_app

    assert "NVDA" in demo_app.FIXTURE_TICKERS
    assert demo_app.DEFAULT_SLOTS == ["NVDA", "TSLA", "AAPL", "META"]
    assert demo_app.MAX_COMPARE_SLOTS == 4
    assert demo_app.DEFAULT_SLOTS == demo_app.FIXTURE_TICKERS
    assert demo_app.DISCLAIMER == EXPECTED_DISCLAIMER
    assert set(demo_app.BAND_COLORS) == {"GREEN", "YELLOW", "ORANGE", "RED"}


def test_readme_disclaimer_and_problem_blurb() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
    assert EXPECTED_DISCLAIMER in text
    assert "how its public CMC/issuer signals look under our published heuristics" in text
    assert "how honest its issuer looks" not in text


def test_readme_chainlink_por_coverage_for_judges() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
    assert "Do not expect every ticker to hit the oracle" in text
    assert "bTokens on Polygon only" in text
    assert "no published Chainlink PoR / SmartData aggregator yet" in text
    assert "That is expected, not a bug" in text
    assert "no aggregator `proxyAddress`" in text
    assert "docs/XSTOCKS_CHAINLINK_POR.md" in text
    assert "do not invent addresses" in text.lower()
    assert "Reserves-only score 90" in text
    assert "must not be scored as undercollateralized PoR" in text


def test_app_reuses_scorer_via_cache_resource() -> None:
    import app as demo_app

    assert hasattr(demo_app._cached_scorer, "clear")
    first = demo_app._cached_scorer(True)
    second = demo_app._cached_scorer(True)
    assert first is second
    assert first.client.source == "fixture"


def test_assign_ticker_replaces_one_slot() -> None:
    import app as demo_app

    original = list(demo_app.DEFAULT_SLOTS)
    updated = demo_app.assign_ticker_to_slot(original, 1, " msft ")
    assert updated == ["NVDA", "MSFT", "AAPL", "META"]
    assert original == demo_app.DEFAULT_SLOTS


def test_assign_ticker_rejects_empty_and_bad_index() -> None:
    import app as demo_app

    with pytest.raises(ValueError, match="empty"):
        demo_app.assign_ticker_to_slot(list(demo_app.DEFAULT_SLOTS), 0, "  ")
    with pytest.raises(IndexError):
        demo_app.assign_ticker_to_slot(list(demo_app.DEFAULT_SLOTS), 4, "NVDA")


class _FakeSS(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class _FakeQP(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def test_category_chips_do_not_remount_or_reset_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Category chips must not full-navigate. Slots live in session_state only."""
    import app as demo_app

    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    picker = source.split("def _render_search_picker", 1)[1]
    chip_block = picker.split("st.text_input", 1)[0]
    assert "rwa_class_" in chip_block
    assert "st.button(" in chip_block
    assert "st.columns(" in chip_block
    assert "category_pill_href" not in source
    assert "rwa_slots" not in source
    assert "rwa_active" not in source
    assert 'href="?rwa_cat' not in source
    assert "target=\"_self\"" not in chip_block
    assert "applyChipQuery" not in demo_app.SEARCH_TYPEAHEAD_JS

    # True first load: empty session → published defaults.
    monkeypatch.setattr(demo_app.st, "session_state", _FakeSS())
    demo_app._ensure_slot_state()
    assert demo_app.st.session_state.slots == list(demo_app.DEFAULT_SLOTS)
    assert demo_app.st.session_state.active_slot == 0

    # ?rwa_cat= deep-link must not reset a filled row (no URL slot payload).
    filled = _FakeSS(slots=["GOLD", "TSLA", "AAPL", "META"], active_slot=1)
    monkeypatch.setattr(demo_app.st, "session_state", filled)
    monkeypatch.setattr(demo_app.st, "query_params", _FakeQP({"rwa_cat": "commodity"}))
    demo_app._ensure_slot_state()
    assert demo_app.st.session_state.slots == ["GOLD", "TSLA", "AAPL", "META"]
    assert demo_app.st.session_state.active_slot == 1

    # Same-session second chip: next pick fills the existing row.
    demo_app._auto_place("SLV")
    assert demo_app.st.session_state.slots == ["GOLD", "SLV", "AAPL", "META"]
    assert demo_app.st.session_state.active_slot == 2
    assert demo_app.st.session_state["_clear_search"] is True


def test_score_slots_isolates_unknown_fixture_ticker(fixture_scorer) -> None:
    import app as demo_app

    slots = demo_app.assign_ticker_to_slot(list(demo_app.DEFAULT_SLOTS), 2, "NOTATICKER")
    results = demo_app._score_slots(fixture_scorer, slots)
    assert [row[0] for row in results] == ["NVDA", "TSLA", "NOTATICKER", "META"]
    assert results[0][1] is not None and results[0][2] is None
    assert results[2][1] is None
    assert "not found" in (results[2][2] or "").lower()


def test_score_slots_accepts_any_mapped_ticker() -> None:
    import app as demo_app
    from rwa_score.scorer import TransparencyScorer

    client = RecordingClient()
    # Pretend live map includes an extra CMC-mapped symbol beyond the fixture four.
    client.assets.append({"symbol": "MSFT", "rwa_id": 99})
    client.info[99] = {
        "rwa_id": 99,
        "symbol": "MSFT",
        "cik": "0000789019",
        "issuer": {"name": "Backed Finance"},
    }
    client.issuer_details["abc"]["tokens"].append({"rwa_id": 99, "crypto_id": 99})
    scorer = TransparencyScorer(client)
    slots = demo_app.assign_ticker_to_slot(list(demo_app.DEFAULT_SLOTS), 0, "MSFT")
    results = demo_app._score_slots(scorer, slots)
    assert results[0][0] == "MSFT"
    assert results[0][1] is not None
    assert results[0][1]["ticker"] == "MSFT"


def test_why_this_score_copy_and_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    import app as demo_app

    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert 'open_expander(\n        "Why this score?"' in source or 'st.expander("Why this score?"' in source
    assert "Show explanation" in source
    why = source.split("def _render_why_this_score", 1)[1].split(
        "def _user_facing_share_status", 1
    )[0]
    assert "_cached_explanation" in why
    assert "AI_FOOTNOTE" in why
    assert "open_expander" in why or "st.expander" in why
    assert "why_exp_" in why
    assert "Share score card" not in why
    card = source.split("def _render_compare_card", 1)[1].split(
        "def _render_slot_error", 1
    )[0]
    assert "_render_why_this_score" in card
    assert "_render_card_details" in card
    assert "st.progress" not in card
    assert "Share score card" not in card
    assert "mode_cue" in card
    assert "weakest_pillar_line" in card
    assert "pillar_dots" in card
    assert "band_chip_html" in card
    assert demo_app.AI_FOOTNOTE == "generated by AI, not financial advice"
    assert demo_app.EXPLAIN_CACHE_TTL_SECONDS == 24 * 3600.0

    demo_app._explain_cache.clear()
    calls = {"n": 0}

    def fake_explain(result):
        calls["n"] += 1
        return f"explained {result['ticker']}"

    monkeypatch.setattr(demo_app, "explain_score", fake_explain)
    report = {"ticker": "AAPL", "score": 47.0, "band": "ORANGE"}
    first = demo_app._cached_explanation(report)
    second = demo_app._cached_explanation(report)
    assert first == second == "explained AAPL"
    assert calls["n"] == 1

    demo_app._explain_cache.clear()

    def boom(_result):
        raise RuntimeError("xAI down")

    monkeypatch.setattr(demo_app, "explain_score", boom)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    text = demo_app._cached_explanation({"ticker": "TSLA", "score": 10, "band": "RED"})
    assert "Explanation unavailable" in text
    assert "not financial advice" in text.lower()


def test_cached_explanation_uses_templated_fallback_without_xai_key(
    fixture_scorer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app as demo_app

    monkeypatch.delenv("XAI_API_KEY", raising=False)
    demo_app._explain_cache.clear()
    report = fixture_scorer.score("NVDA")
    text = demo_app._cached_explanation(report)
    assert "NVDA" in text
    assert "not financial advice" in text.lower()
    assert "XAI_API_KEY" not in text
    assert "Traceback" not in text


def test_share_score_card_is_button_gated() -> None:
    import app as demo_app
    from types import SimpleNamespace

    from rwa_score.x_client import MISSING_CREDS_MESSAGE, X_POST_UNAVAILABLE_MESSAGE

    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert '"Scorecard"' in source
    assert "share_score_card" in source
    assert "_render_share_controls" in source
    assert "_user_facing_share_status" in source
    assert "if st.button(" in source
    assert demo_app.SCORECARD_BUTTON_LABEL == "Scorecard"
    assert demo_app.SHARE_BUTTON_LABEL == "Share"
    assert demo_app.DOWNLOAD_PNG_LABEL == "download PNG"
    assert demo_app.SHARE_EXPANDER_LABEL == "Scorecard"
    button_idx = source.index("st.button(SCORECARD_BUTTON_LABEL")
    call_idx = source.index("share_score_card(report")
    assert button_idx < call_idx
    # Must not fire a share on import / page load.
    assert "share_score_card(" not in source.split("def _render_share_controls")[0]
    # PNG first — X post is attached later so a hang cannot wipe the preview.
    assert "post_to_x=False" in source.split("def _render_share_controls", 1)[1]
    assert "attach_x_share" in source
    assert "_show_share_png" in source
    assert "share_card_preview_html" in source
    assert "Could not build the score card image" in source
    assert "Click Scorecard to preview the signed PNG." in source
    # Markdown data URIs only. st.image / download_button register /media and
    # /_stcore/download; components.html mounts /component — MPA v1 Page not found.
    share_fn = source.split("def _render_share_controls", 1)[1].split(
        "def _render_why_this_score", 1
    )[0]
    show_fn = source.split("def _show_share_png", 1)[1].split("def _auto_place", 1)[0]
    assert "st.download_button(" not in share_fn
    assert "st.image(" not in share_fn
    assert "components.html(" not in share_fn
    assert "_maybe_rerun()" not in share_fn
    assert "components.html(" not in show_fn
    assert "st.image(" not in show_fn
    assert "st.download_button(" not in show_fn
    assert "_maybe_rerun()" not in show_fn
    assert "st.markdown(" in show_fn
    assert "unsafe_allow_html=True" in show_fn
    scorecard_idx = share_fn.index("st.button(SCORECARD_BUTTON_LABEL")
    preview_idx = share_fn.index("_show_share_png(")
    post_idx = share_fn.index("st.button(SHARE_BUTTON_LABEL")
    attach_idx = share_fn.index("attach_x_share(")
    assert scorecard_idx < preview_idx < post_idx < attach_idx
    scorecard_branch = share_fn.split("st.button(SCORECARD_BUTTON_LABEL", 1)[1].split(
        "st.button(SHARE_BUTTON_LABEL", 1
    )[0]
    assert "attach_x_share(" not in scorecard_branch
    assert "post_image(" not in scorecard_branch
    assert "XClient" not in scorecard_branch
    assert "Posted to X" not in share_fn.split("st.button(SHARE_BUTTON_LABEL", 1)[0]
    assert share_fn.index("_show_share_png(") < share_fn.index("attach_x_share(")
    assert "justify-content: flex-start" in source
    assert "stExpander" in source
    # _maybe_rerun is Search _auto_place only (file-adjacent to Share; not called).
    # The only st.rerun() statement lives inside _maybe_rerun itself.
    assert source.count("_maybe_rerun()") == 2
    assert source.count("    st.rerun()") == 1
    # Only Search typeahead may mount a component iframe.
    typeahead_fn = source.split("def _install_search_typeahead", 1)[1].split(
        "def _render_search_picker", 1
    )[0]
    assert "components.html(" in typeahead_fn
    assert source.count("components.html(") == 1
    assert "data:image/png;base64" in source
    assert "st.image(bundle.png_bytes, use_container_width=" not in source

    raw = SimpleNamespace(
        x_posted=False,
        x_message="X post skipped: X media INIT failed (400): boom",
    )
    assert demo_app._user_facing_share_status(raw) == X_POST_UNAVAILABLE_MESSAGE
    missing = SimpleNamespace(
        x_posted=False,
        x_message=MISSING_CREDS_MESSAGE,
    )
    assert demo_app._user_facing_share_status(missing) == MISSING_CREDS_MESSAGE
    assert MISSING_CREDS_MESSAGE != X_POST_UNAVAILABLE_MESSAGE
    posted = SimpleNamespace(
        x_posted=True,
        x_message="Posted to X: https://x.com/i/web/status/1",
        x_url="https://x.com/i/web/status/1",
    )
    assert demo_app._user_facing_share_status(posted) == "Posted to X."
    assert "https://" not in demo_app._user_facing_share_status(posted)
    footer = demo_app.share_footer_markdown(posted)
    assert "Open score page" in footer
    assert "View post on X" in footer
    assert "](https://x.com/i/web/status/1)" in footer
    unseen = SimpleNamespace(x_posted=False, x_url=None, x_message="")
    preview_footer = demo_app.share_footer_markdown(unseen)
    assert "Open score page" in preview_footer
    assert "View post on X" not in preview_footer
    assert demo_app.is_x_status_url("https://x.com/i/web/status/99")
    assert not demo_app.is_x_status_url("https://example.com/status/99")
    assert not demo_app.is_x_status_url("")

    state_key, pending_key = demo_app.share_session_keys(2, "bNVDA")
    assert state_key == "share_card_2_bNVDA"
    assert pending_key == "share_x_pending_2_bNVDA"
    empty = SimpleNamespace(
        x_posted=False,
        x_message=f"{demo_app.PNG_BUILD_FAILED_PREFIX} pillow missing",
        png_bytes=b"",
        fingerprint="UNSIGNED",
    )
    assert not empty.png_bytes
    assert empty.x_message.startswith(demo_app.PNG_BUILD_FAILED_PREFIX)

    html = demo_app.share_card_preview_html(b"\x89PNG fake", 'rat-score-bNVDA.png')
    assert "data:image/png;base64," in html
    assert "download PNG" in html
    assert 'download="rat-score-bNVDA.png"' in html
    assert "<img " in html
    escaped = demo_app.share_card_preview_html(b"x", 'say "hi".png')
    assert "&quot;" in escaped
    assert 'download="say "hi".png"' not in escaped
    # Share stays markdown data-URI — no MPA pages/, no st.image / download.
    assert not Path("pages").exists()
    assert "st.markdown(" in show_fn
    assert "data:image/png;base64" in demo_app.share_card_preview_html(b"png", "x.png")


def test_compare_row_is_native_streamlit_not_html() -> None:
    import app as demo_app

    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert "st.metric" in source
    assert "_score_card_html" not in source
    assert "_error_card_html" not in source
    assert "_empty_slot_html" not in source
    assert "score-hero" not in source
    # Band chip + category pills + Share markdown data-URI + page CSS.
    assert source.count("unsafe_allow_html") <= 4
    assert "Pick a ticker to compare here" in source
    assert "Assign a ticker" not in source
    assert "Why this score?" in source
    assert "Pillar evidence" in source
    assert "open_expander" in source
    assert "pillar_ev_" in source
    assert "why_exp_" in source
    assert "share_exp_" in source
    render = source.split("def _render_compare_card", 1)[1].split(
        "def _render_slot_error", 1
    )[0]
    assert "band_chip_html" in render
    assert "st.metric" in render
    assert "delta_color=\"off\"" in render
    assert "st.progress" not in render
    assert "_render_why_this_score" in render
    assert "_render_card_details" in render
    assert "st.caption" in render
    assert "share_score_card" not in render
    assert "Share score card" not in render
    assert "mode_cue" in render
    assert "weakest_pillar_line" in render
    assert "_render_selected_slot_detail" in source
    assert "max-width" in source
    assert "17rem" in source


def test_score_one_session_cache_skips_rescore(fixture_scorer, monkeypatch) -> None:
    import app as demo_app

    calls = {"n": 0}
    real = fixture_scorer.score

    def wrapped(symbol):
        calls["n"] += 1
        return real(symbol)

    monkeypatch.setattr(fixture_scorer, "score", wrapped)
    demo_app._score_memo.clear()
    try:
        demo_app.st.session_state["score_reports"] = {}
    except Exception:
        pass
    first = demo_app._score_one(fixture_scorer, "NVDA")
    second = demo_app._score_one(fixture_scorer, "NVDA")
    assert first["ticker"] == second["ticker"] == "NVDA"
    assert first["score"] == second["score"]
    assert calls["n"] == 1


def test_score_slots_empty_symbol_does_not_score(fixture_scorer) -> None:
    import app as demo_app

    results = demo_app._score_slots(fixture_scorer, ["NVDA", "", "AAPL", "META"])
    assert results[0][1] is not None
    assert results[1] == ("", None, "Empty slot.")
    assert results[2][0] == "AAPL"


def test_sidebar_density_keeps_required_copy() -> None:
    import app as demo_app

    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    sidebar = source.split("def _render_sidebar_controls", 1)[1].split(
        "def _render_card_details", 1
    )[0]
    assert "Use demo fixtures" in sidebar
    assert "st.toggle(" in sidebar
    assert 'st.expander("How scores are labeled"' in sidebar
    assert 'st.expander("Pillar weights"' in sidebar
    assert 'st.expander("CMC calls this run"' in sidebar
    assert 'st.expander("Disclaimer"' in sidebar
    assert source.index('st.expander("Pillar weights"') < source.index(
        'st.expander("CMC calls this run"'
    )
    assert source.index('st.expander("CMC calls this run"') < source.index(
        'st.expander("Disclaimer"'
    )
    assert "_render_sidebar_tail" in source
    assert 'st.markdown("#### CMC calls this run")' not in source
    assert "st.write(DISCLAIMER)" in sidebar
    assert "[Privacy Policy](/privacy)" in sidebar
    assert "[Terms of Service](/terms)" in sidebar
    assert "These do not replace the Disclaimer." in sidebar
    assert "sidebar_legend_markdown" in sidebar
    assert "sidebar_weights_markdown" in sidebar
    assert 'st.header("Demo controls")' not in source
    assert 'st.markdown("### Legal")' not in source
    assert "st.warning(" not in sidebar
    assert "st.success(" not in sidebar
    assert "CMC_API_KEY is not set" in sidebar

    legend = demo_app.sidebar_legend_markdown()
    assert "self-reported CMC" in legend
    assert "heuristic fallback" in legend
    assert "Educational demo" in legend
    assert legend.count("\n- ") + 1 == len(demo_app.heuristic_legend_lines())

    weights = demo_app.sidebar_weights_markdown()
    for key, weight in demo_app.WEIGHTS.items():
        assert demo_app.PILLARS[key]["label"] in weights
        assert demo_app.PILLARS[key]["what"] in weights
        assert f"{weight:.0%}" in weights
    assert "Cross-issuer basis" in weights

    config = (Path(__file__).resolve().parents[1] / ".streamlit" / "config.toml").read_text(
        encoding="utf-8"
    )
    assert "showSidebarNavigation = false" in config


def test_ui_heuristic_legend_and_selected_slot_badges(fixture_scorer) -> None:
    import app as demo_app

    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert "How scores are labeled" in source
    assert "heuristic_legend_lines" in source
    assert "selected_slot_verification_lines" in source
    assert "Remaining heuristics stay labeled" in source

    lines = demo_app.heuristic_legend_lines()
    assert any("self-reported CMC" in line for line in lines)
    assert any("heuristic fallback" in line for line in lines)
    assert any("not financial advice" in line.lower() or "Educational demo" in line for line in lines)

    report = fixture_scorer.score("NVDA")
    slot = demo_app.selected_slot_verification_lines(report)
    assert len(slot) == 7
    assert slot[0].startswith("FIXTURE data · strongest check:")
    assert "heuristic fallback" in slot[0]
    assert "self-reported" not in slot[0]
    assert any("heuristic fallback" in line for line in slot)
    assert any("Cross-issuer basis" in line for line in slot)
    assert any("CMC field (self-reported)" in line for line in slot)
    assert not any(line.startswith("Cross-issuer basis — Verification:") for line in slot)

    live_por = {
        "ticker": "bNVDA",
        "data_source": "cmc",
        "live_verifiers": True,
        "verification": {
            "backing": {
                "level": "on-chain PoR",
                "source": "chainlink_por",
                "evidence": "published feed",
            },
            "reserves": {
                "level": "on-chain PoR",
                "source": "chainlink_por",
                "evidence": "published feed",
            },
            "redemption": {
                "level": "self-reported",
                "source": "issuer",
                "evidence": "issuer page",
            },
            "price": {"level": "self-reported", "source": "cmc", "evidence": "quote"},
            "disclosure": {"level": "self-reported", "source": "cmc", "evidence": "cik"},
            "basis": {"level": "self-reported", "source": "cmc", "evidence": "pairs"},
        },
    }
    live_lines = demo_app.selected_slot_verification_lines(live_por)
    assert live_lines[0] == "LIVE data · strongest check: on-chain PoR"
    assert any("Verification: on-chain PoR" in line for line in live_lines)
    cmc_lines = [
        line
        for line in live_lines
        if line.startswith(("Price integrity", "Disclosure", "Cross-issuer basis"))
    ]
    assert cmc_lines
    assert all("CMC field (self-reported)" in line for line in cmc_lines)
    assert all("Verification: self-reported" not in line for line in cmc_lines)
    labeled = demo_app.heuristic_legend_lines(report)
    assert any("fixture" in line.lower() for line in labeled)


def test_ui_surfaces_sixth_pillar_badge(fixture_scorer) -> None:
    import app as demo_app
    from rwa_score.scorer import PILLARS, WEIGHTS

    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert "Cross-issuer basis" in source
    assert "CMC RWA quotes/market-pairs" in source or "cmc_rwa_quotes" in source
    assert "basis" in WEIGHTS
    assert PILLARS["basis"]["label"] == "Cross-issuer basis"

    report = fixture_scorer.score("NVDA")
    badge, evidence = demo_app._verification_badge_label("basis", report)
    assert badge == "CMC field (self-reported)"
    assert "self-reported" in badge
    assert "quotes" in evidence.lower() or "market-pairs" in evidence.lower()
    results = demo_app._score_slots(fixture_scorer, ["NVDA", "TSLA", "AAPL", "META"])
    assert all(row[1] is not None for row in results)
    assert all("basis" in row[1]["subscores"] for row in results)


def test_readme_documents_basis_weight_table() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
    assert "Cross-issuer basis" in text
    assert "| Cross-issuer basis |" in text
    assert "20%" in text
    assert "15%" in text
    assert "market-pairs" in text
    assert "quotes/latest" in text
    assert "assets/list" in text
    assert "Weights sum to **100%**" in text


def test_health_launcher_reminder_and_cmc_calls_strip(fixture_scorer) -> None:
    import app as demo_app

    assert demo_app.health_launcher_reminder(launcher_set=True) is None
    warning = demo_app.health_launcher_reminder(launcher_set=False)
    assert warning is not None
    assert "streamlit run app.py" in warning
    assert "python -m rwa_score.health" in warning
    assert "cannot change the Render dashboard" in warning

    report = fixture_scorer.score("NVDA")
    block = demo_app.collect_cmc_calls([report], fixture_scorer.client)
    assert block["live"] is False
    lines = demo_app.format_cmc_calls_lines(block)
    assert lines[0].startswith("CMC calls this run — Fixture")
    assert "not" in lines[0].lower() and "live" in lines[0].lower()
    assert any("quotes/latest" in line for line in lines)
    assert not any(" · live" in line for line in lines)

    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert "CMC calls this run" in source
    assert "_render_sidebar_tail" in source
    assert "health_launcher_reminder" in source
    unknown = demo_app.format_cmc_calls_lines(
        {"source": "mystery", "endpoints": [{"endpoint": "/v5/x", "source": "mystery"}]}
    )
    assert "not labeled live" in unknown[0].lower()
    assert "Live CMC" not in unknown[0]
    assert " · live" not in unknown[1]
