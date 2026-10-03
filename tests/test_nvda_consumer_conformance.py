"""Contract checks for the published, simulation-only NVDA consumer example."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from scripts.nvda_consumer_conformance import (
    FIXTURE_COMMIT,
    FIXTURE_SHA256,
    build_simulation,
    read_pinned_fixture,
    render_simulation,
)


GOLDEN = Path(__file__).resolve().parents[1] / "docs/examples/nvda_consumer_conformance.simulation.json"


def test_golden_is_byte_exact_and_pinned() -> None:
    output = build_simulation(read_pinned_fixture())
    assert render_simulation() == GOLDEN.read_text(encoding="utf-8")
    assert output["fixture"]["commit"] == FIXTURE_COMMIT
    assert output["fixture"]["sha256"] == FIXTURE_SHA256
    assert output["simulationOnly"] is True


def test_preserves_rat_labels_and_full_pillar_verification() -> None:
    fixture = read_pinned_fixture()
    output = build_simulation(fixture)
    signals = output["ratSignals"]
    assert signals["score"] == fixture["score"]
    assert signals["band"] == fixture["band"]
    assert signals["bandLabel"] == fixture["band_label"]
    assert signals["dataSource"] == "fixture"
    assert signals["verificationMode"] == "offline_heuristic"
    assert signals["liveVerifiers"] is False
    assert signals["confidence"] == fixture["confidence"]
    assert signals["verification"] == fixture["verification"]
    assert signals["pillars"] == fixture["pillars"]
    assert signals["explanations"] == fixture["explanations"]
    assert signals["notes"] == fixture["notes"]
    assert signals["heuristics"] == fixture["heuristics"]
    assert signals["cmcCalls"] == fixture["cmc_calls"]
    assert signals["basis"] == fixture["basis"]
    assert signals["attestation"] == fixture["attestation"]


def test_ticker_and_cross_issuer_prices_cannot_bind_or_authorize() -> None:
    output = build_simulation(read_pinned_fixture())
    assert output["candidateInstrument"]["exactInstrumentId"] is None
    assert output["candidateInstrument"]["reasonCode"] == "TICKER_IS_CANDIDATE_CONTEXT_ONLY"
    observations = output["wrapperPriceObservations"]
    assert {(item["issuer"], item["symbol"]) for item in observations} == {
        ("Backed Finance", "NVDAx"), ("Ondo", "NVDAon"), ("xStocks", "NVDAxst")
    }
    assert all(item["exactInstrumentId"] is None for item in observations)
    assert all(item["originalObservationTime"] is None for item in observations)
    assert all(item["freshnessStatus"] == "UNKNOWN" for item in observations)
    assert all(item["countsAsSameInstrumentIndependentQuote"] is False for item in observations)
    assert output["sameInstrumentIndependentQuoteCount"] == 0
    assert output["mayAuthorizeExecution"] is False
    assert "verdict" not in output and "signature" not in output


def test_rejects_changed_pin_and_promotion_of_heuristic_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = read_pinned_fixture()
    changed = copy.deepcopy(fixture)
    changed["verification"]["reserves"]["level"] = "authenticated"
    with pytest.raises(ValueError, match="verification level"):
        build_simulation(changed)
    changed = copy.deepcopy(fixture)
    changed["price"]["tokens"][0]["observedAt"] = "2026-09-21T00:00:00Z"
    with pytest.raises(ValueError, match="observation time"):
        build_simulation(changed)
    monkeypatch.setattr("scripts.nvda_consumer_conformance.FIXTURE_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="SHA-256"):
        read_pinned_fixture()
