"""Unit tests for the xAI explainer (mocked HTTP)."""

from __future__ import annotations

from typing import Any

import pytest

from rwa_score.explainer import (
    AI_FOOTNOTE,
    XAI_CHAT_URL,
    XAI_MAX_TOKENS,
    XAI_MODEL,
    build_explain_prompt,
    explain_score,
    fallback_explanation,
)

ROBINHOOD_RESULT: dict[str, Any] = {
    "ticker": "AAPL",
    "issuer": "Robinhood",
    "score": 47.2,
    "band": "ORANGE",
    "issuer_note": (
        "Debt wrapper: holders are creditors of Robinhood Assets Jersey, "
        "not shareholders of the listed company. Self-reported 1:1; no public PoR."
    ),
    "subscores": {
        "backing": 55.0,
        "reserves": 40.0,
        "redemption": 35.0,
        "price": 80.0,
        "disclosure": 20.0,
    },
    "explanations": {
        "backing": "Live backing check for 'Robinhood'.",
        "reserves": "Live reserves check for 'Robinhood'.",
        "redemption": "Debt wrapper redemption.",
        "price": "24h change +1.00%.",
        "disclosure": "No SEC CIK on the RWA info record.",
    },
    "verification": {
        "backing": {
            "level": "self-reported",
            "evidence": "Robinhood 1:1 claim, no public PoR",
        },
        "reserves": {
            "level": "self-reported",
            "evidence": "no independent attestation",
        },
        "redemption": {
            "level": "self-reported",
            "evidence": "debt wrapper, creditor claim only",
        },
        "price": {"level": "self-reported", "evidence": "CMC quote"},
        "disclosure": {"level": "self-reported", "evidence": "No SEC CIK"},
    },
    "flags": ["No independent on-chain proof of reserves found (heuristic)."],
    "notes": ["Issuer backing / proof-of-reserves / redemption flags are name-matching heuristics."],
}


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return dict(self._payload)


class FakeSession:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, headers: dict | None = None, json: dict | None = None, timeout: float | None = None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def test_build_explain_prompt_includes_issuer_note_and_evidence() -> None:
    prompt = build_explain_prompt(ROBINHOOD_RESULT)
    assert "Robinhood Assets Jersey" in prompt
    assert "Robinhood 1:1 claim, no public PoR" in prompt
    assert "no independent attestation" in prompt
    assert "debt wrapper, creditor claim only" in prompt
    assert "ORANGE" in prompt
    assert "47.2" in prompt


def test_explain_score_posts_prompt_to_xai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "test-key-not-secret")
    session = FakeSession(
        FakeResponse(
            200,
            {"choices": [{"message": {"content": "Debt wrapper, so the score stays orange."}}]},
        )
    )
    text = explain_score(ROBINHOOD_RESULT, session=session)
    assert text == "Debt wrapper, so the score stays orange."
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == XAI_CHAT_URL
    assert call["json"]["model"] == XAI_MODEL
    assert call["json"]["max_tokens"] == XAI_MAX_TOKENS
    user_msg = call["json"]["messages"][1]["content"]
    assert "Robinhood Assets Jersey" in user_msg
    assert "Robinhood 1:1 claim, no public PoR" in user_msg
    assert call["headers"]["Authorization"] == "Bearer test-key-not-secret"


def test_explain_score_fallback_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    text = explain_score(ROBINHOOD_RESULT)
    assert "ORANGE" in text
    assert "47.2" in text
    assert "Robinhood Assets Jersey" in text
    assert "Robinhood 1:1 claim, no public PoR" in text
    assert "not financial advice" in text.lower()


def test_explain_score_fallback_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "test-key-not-secret")
    session = FakeSession(FakeResponse(500, {"error": "boom"}))
    text = explain_score(ROBINHOOD_RESULT, session=session)
    assert text == fallback_explanation(ROBINHOOD_RESULT)


def test_explain_score_fallback_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "test-key-not-secret")
    session = FakeSession(RuntimeError("timeout"))
    text = explain_score(ROBINHOOD_RESULT, session=session)
    assert "ORANGE" in text
    assert "not financial advice" in text.lower()


def test_ai_footnote_wording() -> None:
    assert AI_FOOTNOTE == "generated by AI, not financial advice"
