"""Band chip + pillar evidence helpers — published fields only."""

from __future__ import annotations

from pathlib import Path

from rwa_score.scorer import PILLARS, WEIGHTS


def test_normalize_band_and_chip_spec() -> None:
    import app as demo_app

    assert demo_app.normalize_band("green") == "GREEN"
    assert demo_app.normalize_band("YELLOW") == "YELLOW"
    assert demo_app.normalize_band(None, score=80) == "GREEN"
    assert demo_app.normalize_band(None, score=40) == "ORANGE"
    assert demo_app.normalize_band(None, score=10) == "RED"
    assert demo_app.normalize_band("PURPLE") == ""
    assert demo_app.normalize_band("") == ""

    green = demo_app.band_chip_spec("GREEN")
    assert green["band"] == "GREEN"
    assert green["color"] == demo_app.BAND_COLORS["GREEN"]
    assert green["text_color"] == "#0D1117"

    red = demo_app.band_chip_spec("RED")
    assert red["color"] == demo_app.BAND_COLORS["RED"]
    assert red["text_color"] == "#FFFFFF"

    missing = demo_app.band_chip_spec("NOT-A-BAND")
    assert missing["band"] == ""
    assert missing["color"] == ""


def test_band_chip_html_is_scannable_pill() -> None:
    import app as demo_app

    html = demo_app.band_chip_html("GREEN")
    assert "GREEN" in html
    assert "rat-band-chip" in html
    assert "rat-band-green" in html
    assert demo_app.BAND_COLORS["GREEN"] in html
    assert "rat-band-chip-selected" not in html

    selected = demo_app.band_chip_html("ORANGE", selected=True)
    assert "ORANGE" in selected
    assert "rat-band-chip-selected" in selected
    assert demo_app.BAND_COLORS["ORANGE"] in selected

    assert demo_app.band_chip_html("PURPLE") == ""
    assert demo_app.band_chip_html(None, score=74.9).startswith("<span")
    assert "YELLOW" in demo_app.band_chip_html(None, score=74.9)

    escaped = demo_app.band_chip_html("<script>")
    assert "<script>" not in escaped
    assert escaped == ""


def test_pillar_evidence_rows_reuse_existing_verification(fixture_scorer) -> None:
    import app as demo_app

    nvda = fixture_scorer.score("NVDA")
    rows = demo_app.pillar_evidence_rows(nvda)
    assert [row["key"] for row in rows] == list(WEIGHTS)
    assert all(row["label"] == PILLARS[row["key"]]["label"] for row in rows)
    assert all(row["what"] == PILLARS[row["key"]]["what"] for row in rows)
    reserves = next(row for row in rows if row["key"] == "reserves")
    assert "heuristic fallback" in reserves["badge"]
    assert "heuristic fallback" in reserves["evidence"].lower()
    assert "published Chainlink PoR" not in reserves["badge"]
    basis = next(row for row in rows if row["key"] == "basis")
    assert "self-reported" in basis["badge"]
    assert "CMC market-pairs" in basis["evidence"]


def test_pillar_evidence_rows_bnvda_fixture_por_skip(fixture_scorer) -> None:
    import app as demo_app

    report = fixture_scorer.score("bNVDA")
    rows = {row["key"]: row for row in demo_app.pillar_evidence_rows(report)}
    reserves = rows["reserves"]
    assert "published Chainlink PoR" in reserves["badge"]
    assert "bNVDA" in reserves["badge"]
    assert "fixture/offline skip" in reserves["badge"]
    assert "live RPC skipped" in reserves["evidence"]
    assert "on-chain PoR" not in reserves["badge"]
    backing = rows["backing"]
    assert "heuristic fallback" in backing["badge"]
    assert "published Chainlink PoR" in backing["badge"]


def test_card_surface_wired_into_compare_row() -> None:
    import app as demo_app

    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert "def band_chip_html" in source
    assert "def pillar_evidence_rows" in source
    assert "rat-band-chip" in source
    card = source.split("def _render_compare_card", 1)[1].split(
        "def _render_slot_error", 1
    )[0]
    assert "band_chip_html" in card
    assert "_render_card_details" in card
    assert "_render_why_this_score" in card
    details = source.split("def _render_card_details", 1)[1].split(
        "def _render_compare_card", 1
    )[0]
    assert 'st.expander("Pillar evidence"' in details
    assert "pillar_evidence_rows" in details
    assert "Evidence:" in details
    assert "_cached_explanation" not in details
    assert "share_score_card" not in details
    assert "Educational demo — existing verification evidence" in details
