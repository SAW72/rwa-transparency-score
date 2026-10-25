"""A failed live check must not print GREEN.

The production bug: Backed Finance name-list stand-ins (backing 90, reserves 90,
redemption 85) plus a CIK (disclosure 80) and missing price/basis (50/50) sum
to 75.75 and print GREEN when the live probes failed.

On unfixed main the 75.75 assertion fails because the report band is GREEN.
"""

from __future__ import annotations

from rwa_score.scorer import (
    NOT_GREEN_CAP,
    WEIGHTS,
    TransparencyScorer,
    band_code,
    remaining_heuristic_paths,
)
from rwa_score.verifiers import (
    VerificationLevel,
    VerificationResult,
    heuristic_result,
)
from tests.conftest import RecordingClient


def _legacy_green_stand_in() -> float:
    """Name-list plus CIK plus missing price/basis. This is the bug number."""
    return (
        90.0 * WEIGHTS["backing"]
        + 90.0 * WEIGHTS["reserves"]
        + 85.0 * WEIGHTS["redemption"]
        + 50.0 * WEIGHTS["price"]
        + 80.0 * WEIGHTS["disclosure"]
        + 50.0 * WEIGHTS["basis"]
    )


class _DeadSession:
    """Live backing, reserves, and redemption all fail."""

    def post(self, *args, **kwargs):
        raise RuntimeError("rpc down")

    def get(self, *args, **kwargs):
        raise RuntimeError("docs down")


def _failed_live_client() -> RecordingClient:
    """Backed Finance, CIK present, price 50, basis 50."""
    return RecordingClient(
        quotes={99: {"quote": {}}},
        market_pairs={},
    )


def test_exact_75_75_stand_in_would_print_green() -> None:
    legacy = _legacy_green_stand_in()
    assert legacy == 75.75
    assert band_code(legacy) == "GREEN"
    assert band_code(round(legacy, 1)) == "GREEN"


def test_failed_live_check_cannot_print_green_at_75_75() -> None:
    from rwa_score.verifiers import BackedVerifier

    report = TransparencyScorer(
        _failed_live_client(),
        verifiers={"backed": BackedVerifier(session=_DeadSession())},
        use_live_verifiers=True,
    ).score("NVDA")

    assert report["subscores"]["price"] == 50.0
    assert report["subscores"]["disclosure"] == 80.0
    assert report["subscores"]["basis"] == 50.0
    for key, forbidden in (
        ("backing", 90.0),
        ("reserves", 90.0),
        ("redemption", 85.0),
    ):
        assert report["subscores"][key] is None
        assert report["subscores"][key] != forbidden
        block = report["verification"][key]
        assert block["source"] == "not_available"
        assert block["level"] == "not available"
        assert block["score"] is None
        assert block["ok"] is False
        assert block["error"]
        assert "not available" in block["evidence"].lower()
        assert "name-list" in block["evidence"].lower()
        assert "heuristic fallback" not in block["evidence"].lower()
        assert "not available" in report["explanations"][key].lower()

    available = (
        report["subscores"]["price"] * WEIGHTS["price"]
        + report["subscores"]["disclosure"] * WEIGHTS["disclosure"]
        + report["subscores"]["basis"] * WEIGHTS["basis"]
    )
    assert report["score"] == round(available, 1)
    assert report["score"] < 75
    assert report["score"] != round(75.75, 1)
    assert report["band"] != "GREEN"
    assert report["band"] == band_code(report["score"])
    assert not report["band_label"].startswith("GREEN")
    assert any("not available" in note.lower() for note in report["notes"])
    assert any("not GREEN" in note for note in report["notes"])


def test_one_failed_live_check_cannot_print_green() -> None:
    """Successful pillars that would clear 75 still must not print GREEN."""

    class SplitVerifier:
        name = "backed"

        def verify_backing(self, *, ticker: str, issuer_name: str) -> VerificationResult:
            return VerificationResult(
                score=95.0,
                level=VerificationLevel.ON_CHAIN_POR,
                evidence="Chainlink PoR ok",
                source="chainlink_por",
                ok=True,
            )

        def verify_reserves(self, *, ticker: str, issuer_name: str) -> VerificationResult:
            return VerificationResult(
                score=95.0,
                level=VerificationLevel.ON_CHAIN_POR,
                evidence="Chainlink PoR ok",
                source="chainlink_por",
                ok=True,
            )

        def verify_redemption(self, *, ticker: str, issuer_name: str) -> VerificationResult:
            return heuristic_result(
                "redemption",
                issuer_name,
                reason="Backed docs fetch failed.",
                error="503 docs down",
            )

    client = RecordingClient(
        quotes={99: {"quote": {"USD": {"percent_change_24h": 0.0, "price": 100.0}}}},
        market_pairs={
            2: {
                "rwa_id": 2,
                "symbol": "NVDA",
                "market_pairs": [
                    {
                        "market_pair": "NVDAx/USDT",
                        "market_pair_base": {"crypto_id": 99, "symbol": "NVDAx"},
                        "quotes": [{"symbol": "USD", "price": 100.0, "volume_24h": 10.0}],
                    },
                    {
                        "market_pair": "NVDAon/USDT",
                        "market_pair_base": {"crypto_id": 100, "symbol": "NVDAon"},
                        "quotes": [{"symbol": "USD", "price": 100.0, "volume_24h": 10.0}],
                    },
                ],
            }
        },
    )
    report = TransparencyScorer(
        client,
        verifiers={"backed": SplitVerifier()},
        use_live_verifiers=True,
    ).score("NVDA")

    assert report["subscores"]["backing"] == 95.0
    assert report["subscores"]["reserves"] == 95.0
    assert report["subscores"]["redemption"] is None
    assert report["verification"]["backing"]["source"] == "chainlink_por"
    assert report["verification"]["redemption"]["source"] == "not_available"
    assert "not available" in report["verification"]["redemption"]["evidence"].lower()

    raw_total = 0.0
    for key, weight in WEIGHTS.items():
        value = report["subscores"][key]
        if value is not None:
            raw_total += float(value) * weight
    assert raw_total >= 75
    assert band_code(round(raw_total, 1)) == "GREEN"
    assert report["score"] == NOT_GREEN_CAP
    assert report["score"] < 75
    assert report["band"] == "YELLOW"
    assert report["band"] != "GREEN"
    assert report["band"] == band_code(report["score"])
    assert "not available" in report["band_label"].lower()
    assert report["band_label"].startswith("YELLOW")


def test_offline_name_list_is_not_a_failed_live_check() -> None:
    report = TransparencyScorer(
        RecordingClient(), use_live_verifiers=False
    ).score("NVDA")
    assert report["verification"]["backing"]["source"] == "heuristic_fallback"
    assert report["subscores"]["backing"] == 90.0
    assert report["subscores"]["reserves"] == 90.0
    assert report["subscores"]["redemption"] == 85.0


def test_failed_live_card_labels_not_available() -> None:
    import app as demo_app
    from rwa_score.score_card import canonical_score_payload, share_caption
    from rwa_score.verifiers import BackedVerifier

    report = TransparencyScorer(
        _failed_live_client(),
        verifiers={"backed": BackedVerifier(session=_DeadSession())},
        use_live_verifiers=True,
    ).score("NVDA")
    rows = {row["key"]: row for row in demo_app.pillar_evidence_rows(report)}
    for key in ("backing", "reserves", "redemption"):
        assert rows[key]["score"] is None
        assert rows[key]["badge"] == "not available"
        assert "not available" in rows[key]["evidence"].lower()
        assert demo_app.format_pillar_score(rows[key]["score"]) == "not available"
    assert "Not available: Backing model" in demo_app.weakest_pillar_line(report)
    assert "·" in demo_app.pillar_dots(report)
    kinds = [
        row["kind"]
        for row in remaining_heuristic_paths(
            report["verification"],
            data_source=report["data_source"],
            live_verifiers=True,
        )
        if row["key"] in {"backing", "reserves", "redemption"}
    ]
    assert kinds == ["not available", "not available", "not available"]

    payload = canonical_score_payload(report, "2026-10-24T00:00:00Z")
    assert payload["subscores"]["backing"] is None
    assert payload["band"] != "GREEN"
    caption = share_caption(payload, "abc123def4567890")
    assert "Backing not available" in caption
    assert "not GREEN" not in payload["band"]
