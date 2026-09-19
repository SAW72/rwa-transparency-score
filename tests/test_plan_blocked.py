"""CMC market-pairs error 1006 / HTTP 403 plan-block degrade."""

from __future__ import annotations

from pathlib import Path

from rwa_score.ask_rat import TOOL_MARKET_PAIRS, TOOL_SCORE_TICKER, execute_tool
from rwa_score.client import (
    CMCPlanBlockedError,
    ENDPOINT_MARKET_PAIRS,
    PLAN_BLOCKED_LABEL,
    plan_blocked_market_pairs_payload,
)
from rwa_score.scorer import (
    PLAN_BLOCKED_BASIS_SCORE,
    SOURCE_MARKET_PAIRS_PLAN_BLOCKED,
    SOURCE_RWA_QUOTES,
    TransparencyScorer,
    remaining_heuristic_paths,
)
from tests.conftest import RecordingClient


def _plan_block(
    message: str = "Your API Key subscription plan doesn't support this endpoint.",
) -> CMCPlanBlockedError:
    return CMCPlanBlockedError(
        ENDPOINT_MARKET_PAIRS,
        403,
        {"status": {"error_code": 1006, "error_message": message}},
    )


def test_basis_plan_blocked_1006_does_not_crash_other_pillars() -> None:
    client = RecordingClient(market_pairs_error=_plan_block())
    report = TransparencyScorer(client, use_live_verifiers=False).score("NVDA")
    assert report["basis"]["plan_blocked"] is True
    assert report["basis"]["available"] is False
    assert report["basis"]["source"] == SOURCE_MARKET_PAIRS_PLAN_BLOCKED
    assert report["subscores"]["basis"] == PLAN_BLOCKED_BASIS_SCORE
    assert PLAN_BLOCKED_LABEL in report["verification"]["basis"]["evidence"]
    assert "not live market-pairs" in report["verification"]["basis"]["evidence"]
    assert report["verification"]["basis"]["ok"] is False
    assert report["verification"]["basis"]["meta"]["plan_blocked"] is True
    assert any(PLAN_BLOCKED_LABEL in f for f in report["flags"])
    # Other pillars still score — heuristic / CMC info path unchanged.
    assert report["subscores"]["backing"] == 90.0
    assert report["subscores"]["reserves"] == 90.0
    assert report["subscores"]["redemption"] == 85.0
    assert report["subscores"]["disclosure"] == 80.0
    assert "price" in report["subscores"]
    assert report["band"] in {"GREEN", "YELLOW", "ORANGE", "RED"}


def test_basis_plan_blocked_payload_is_not_treated_as_live_pairs() -> None:
    client = RecordingClient(
        market_pairs={2: plan_blocked_market_pairs_payload(rwa_id=2, symbol="NVDA")}
    )
    report = TransparencyScorer(client, use_live_verifiers=False).score("NVDA")
    assert report["basis"]["plan_blocked"] is True
    assert report["basis"]["available"] is False
    assert report["verification"]["basis"]["source"] == SOURCE_MARKET_PAIRS_PLAN_BLOCKED
    assert "cmc_market_pairs" != report["basis"]["source"]
    assert "+market_pairs" not in str(report["basis"]["source"] or "")


def test_basis_plan_blocked_keeps_quotes_only_and_labels_honestly() -> None:
    client = RecordingClient(
        rwa_quotes={
            2: {
                "tokens": [
                    {
                        "symbol": "NVDAx",
                        "price": 100.0,
                        "crypto_id": 99,
                        "issuer_name": "Backed",
                    },
                    {
                        "symbol": "NVDAon",
                        "price": 102.0,
                        "crypto_id": 100,
                        "issuer_name": "Ondo",
                    },
                ]
            }
        },
        market_pairs_error=_plan_block(),
    )
    report = TransparencyScorer(client, use_live_verifiers=False).score("NVDA")
    assert report["basis"]["plan_blocked"] is True
    assert report["basis"]["source"] == SOURCE_RWA_QUOTES
    assert report["verification"]["basis"]["source"] == SOURCE_RWA_QUOTES
    assert report["basis"]["wrapper_count"] == 2
    assert PLAN_BLOCKED_LABEL in report["verification"]["basis"]["evidence"]
    assert "not live market-pairs" in report["verification"]["basis"]["evidence"]
    assert "+market_pairs" not in report["basis"]["source"]


def test_remaining_heuristics_mark_basis_plan_blocked() -> None:
    rows = remaining_heuristic_paths(
        {
            "basis": {
                "source": SOURCE_MARKET_PAIRS_PLAN_BLOCKED,
                "evidence": f"Cross-issuer basis: {PLAN_BLOCKED_LABEL}",
                "meta": {"plan_blocked": True},
            }
        },
        data_source="live",
        live_verifiers=True,
    )
    basis = next(row for row in rows if row["key"] == "basis")
    assert basis["kind"] == "plan-blocked"
    assert PLAN_BLOCKED_LABEL in basis["note"]


def test_ask_rat_market_pairs_tool_degrades_on_1006() -> None:
    scorer = TransparencyScorer(
        RecordingClient(market_pairs_error=_plan_block()),
        use_live_verifiers=False,
    )
    pairs = execute_tool(TOOL_MARKET_PAIRS, {"symbol": "NVDA"}, scorer=scorer)
    assert pairs["ok"] is False
    assert pairs["plan_blocked"] is True
    assert pairs["label"] == PLAN_BLOCKED_LABEL
    assert pairs["tool"] == TOOL_MARKET_PAIRS
    scored = execute_tool(TOOL_SCORE_TICKER, {"symbol": "NVDA"}, scorer=scorer)
    assert scored["ok"] is True
    assert scored["ticker"] == "NVDA"
    assert scored["basis"]["plan_blocked"] is True
    assert scored["basis"]["label"] == PLAN_BLOCKED_LABEL


def test_ask_rat_market_pairs_tool_reads_plan_blocked_payload() -> None:
    scorer = TransparencyScorer(
        RecordingClient(
            market_pairs={2: plan_blocked_market_pairs_payload(rwa_id=2, symbol="NVDA")}
        ),
        use_live_verifiers=False,
    )
    pairs = execute_tool(TOOL_MARKET_PAIRS, {"rwa_id": 2}, scorer=scorer)
    assert pairs["ok"] is False
    assert pairs["plan_blocked"] is True
    assert pairs["label"] == PLAN_BLOCKED_LABEL


def test_ui_basis_caption_and_badge_are_plan_blocked() -> None:
    import app as demo_app

    caption = demo_app.basis_status_caption(
        {
            "plan_blocked": True,
            "available": False,
            "source": SOURCE_MARKET_PAIRS_PLAN_BLOCKED,
        }
    )
    assert caption is not None
    assert f"**{PLAN_BLOCKED_LABEL}**" in caption
    assert "not live market-pairs data" in caption

    report = {
        "basis": {"plan_blocked": True, "available": False},
        "verification": {
            "basis": {
                "level": "self-reported",
                "source": SOURCE_MARKET_PAIRS_PLAN_BLOCKED,
                "evidence": f"Cross-issuer basis: {PLAN_BLOCKED_LABEL}",
                "meta": {"plan_blocked": True},
            }
        },
    }
    badge, evidence = demo_app._verification_badge_label("basis", report)
    assert badge == PLAN_BLOCKED_LABEL
    assert PLAN_BLOCKED_LABEL in evidence
    slot = demo_app.selected_slot_verification_lines(
        {
            "ticker": "NVDA",
            "data_source": "cmc",
            "live_verifiers": True,
            "verification": {
                "backing": {"level": "self-reported", "source": "heuristic_fallback", "evidence": "h"},
                "reserves": {"level": "self-reported", "source": "heuristic_fallback", "evidence": "h"},
                "redemption": {"level": "self-reported", "source": "heuristic_fallback", "evidence": "h"},
                "price": {"level": "self-reported", "source": "cmc", "evidence": "q"},
                "disclosure": {"level": "self-reported", "source": "cmc", "evidence": "cik"},
                "basis": report["verification"]["basis"],
            },
            "basis": report["basis"],
        }
    )
    assert any(
        line.startswith("Cross-issuer basis — ") and PLAN_BLOCKED_LABEL in line
        for line in slot
    )


def test_ui_caption_still_shows_spread_when_not_plan_blocked() -> None:
    import app as demo_app

    caption = demo_app.basis_status_caption(
        {
            "available": True,
            "percent_spread": 0.21,
            "wrapper_count": 3,
            "source": "cmc_rwa_quotes+market_pairs",
            "plan_blocked": False,
        }
    )
    assert caption is not None
    assert "0.21%" in caption
    assert PLAN_BLOCKED_LABEL not in caption


def test_card_details_uses_basis_status_caption() -> None:
    import app as demo_app

    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    details = source.split("def _render_card_details", 1)[1].split(
        "def _render_compare_card", 1
    )[0]
    assert "basis_status_caption" in details
    assert "PLAN_BLOCKED_LABEL" in source
    assert PLAN_BLOCKED_LABEL == "plan-blocked / unavailable"
