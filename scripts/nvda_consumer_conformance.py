"""Offline, simulation-only consumer reading of the pinned RAT NVDA fixture.

This is a transparency-label conformance example, not an action evaluator.
It reads one local JSON file and never imports Insight or a RAT live client.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


FIXTURE_COMMIT = "0e832c24a405e17cfc567ebe51cefd9046c5e10d"
FIXTURE_SHA256 = "76316c0d6b933a111d32e0cca9c50d9c02851da61c289a80701151370b46903a"
FIXTURE_RELATIVE_PATH = "docs/examples/v1_score_NVDA.fixture.json"
ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / FIXTURE_RELATIVE_PATH


def _record(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _fixture_value(fixture: dict[str, Any], name: str, expected: Any) -> None:
    if fixture.get(name) != expected or type(fixture.get(name)) is not type(expected):
        raise ValueError(f"unexpected pinned fixture {name}")


def read_pinned_fixture() -> dict[str, Any]:
    raw = FIXTURE_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != FIXTURE_SHA256:
        raise ValueError("NVDA fixture SHA-256 differs from the accepted pin")
    return _record(json.loads(raw), "fixture")


def build_simulation(fixture: dict[str, Any]) -> dict[str, Any]:
    """Preserve RAT labels while refusing to manufacture consumer policy inputs."""
    _fixture_value(fixture, "ticker", "NVDA")
    _fixture_value(fixture, "issuer", "Backed Finance")
    _fixture_value(fixture, "data_source", "fixture")
    _fixture_value(fixture, "verification_mode", "offline_heuristic")
    _fixture_value(fixture, "live_verifiers", False)
    _fixture_value(fixture, "score", 90.4)
    _fixture_value(fixture, "band", "GREEN")
    _record(fixture.get("confidence"), "confidence")
    verification = _record(fixture.get("verification"), "verification")
    for pillar in ("backing", "reserves", "redemption", "price", "disclosure", "basis"):
        result = _record(verification.get(pillar), f"verification.{pillar}")
        if result.get("level") != "self-reported":
            raise ValueError(f"unexpected pinned verification level: {pillar}")
    for pillar in ("backing", "reserves", "redemption"):
        if verification[pillar].get("source") != "heuristic_fallback":
            raise ValueError(f"unexpected pinned verification source: {pillar}")

    price = _record(fixture.get("price"), "price")
    basis = _record(fixture.get("basis"), "basis")
    cmc_calls = _record(fixture.get("cmc_calls"), "cmc_calls")
    if cmc_calls.get("source") != "fixture" or cmc_calls.get("live") is not False:
        raise ValueError("unexpected pinned CMC call provenance")
    tokens = price.get("tokens")
    wrappers = basis.get("wrappers")
    if not isinstance(tokens, list) or not isinstance(wrappers, list) or len(tokens) != 3:
        raise ValueError("expected three published NVDA issuer wrappers")
    if [(t.get("issuer"), t.get("symbol")) for t in tokens if isinstance(t, dict)] != [
        ("Backed Finance", "NVDAx"), ("Ondo", "NVDAon"), ("xStocks", "NVDAxst")
    ]:
        raise ValueError("unexpected pinned NVDA wrapper identities")
    if [(w.get("issuer"), w.get("symbol")) for w in wrappers if isinstance(w, dict)] != [
        (t["issuer"], t["symbol"]) for t in tokens
    ]:
        raise ValueError("price and basis wrapper identities differ")

    observations = []
    for token in tokens:
        if not isinstance(token.get("price"), (int, float)) or isinstance(token["price"], bool):
            raise ValueError("wrapper price must be numeric")
        if any(key in token for key in ("observed_at", "observedAt", "timestamp", "time")):
            raise ValueError("pinned wrapper unexpectedly contains source observation time")
        observations.append({
            "issuer": token["issuer"],
            "symbol": token["symbol"],
            "cryptoId": token["crypto_id"],
            "price": token["price"],
            "source": token["source"],
            "originalObservationTime": None,
            "freshnessStatus": "UNKNOWN",
            "exactInstrumentId": None,
            "countsAsSameInstrumentIndependentQuote": False,
            "exclusionReasons": ["EXACT_INSTRUMENT_UNBOUND", "SOURCE_OBSERVATION_TIME_UNAVAILABLE"],
        })

    return {
        "schema": "rat.nvda-consumer-conformance.simulation.v1",
        "simulationOnly": True,
        "fixture": {
            "path": FIXTURE_RELATIVE_PATH,
            "commit": FIXTURE_COMMIT,
            "sha256": FIXTURE_SHA256,
        },
        "ratSignals": {
            "score": fixture["score"],
            "band": fixture["band"],
            "bandLabel": fixture["band_label"],
            "subscores": fixture["subscores"],
            "pillars": fixture["pillars"],
            "explanations": fixture["explanations"],
            "dataSource": fixture["data_source"],
            "verificationMode": fixture["verification_mode"],
            "liveVerifiers": fixture["live_verifiers"],
            "confidence": fixture["confidence"],
            "verification": verification,
            "notes": fixture["notes"],
            "heuristics": fixture["heuristics"],
            "cmcCalls": cmc_calls,
            "basis": basis,
            "attestation": fixture["attestation"],
        },
        "candidateInstrument": {
            "ticker": fixture["ticker"],
            "rwaId": fixture["rwa_id"],
            "issuer": fixture["issuer"],
            "exactInstrumentId": None,
            "reasonCode": "TICKER_IS_CANDIDATE_CONTEXT_ONLY",
        },
        "wrapperPriceObservations": observations,
        "sameInstrumentIndependentQuoteCount": 0,
        "excludedEvidence": [
            {"kind": "backing", "reasonCode": "SELF_REPORTED_HEURISTIC_NOT_AUTHENTICATED"},
            {"kind": "reserves", "reasonCode": "SELF_REPORTED_HEURISTIC_NOT_AUTHENTICATED"},
            {"kind": "redemption", "reasonCode": "SELF_REPORTED_HEURISTIC_NOT_AUTHENTICATED"},
            {"kind": "eligibility", "reasonCode": "ELIGIBILITY_NOT_SUPPLIED"},
        ],
        "requiredConsumerInputs": [
            "exactInstrumentBinding", "exactActionAndActor", "consumerPolicy",
            "instrumentBoundPriceObservationsWithSourceTime", "currentMarketState",
            "authenticatedBackingReserveRedemptionAndEligibilityAsPolicyRequires",
        ],
        "mayAuthorizeExecution": False,
    }


def render_simulation() -> str:
    return json.dumps(build_simulation(read_pinned_fixture()), ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    print(render_simulation(), end="")
