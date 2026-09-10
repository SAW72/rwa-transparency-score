"""Unit tests for attestation / PoR verifiers (mocked HTTP)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from rwa_score.verifiers import (
    BACKED_POR_URL,
    DINARI_DSHARES_URL,
    BackedVerifier,
    DinariVerifier,
    VerificationLevel,
    por_score_from_ratio,
    resolve_verifier_id,
)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict[str, Any] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("no json")
        return dict(self._payload)


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse] | list[FakeResponse]) -> None:
        if isinstance(responses, list):
            self._queue = list(responses)
            self._by_url: dict[str, FakeResponse] = {}
        else:
            self._queue = []
            self._by_url = dict(responses)
        self.calls: list[str] = []

    def get(self, url: str, timeout: float | None = None, headers: dict | None = None):
        self.calls.append(url)
        if self._by_url:
            if url in self._by_url:
                return self._by_url[url]
            # Case-sensitive exact first, then case-insensitive path match.
            lower = url.lower()
            for key, resp in self._by_url.items():
                if lower == key.lower() or lower.endswith(key.lower().rsplit("/", 1)[-1]):
                    # Prefer exact case when multiple keys share a suffix.
                    if url.rsplit("/", 1)[-1] == key.rsplit("/", 1)[-1]:
                        return resp
            for key, resp in self._by_url.items():
                if lower == key.lower() or url.rsplit("/", 1)[-1].lower() == key.rsplit("/", 1)[-1].lower():
                    return resp
            raise AssertionError(f"unexpected URL {url}")
        if not self._queue:
            raise AssertionError(f"no queued response for {url}")
        return self._queue.pop(0)


@pytest.mark.parametrize(
    "ratio,expected",
    [(1.0, 95.0), (0.999, 95.0), (0.995, 80.0), (0.99, 80.0), (0.96, 60.0), (0.95, 60.0), (0.94, 30.0)],
)
def test_por_score_thresholds(ratio: float, expected: float) -> None:
    assert por_score_from_ratio(ratio) == expected


def test_resolve_verifier_keywords() -> None:
    assert resolve_verifier_id("Backed Finance") == "backed"
    assert resolve_verifier_id("xStocks") == "backed"
    assert resolve_verifier_id("Dinari Securities") == "dinari"
    assert resolve_verifier_id("NoteVault Demo Issuer") is None


def test_backed_verifier_high_ratio() -> None:
    payload = {
        "symbol": "NVDAx",
        "sharesHeld": "1000",
        "circulatingSupply": "1000",
        "holdings": [{"provider": "Alpaca", "quantity": "1000", "symbol": "NVDA"}],
    }
    # ticker NVDA -> tries NVDA, nvda, then NVDAx (API is case-sensitive)
    session = FakeSession(
        {
            BACKED_POR_URL.format(symbol="NVDA"): FakeResponse(404, text="missing"),
            BACKED_POR_URL.format(symbol="NVDAx"): FakeResponse(200, payload),
        }
    )
    v = BackedVerifier(session=session, cache_ttl=3600)
    result = v.verify_reserves(ticker="NVDA", issuer_name="Backed Finance")
    assert result.ok is True
    assert result.level == VerificationLevel.ON_CHAIN_POR
    assert result.score == 95.0
    assert "collateralization_ratio" in result.evidence
    assert result.meta["collateralization_ratio"] == pytest.approx(1.0)


def test_backed_verifier_fallback_on_http_error() -> None:
    session = FakeSession(
        {
            BACKED_POR_URL.format(symbol="TSLA"): FakeResponse(500, text="boom"),
            BACKED_POR_URL.format(symbol="TSLAx"): FakeResponse(500, text="boom"),
        }
    )
    v = BackedVerifier(session=session)
    result = v.verify_reserves(ticker="TSLA", issuer_name="Backed Finance")
    assert result.level == VerificationLevel.SELF_REPORTED
    assert result.source == "heuristic_fallback"
    assert any("heuristic fallback" in n for n in result.notes)
    assert result.error
    assert result.score == 90.0  # Backed Finance is on AUDITED list


def test_backed_verifier_caches_for_one_hour() -> None:
    payload = {
        "symbol": "AAPLx",
        "sharesHeld": "100",
        "circulatingSupply": "100",
        "holdings": [],
    }
    url = BACKED_POR_URL.format(symbol="AAPLx")
    session = FakeSession({url: FakeResponse(200, payload)})
    v = BackedVerifier(session=session, cache_ttl=3600)
    first = v.verify_reserves(ticker="AAPLx", issuer_name="xStocks")
    second = v.verify_reserves(ticker="AAPLx", issuer_name="xStocks")
    assert first.score == second.score == 95.0
    assert session.calls == [url]


def test_dinari_verifier_attestation_pending() -> None:
    html = """
    <html><body>
    <p>dShares are backed 1:1 by the underlying securities.</p>
    <p>Custody lives at Alpaca Securities LLC.</p>
    <p>Reserve audits are performed by an independent Big 4 accounting firm.</p>
    </body></html>
    """
    session = FakeSession({DINARI_DSHARES_URL: FakeResponse(200, text=html)})
    v = DinariVerifier(session=session)
    result = v.verify_reserves(ticker="AAPL", issuer_name="Dinari")
    assert result.ok is True
    assert result.level == VerificationLevel.ATTESTED
    assert result.score == 85.0
    assert any("attestation pending" in n for n in result.notes)
    assert "Alpaca" in result.evidence


def test_dinari_verifier_fallback_when_incomplete() -> None:
    html = "<html><body><p>Welcome to Dinari.</p></body></html>"
    session = FakeSession({DINARI_DSHARES_URL: FakeResponse(200, text=html)})
    v = DinariVerifier(session=session)
    result = v.verify_backing(ticker="AAPL", issuer_name="Dinari")
    assert result.source == "heuristic_fallback"
    assert any("heuristic fallback" in n for n in result.notes)
    assert result.error


def test_resolve_rejects_adversarial_backed_names() -> None:
    from rwa_score.verifiers import resolve_verifier_id

    assert resolve_verifier_id("Not Backed At All") is None
    assert resolve_verifier_id("UNBACKED Holdings") is None
    assert resolve_verifier_id("Anti-Backed Finance") is None
    assert resolve_verifier_id("feedbacked") is None
    assert resolve_verifier_id("Backed Finance") == "backed"
    assert resolve_verifier_id("xStocks") == "backed"
    assert resolve_verifier_id("Dinari") == "dinari"
