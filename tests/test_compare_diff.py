"""Compare-row differentiation helpers — why 2–4 slots differ."""

from __future__ import annotations

from pathlib import Path

from rwa_score.verifiers import VerificationLevel


def _subs(
    *,
    backing: float = 90,
    reserves: float = 90,
    redemption: float = 85,
    price: float = 90,
    disclosure: float = 80,
    basis: float = 90,
) -> dict[str, float]:
    return {
        "backing": backing,
        "reserves": reserves,
        "redemption": redemption,
        "price": price,
        "disclosure": disclosure,
        "basis": basis,
    }


def _report(
    ticker: str,
    *,
    score: float,
    band: str,
    issuer: str = "Backed Finance",
    subs: dict[str, float] | None = None,
    verification: dict | None = None,
    cue: str | None = None,
) -> dict:
    payload = {
        "ticker": ticker,
        "score": score,
        "band": band,
        "issuer": issuer,
        "subscores": subs or _subs(),
        "verification": verification or {},
    }
    if cue is not None:
        # Convenience: stamp the same kind on live-or-heuristic pillars.
        payload["verification"] = {
            key: {
                "level": cue
                if cue != "heuristic fallback"
                else VerificationLevel.SELF_REPORTED.value,
                "source": "heuristic_fallback" if cue == "heuristic fallback" else "test",
                "evidence": "heuristic fallback" if cue == "heuristic fallback" else cue,
            }
            for key in ("backing", "reserves", "redemption")
        }
    return payload


def test_join_tickers_and_verification_cue() -> None:
    import app as demo_app

    assert demo_app._join_tickers(["NVDA", "META"]) == "NVDA/META"
    assert demo_app._join_tickers(["NVDA", "TSLA", "AAPL", "META"]) == "NVDA/TSLA…"
    assert demo_app._join_tickers([]) == ""

    heuristic = _report("NVDA", score=90, band="GREEN", cue="heuristic fallback")
    assert demo_app.verification_cue(heuristic) == "heuristic fallback"
    assert demo_app.pillar_verification_kind("backing", heuristic) == "heuristic fallback"

    on_chain = _report("bNVDA", score=90, band="GREEN", cue=VerificationLevel.ON_CHAIN_POR.value)
    assert demo_app.verification_cue(on_chain) == VerificationLevel.ON_CHAIN_POR.value

    robinhood = _report(
        "HOOD",
        score=40,
        band="ORANGE",
        issuer="Robinhood",
        cue=VerificationLevel.SELF_REPORTED.value,
    )
    assert demo_app.verification_cue(robinhood) == VerificationLevel.SELF_REPORTED.value


def test_compare_differentiation_requires_two_reports() -> None:
    import app as demo_app

    empty = demo_app.compare_differentiation([])
    assert empty["available"] is False
    assert empty["headline"] == ""
    assert empty["cards"] == {}

    single = demo_app.compare_differentiation(
        [_report("NVDA", score=90.2, band="GREEN")]
    )
    assert single["available"] is False
    assert demo_app.card_contrast_line("NVDA", single) == ""
    assert demo_app.selected_slot_contrast_line("NVDA", single) == ""


def test_compare_differentiation_highlights_weighted_pillar_gap() -> None:
    import app as demo_app

    high = _report(
        "NVDA",
        score=90.2,
        band="GREEN",
        issuer="Backed Finance",
        subs=_subs(reserves=90, basis=98),
        cue="heuristic fallback",
    )
    low = _report(
        "TSLA",
        score=28.5,
        band="ORANGE",
        issuer="NoteVault Demo Issuer",
        subs=_subs(
            backing=35,
            reserves=30,
            redemption=25,
            price=43,
            disclosure=20,
            basis=15,
        ),
        cue="heuristic fallback",
    )
    diff = demo_app.compare_differentiation([high, low])
    assert diff["available"] is True
    assert diff["score_spread"] == 61.7
    assert "TSLA trails NVDA by 61.7" in diff["headline"]
    assert diff["largest_delta"] is not None
    # Basis 83-pt raw gap × 15% > reserves 60 × 20%.
    assert diff["largest_delta"]["pillar"] == "basis"
    assert "Cross-issuer basis drives the gap" in diff["headline"]
    assert "98 vs 15" in diff["headline"]
    assert any("Largest pillar gap: Cross-issuer basis" in line for line in diff["lines"])
    assert any("Bands: GREEN NVDA · ORANGE TSLA" in line for line in diff["lines"])
    assert any("all heuristic fallback" in line for line in diff["lines"])
    assert any("Issuers:" in line and "Backed Finance" in line for line in diff["lines"])
    assert "▲ Cross-issuer basis 98 vs TSLA 15" in demo_app.card_contrast_line("NVDA", diff)
    assert "▼ Cross-issuer basis 15 vs NVDA 98" in demo_app.card_contrast_line("TSLA", diff)
    assert "Highest in this row" in demo_app.selected_slot_contrast_line("NVDA", diff)
    assert "Lowest in this row" in demo_app.selected_slot_contrast_line("TSLA", diff)


def test_compare_differentiation_verification_badge_split() -> None:
    import app as demo_app

    backed = _report(
        "bNVDA",
        score=88.0,
        band="GREEN",
        issuer="Backed Finance",
        cue=VerificationLevel.ON_CHAIN_POR.value,
    )
    robinhood = _report(
        "TSLA",
        score=42.0,
        band="ORANGE",
        issuer="Robinhood",
        subs=_subs(backing=55, reserves=40, redemption=35, price=50, disclosure=50, basis=50),
        cue=VerificationLevel.SELF_REPORTED.value,
    )
    heuristic = _report(
        "AAPL",
        score=70.0,
        band="YELLOW",
        issuer="xStocks",
        subs=_subs(reserves=30),
        cue="heuristic fallback",
    )
    diff = demo_app.compare_differentiation([backed, robinhood, heuristic])
    assert diff["verification_differs"] is True
    groups = diff["verification_groups"]
    assert groups[VerificationLevel.ON_CHAIN_POR.value] == ["bNVDA"]
    assert groups[VerificationLevel.SELF_REPORTED.value] == ["TSLA"]
    assert groups["heuristic fallback"] == ["AAPL"]
    verify_line = next(line for line in diff["lines"] if line.startswith("Verification:"))
    assert "on-chain PoR (bNVDA)" in verify_line
    assert "heuristic fallback (AAPL)" in verify_line
    assert "self-reported (TSLA)" in verify_line
    assert diff["cards"]["bNVDA"]["verification"] == VerificationLevel.ON_CHAIN_POR.value
    assert diff["cards"]["TSLA"]["verification"] == VerificationLevel.SELF_REPORTED.value


def test_compare_differentiation_close_scores_do_not_invent_driver() -> None:
    import app as demo_app

    a = _report("NVDA", score=90.2, band="GREEN", subs=_subs(price=91))
    b = _report("META", score=89.9, band="GREEN", issuer="Ondo", subs=_subs(price=90))
    diff = demo_app.compare_differentiation([a, b])
    assert diff["available"] is True
    assert "Scores are close" in diff["headline"]
    assert "drives the gap" not in diff["headline"]
    assert diff["largest_delta"] is None
    assert demo_app.card_contrast_line("NVDA", diff) == ""
    assert demo_app.card_contrast_line("META", diff) == ""


def test_compare_differentiation_default_fixture_row(fixture_scorer) -> None:
    import app as demo_app

    results = demo_app._score_slots(fixture_scorer, list(demo_app.DEFAULT_SLOTS))
    reports = [row[1] for row in results if row[1] is not None]
    assert [row["ticker"] for row in reports] == ["NVDA", "TSLA", "AAPL", "META"]
    diff = demo_app.compare_differentiation(reports)
    assert diff["available"] is True
    assert diff["score_spread"] >= 55
    assert "TSLA trails NVDA" in diff["headline"]
    assert "drives the gap" in diff["headline"]
    assert set(diff["bands"]) == {"NVDA", "TSLA", "AAPL", "META"}
    assert diff["bands"]["NVDA"] == "GREEN"
    assert diff["bands"]["TSLA"] == "ORANGE"
    assert diff["bands"]["AAPL"] == "YELLOW"
    assert diff["cards"]["NVDA"]["delta_role"] == "high"
    assert diff["cards"]["TSLA"]["delta_role"] == "low"
    # Mid-pack AAPL is neither the score leader nor the trailer.
    assert "below NVDA" in diff["cards"]["AAPL"]["row_role"]
    assert any("Issuers:" in line for line in diff["lines"])


def test_compare_callout_and_card_wire_into_app() -> None:
    import app as demo_app

    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert "compare_differentiation" in source
    assert "Why these differ" in source
    assert "_render_compare_callout" in source
    assert "Educational compare — heuristic scores, not financial advice." in source
    card = source.split("def _render_compare_card", 1)[1].split(
        "def _render_slot_error", 1
    )[0]
    assert "band" in card
    assert "band_chip_html" in card
    assert "delta_color=\"off\"" in card
    assert "card_contrast_line" in card
    assert "verification_differs" in card
    assert "Verify:" in card
    assert "_render_card_details" in card
    assert "st.progress" not in card
    assert "mode_cue" in card
    assert "weakest_pillar_line" in card
    assert "pillar_dots" in card
    details = source.split("def _render_card_details", 1)[1].split(
        "def _render_compare_card", 1
    )[0]
    assert 'st.expander("Pillar evidence"' in details
    assert "_cached_explanation" not in details
    assert "Share score card" not in details
    detail = source.split("def _render_selected_slot_detail", 1)[1].split(
        "st.markdown(", 1
    )[0]
    assert "selected_slot_contrast_line" in detail
    assert "Pillar detail" not in detail
