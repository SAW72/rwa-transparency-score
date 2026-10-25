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


class _BareTimeoutVerifier:
    """Every live method raises ``TimeoutError()`` with an empty message."""

    name = "backed"

    def verify_backing(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        raise TimeoutError()

    def verify_reserves(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        raise TimeoutError()

    def verify_redemption(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        raise TimeoutError()


class _RedemptionConnectionError:
    """Backing and reserves succeed. Redemption raises ``ConnectionError()``."""

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
        raise ConnectionError()


def _high_quote_client() -> RecordingClient:
    return RecordingClient(
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


def test_bare_timeout_error_is_not_green() -> None:
    """Scorer catch-all. Empty TimeoutError used to print 75.8 GREEN."""
    report = TransparencyScorer(
        _failed_live_client(),
        verifiers={"backed": _BareTimeoutVerifier()},
        use_live_verifiers=True,
    ).score("NVDA")
    assert round(_legacy_green_stand_in(), 1) == 75.8
    assert report["score"] != 75.8
    assert report["band"] != "GREEN"
    assert report["score"] == 27.0
    assert report["band"] == "ORANGE"
    for key in ("backing", "reserves", "redemption"):
        assert report["subscores"][key] is None
        block = report["verification"][key]
        assert block["source"] == "not_available"
        assert block["error"] == "TimeoutError"
        assert block["score"] is None


def test_bare_connection_error_on_redemption_is_not_green() -> None:
    """Redemption catch-all. Empty ConnectionError used to print 92.8 GREEN."""
    stand_in = (
        95.0 * WEIGHTS["backing"]
        + 95.0 * WEIGHTS["reserves"]
        + 85.0 * WEIGHTS["redemption"]
        + 100.0 * WEIGHTS["price"]
        + 80.0 * WEIGHTS["disclosure"]
        + 100.0 * WEIGHTS["basis"]
    )
    assert round(stand_in, 1) == 92.8
    report = TransparencyScorer(
        _high_quote_client(),
        verifiers={"backed": _RedemptionConnectionError()},
        use_live_verifiers=True,
    ).score("NVDA")
    assert report["subscores"]["backing"] == 95.0
    assert report["subscores"]["reserves"] == 95.0
    assert report["subscores"]["redemption"] is None
    assert report["verification"]["redemption"]["error"] == "ConnectionError"
    assert report["verification"]["redemption"]["source"] == "not_available"
    assert report["score"] != 92.8
    assert report["score"] == NOT_GREEN_CAP
    assert report["band"] != "GREEN"
    assert report["band"] == "YELLOW"


def test_empty_error_string_is_a_failed_live_check() -> None:
    """Guards use ``is not None``, so an empty message is still a failure."""
    from rwa_score.scorer import _append_verification_notes
    from rwa_score.verifiers import live_check_failed

    missed = heuristic_result(
        "backing",
        "Backed Finance",
        reason="probe returned no message",
        error="",
    )
    assert missed.source == "not_available"
    assert missed.score is None
    assert live_check_failed(missed) is True

    stored_empty = VerificationResult(
        score=90.0,
        level=VerificationLevel.SELF_REPORTED,
        evidence="heuristic fallback",
        source="heuristic_fallback",
        error="",
        ok=False,
    )
    assert live_check_failed(stored_empty) is True
    text = _append_verification_notes("Lead.", stored_empty)
    assert "Verifier failure recorded" in text

    class _BlankMessage:
        name = "backed"

        def _result(self) -> VerificationResult:
            return VerificationResult(
                score=90.0,
                level=VerificationLevel.SELF_REPORTED,
                evidence="name list",
                source="heuristic_fallback",
                error="",
                ok=False,
            )

        def verify_backing(self, *, ticker: str, issuer_name: str) -> VerificationResult:
            return self._result()

        def verify_reserves(self, *, ticker: str, issuer_name: str) -> VerificationResult:
            return self._result()

        def verify_redemption(self, *, ticker: str, issuer_name: str) -> VerificationResult:
            return self._result()

    report = TransparencyScorer(
        _failed_live_client(),
        verifiers={"backed": _BlankMessage()},
        use_live_verifiers=True,
    ).score("NVDA")
    assert report["band"] != "GREEN"
    assert report["subscores"]["backing"] is None
    assert report["verification"]["backing"]["error"] == ""
    assert any(
        note.startswith("Verifier error (not dropped):") for note in report["notes"]
    )


def test_raw_score_of_exactly_75_stays_below_green() -> None:
    """``published >= 75`` includes 75.0. That boundary must not print GREEN."""
    assert band_code(75.0) == "GREEN"
    assert band_code(NOT_GREEN_CAP) == "YELLOW"

    class _Boundary:
        name = "backed"

        def verify_backing(self, *, ticker: str, issuer_name: str) -> VerificationResult:
            return VerificationResult(
                score=90.0,
                level=VerificationLevel.ON_CHAIN_POR,
                evidence="ok",
                source="chainlink_por",
                ok=True,
            )

        def verify_reserves(self, *, ticker: str, issuer_name: str) -> VerificationResult:
            return VerificationResult(
                score=90.0,
                level=VerificationLevel.ON_CHAIN_POR,
                evidence="ok",
                source="chainlink_por",
                ok=True,
            )

        def verify_redemption(self, *, ticker: str, issuer_name: str) -> VerificationResult:
            raise TimeoutError()

    scorer = TransparencyScorer(
        RecordingClient(),
        verifiers={"backed": _Boundary()},
        use_live_verifiers=True,
    )
    scorer._price_score = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
        100.0,
        {"available": False, "source": "cmc_rwa_quotes"},
        [],
        "price",
    )
    scorer._basis_score = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
        80.0,
        {"available": False, "wrapper_count": 0, "source": "cmc_market_pairs"},
        [],
        "basis",
    )
    raw = (
        90.0 * WEIGHTS["backing"]
        + 90.0 * WEIGHTS["reserves"]
        + 100.0 * WEIGHTS["price"]
        + 80.0 * WEIGHTS["disclosure"]
        + 80.0 * WEIGHTS["basis"]
    )
    assert raw == 75.0
    report = scorer.score("NVDA")
    assert report["subscores"]["redemption"] is None
    assert report["verification"]["redemption"]["error"] == "TimeoutError"
    assert report["score"] == NOT_GREEN_CAP
    assert report["band"] != "GREEN"
    assert report["band"] == "YELLOW"


def test_verifier_catches_store_the_exception_type_when_the_message_is_empty() -> None:
    """The four verifier catches, not only the scorer catch-all."""
    from rwa_score.verifiers import BackedVerifier, DinariVerifier

    class _Reader:
        def read(self, ticker: str, *, feeds=None):
            raise TimeoutError()

    class _Get:
        def get(self, *args, **kwargs):
            raise ConnectionError()

    por = BackedVerifier(client=_Reader()).verify_reserves(
        ticker="NVDA", issuer_name="Backed Finance"
    )
    assert por.source == "not_available"
    assert por.score is None
    assert por.error == "TimeoutError"

    backed_docs = BackedVerifier(session=_Get()).verify_redemption(
        ticker="NVDA", issuer_name="Backed Finance"
    )
    assert backed_docs.source == "not_available"
    assert backed_docs.error == "ConnectionError"

    dinari_page = DinariVerifier(session=_Get()).verify_backing(
        ticker="AAPL", issuer_name="Dinari"
    )
    assert dinari_page.source == "not_available"
    assert dinari_page.error == "ConnectionError"

    dinari_docs = DinariVerifier(session=_Get()).verify_redemption(
        ticker="AAPL", issuer_name="Dinari"
    )
    assert dinari_docs.source == "not_available"
    assert dinari_docs.error == "ConnectionError"


def test_all_failed_band_label_and_name_list_sentence() -> None:
    report = TransparencyScorer(
        _failed_live_client(),
        verifiers={"backed": _BareTimeoutVerifier()},
        use_live_verifiers=True,
    ).score("NVDA")
    assert "heuristic" not in report["band_label"].lower()
    assert report["band_label"].startswith("ORANGE")
    assert "not available" in report["band_label"].lower()
    phrase = "Name-list scores are not used."
    assert "\n".join(report["notes"]).count(phrase) == 1
    for key in ("backing", "reserves", "redemption"):
        assert report["explanations"][key].count(phrase) == 1
        assert "name-list" in report["verification"][key]["evidence"].lower()


def test_failed_live_confidence_is_below_name_list_and_offline() -> None:
    from rwa_score.api.confidence import compute_confidence

    failed_one = compute_confidence(
        {"verification": {"backing": {"source": "not_available", "level": "not available"}}}
    )
    fallback_one = compute_confidence(
        {
            "verification": {
                "backing": {"source": "heuristic_fallback", "level": "self-reported"}
            }
        }
    )
    assert failed_one["score"] < fallback_one["score"]
    assert failed_one["score"] <= 0.2
    assert fallback_one["score"] == 0.3

    failed_report = TransparencyScorer(
        _failed_live_client(),
        verifiers={"backed": _BareTimeoutVerifier()},
        use_live_verifiers=True,
    ).score("NVDA")
    offline_report = TransparencyScorer(
        RecordingClient(), use_live_verifiers=False
    ).score("NVDA")
    failed_conf = compute_confidence(failed_report)
    offline_conf = compute_confidence(offline_report)
    assert failed_conf["score"] < offline_conf["score"]


def test_app_share_image_and_ask_rat_say_not_available(monkeypatch) -> None:
    import app as demo_app
    from PIL import ImageDraw

    from rwa_score.ask_rat import fallback_answer
    from rwa_score.score_card import render_score_card_png, share_caption, canonical_score_payload

    scorer = TransparencyScorer(
        _failed_live_client(),
        verifiers={"backed": _BareTimeoutVerifier()},
        use_live_verifiers=True,
    )
    report = scorer.score("NVDA")
    rows = {row["key"]: row for row in demo_app.pillar_evidence_rows(report)}
    for key in ("backing", "reserves", "redemption"):
        assert rows[key]["badge"] == "not available"
        assert demo_app.format_pillar_score(rows[key]["score"]) == "not available"

    payload = canonical_score_payload(report, "2026-10-24T00:00:00Z")
    assert "not available" in share_caption(payload, "abc123def4567890").lower()

    drawn: list[str] = []
    original = ImageDraw.ImageDraw.text

    def _capture(self, xy, text, *args, **kwargs):
        drawn.append(str(text))
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", _capture)
    png = render_score_card_png(
        report,
        timestamp="2026-10-24T00:00:00Z",
        fingerprint="abc123def4567890",
        signed=False,
    )
    assert png.startswith(b"\x89PNG")
    assert any("not available" in line for line in drawn)

    answer = fallback_answer("What is the NVDA score?", scorer)
    assert "not available" in answer.lower()
    assert "GREEN" not in answer
    self_reported = fallback_answer("Which NVDA pillars are self-reported?", scorer)
    assert "not available" in self_reported.lower()


def test_comparison_table_sorts_not_available_as_missing() -> None:
    import app as demo_app

    rows = [
        {"ticker": "LOW", "backing": 2},
        {"ticker": "HIGH", "backing": 100},
        {"ticker": "MID", "backing": 10},
        {"ticker": "MISS", "backing": "not available"},
        {"ticker": "NONE", "backing": None},
    ]
    ascending = [row["ticker"] for row in demo_app.sort_comparison_rows(rows, "backing")]
    assert ascending == ["LOW", "MID", "HIGH", "MISS", "NONE"]
    descending = [
        row["ticker"]
        for row in demo_app.sort_comparison_rows(rows, "backing", ascending=False)
    ]
    assert descending == ["HIGH", "MID", "LOW", "MISS", "NONE"]

    reports = [
        {
            "ticker": "LOW",
            "issuer": "A",
            "score": 2.0,
            "band": "RED",
            "subscores": {"backing": 2},
        },
        {
            "ticker": "HIGH",
            "issuer": "B",
            "score": 100.0,
            "band": "GREEN",
            "subscores": {"backing": 100},
        },
        {
            "ticker": "MISS",
            "issuer": "C",
            "score": 27.0,
            "band": "ORANGE",
            "subscores": {"backing": None},
        },
    ]
    frame = demo_app.comparison_table_frame(reports)
    assert str(frame["backing"].dtype).startswith("float")
    ordered = frame.sort_values("backing", ascending=True, na_position="last")
    assert list(ordered["ticker"]) == ["LOW", "HIGH", "MISS"]
    styled = demo_app.comparison_table(reports)
    html = styled.to_html()
    assert "not available" in html
    assert "not available" not in set(frame["backing"].dropna().astype(str))
