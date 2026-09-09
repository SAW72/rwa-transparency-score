"""Smoke-import the Streamlit entrypoint without launching the server."""

from pathlib import Path

import pytest

from tests.conftest import RecordingClient

README_ONELINER = (
    "RAT Score (RWA Transparency Score) — an AI-assisted risk radar for tokenized stocks. "
    "Rates issuers 0–100 on backing, proof of reserves, redemption, price integrity, "
    "and disclosure using CoinMarketCap’s RWA API."
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
    assert (demo_app.ASSETS_DIR / "rat-monogram.svg").is_file()
    uri = demo_app._asset_data_uri(demo_app.MONOGRAM_PATH)
    assert uri is not None and uri.startswith("data:image/png;base64,")


def test_app_module_exports_helpers() -> None:
    import app as demo_app

    assert "NVDA" in demo_app.FIXTURE_TICKERS
    assert demo_app.DEFAULT_SLOTS == ["NVDA", "TSLA", "AAPL", "META"]
    assert demo_app.MAX_COMPARE_SLOTS == 4
    assert demo_app.DEFAULT_SLOTS == demo_app.FIXTURE_TICKERS
    assert "financial advice" in demo_app.DISCLAIMER.lower()
    assert set(demo_app.BAND_COLORS) == {"GREEN", "YELLOW", "ORANGE", "RED"}


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
