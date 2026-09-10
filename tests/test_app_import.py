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
    assert 'class="mode-chip"' in source
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
    assert "Next upgrade:" in text
    assert "no on-chain Chainlink PoR / SmartData proxy addresses for the xStocks line" in text
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
    assert "Why this score?" in source
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


def test_score_card_escapes_html() -> None:
    import app as demo_app

    markup = demo_app._score_card_html(
        {
            "score": 80.0,
            "band": "GREEN",
            "band_label": "GREEN — heuristic: stronger transparency signals (still verify)",
            "ticker": "<script>alert(1)</script>",
            "issuer": "x<y>&z",
            "summary": 'hi & bye <img src=x onerror=alert(1)>',
        }
    )
    assert "<script>" not in markup
    assert "<img" not in markup
    assert "&lt;script&gt;" in markup
    assert "x&lt;y&gt;&amp;z" in markup
    assert "hi &amp; bye" in markup
    assert "&lt;img" in markup


def test_error_card_escapes_html() -> None:
    import app as demo_app

    markup = demo_app._error_card_html("<b>NVDA</b>", "boom <script>x</script>")
    assert "<b>" not in markup
    assert "<script>" not in markup
    assert "&lt;b&gt;NVDA&lt;/b&gt;" in markup
    assert "&lt;script&gt;" in markup


def test_ui_surfaces_sixth_pillar_badge(fixture_scorer) -> None:
    import app as demo_app
    from rwa_score.scorer import PILLARS, WEIGHTS

    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert "Cross-issuer basis" in source
    assert "self-reported CMC market-pairs" in source
    assert "basis" in WEIGHTS
    assert PILLARS["basis"]["label"] == "Cross-issuer basis"

    report = fixture_scorer.score("NVDA")
    badge, evidence = demo_app._verification_badge_label("basis", report)
    assert "self-reported" in badge
    assert "CMC market-pairs" in evidence
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
    assert "Weights sum to **100%**" in text
