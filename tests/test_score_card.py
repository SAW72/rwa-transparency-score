"""Score-card PNG + HMAC tests. No network."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from rwa_score.score_card import (
    CARD_HEIGHT,
    CARD_WIDTH,
    SIGNING_SECRET_ENV,
    UNSIGNED_FINGERPRINT,
    build_signed_card,
    canonical_json,
    canonical_score_payload,
    share_caption,
    share_score_card,
    signature_fingerprint,
    verify_signature,
)
from rwa_score.scorer import PILLARS, WEIGHTS, TransparencyScorer

SAMPLE_REPORT: dict[str, Any] = {
    "ticker": "NVDA",
    "issuer": "Backed Finance",
    "score": 88.6,
    "band": "GREEN",
    "band_label": "GREEN — heuristic: stronger transparency signals (still verify)",
    "subscores": {
        "backing": 95.0,
        "reserves": 90.0,
        "redemption": 88.0,
        "price": 80.0,
        "disclosure": 80.0,
        "basis": 92.0,
    },
}

FIXED_TS = "2026-09-10T15:02:00Z"
TEST_SECRET = "test-signing-secret-not-for-prod"


class FakeXResult:
    def __init__(self, *, posted: bool, message: str, url: str | None = None) -> None:
        self.posted = posted
        self.message = message
        self.url = url


class RecordingXClient:
    def __init__(self, result: FakeXResult | Exception) -> None:
        self._result = result
        self.calls: list[tuple[bytes, str]] = []

    def post_image(self, png_bytes: bytes, text: str) -> FakeXResult:
        self.calls.append((png_bytes, text))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_weights_include_six_pillars_with_basis() -> None:
    assert list(WEIGHTS) == [
        "backing",
        "reserves",
        "redemption",
        "price",
        "disclosure",
        "basis",
    ]
    assert "basis" in PILLARS
    assert "basis" in PILLARS["basis"]["label"].lower() or "basis" in PILLARS["basis"]["what"].lower()


def test_canonical_payload_is_stable_and_sorted() -> None:
    payload = canonical_score_payload(SAMPLE_REPORT, FIXED_TS)
    assert list(payload["subscores"]) == list(WEIGHTS)
    assert payload == {
        "band": "GREEN",
        "score": 88.6,
        "subscores": {
            "backing": 95.0,
            "reserves": 90.0,
            "redemption": 88.0,
            "price": 80.0,
            "disclosure": 80.0,
            "basis": 92.0,
        },
        "ticker": "NVDA",
        "timestamp": FIXED_TS,
    }
    dumped = canonical_json(payload)
    assert dumped == (
        '{"band":"GREEN","score":88.6,"subscores":{"backing":95.0,"basis":92.0,'
        '"disclosure":80.0,"price":80.0,"redemption":88.0,"reserves":90.0},'
        f'"ticker":"NVDA","timestamp":"{FIXED_TS}"}}'
    )
    assert dumped == canonical_json(canonical_score_payload(SAMPLE_REPORT, FIXED_TS))


def test_hmac_roundtrip_and_tamper_detect() -> None:
    card = build_signed_card(SAMPLE_REPORT, secret=TEST_SECRET, timestamp=FIXED_TS)
    assert card.signed is True
    assert card.signature
    assert card.fingerprint == signature_fingerprint(card.signature)
    assert len(card.fingerprint) == 16
    assert verify_signature(card.canonical, card.signature, TEST_SECRET)
    assert not verify_signature(card.canonical, card.signature, "wrong-secret")
    tampered = card.canonical.replace("88.6", "12.0")
    assert not verify_signature(tampered, card.signature, TEST_SECRET)
    assert not verify_signature(card.canonical, "0" * 64, TEST_SECRET)


def test_missing_secret_is_unsigned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SIGNING_SECRET_ENV, raising=False)
    card = build_signed_card(SAMPLE_REPORT, timestamp=FIXED_TS)
    assert card.signed is False
    assert card.signature == ""
    assert card.fingerprint == UNSIGNED_FINGERPRINT
    assert not verify_signature(card.canonical, card.signature, "")


def test_png_is_valid_and_branded() -> None:
    from PIL import Image

    card = build_signed_card(SAMPLE_REPORT, secret=TEST_SECRET, timestamp=FIXED_TS)
    assert card.png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    image = Image.open(io.BytesIO(card.png_bytes))
    assert image.format == "PNG"
    assert image.size == (CARD_WIDTH, CARD_HEIGHT)
    pixels = image.convert("RGB").getdata()
    mint = (61, 220, 151)
    assert any(px == mint for px in pixels)
    assert card.filename.startswith("rat-score-NVDA-")
    assert card.filename.endswith(".png")
    assert FIXED_TS in card.caption
    assert card.fingerprint in card.caption
    assert "Not financial advice" in card.caption
    assert "NVDA" in card.caption
    assert len(card.caption) <= 280


def test_share_caption_mentions_pillars() -> None:
    payload = canonical_score_payload(SAMPLE_REPORT, FIXED_TS)
    text = share_caption(payload, "abc123def4567890")
    assert "Backing 95" in text
    assert "Cross-issuer 92" in text
    assert "sig abc123def4567890" in text
    assert "Not financial advice" in text


def test_fixture_report_builds_card(fixture_scorer: TransparencyScorer) -> None:
    report = fixture_scorer.score("NVDA")
    card = build_signed_card(
        report,
        secret=TEST_SECRET,
        now=datetime(2026, 9, 10, 15, 2, 0, tzinfo=timezone.utc),
    )
    assert card.payload["ticker"] == "NVDA"
    assert "basis" in card.payload["subscores"]
    assert list(card.payload["subscores"]) == list(WEIGHTS)
    assert card.signed
    assert verify_signature(card.canonical, card.signature, TEST_SECRET)
    from PIL import Image

    image = Image.open(io.BytesIO(card.png_bytes))
    assert image.size == (CARD_WIDTH, CARD_HEIGHT)


def test_share_skips_x_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X_API_KEY", raising=False)
    monkeypatch.delenv("X_API_SECRET", raising=False)
    monkeypatch.delenv("X_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("X_ACCESS_TOKEN_SECRET", raising=False)
    monkeypatch.setenv(SIGNING_SECRET_ENV, TEST_SECRET)
    card = share_score_card(SAMPLE_REPORT, timestamp=FIXED_TS)
    assert card.png_bytes.startswith(b"\x89PNG")
    assert card.signed
    assert card.x_posted is False
    assert "X post skipped" in card.x_message


def test_share_uses_injected_x_client() -> None:
    client = RecordingXClient(
        FakeXResult(posted=True, message="Posted to X: https://x.com/i/web/status/1", url="https://x.com/i/web/status/1")
    )
    card = share_score_card(
        SAMPLE_REPORT,
        secret=TEST_SECRET,
        timestamp=FIXED_TS,
        x_client=client,
    )
    assert card.x_posted is True
    assert card.x_url == "https://x.com/i/web/status/1"
    assert len(client.calls) == 1
    png, caption = client.calls[0]
    assert png == card.png_bytes
    assert card.fingerprint in caption


def test_share_survives_x_client_raise() -> None:
    client = RecordingXClient(RuntimeError("timeout"))
    card = share_score_card(
        SAMPLE_REPORT,
        secret=TEST_SECRET,
        timestamp=FIXED_TS,
        x_client=client,
    )
    assert card.png_bytes
    assert card.x_posted is False
    assert "timeout" in card.x_message


def test_share_disabled_does_not_call_x() -> None:
    client = RecordingXClient(FakeXResult(posted=True, message="should not run"))
    card = share_score_card(
        SAMPLE_REPORT,
        secret=TEST_SECRET,
        timestamp=FIXED_TS,
        post_to_x=False,
        x_client=client,
    )
    assert client.calls == []
    assert card.x_posted is False
    assert "skipped" in card.x_message.lower()


def test_readme_documents_share_and_secrets() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
    assert "Share score card" in text
    assert "SCORE_CARD_SIGNING_SECRET" in text
    assert "X_API_KEY" in text
    assert "X_API_SECRET" in text
    assert "X_ACCESS_TOKEN" in text
    assert "X_ACCESS_TOKEN_SECRET" in text
    assert "Bitwarden" in text
    assert "HMAC-SHA256" in text
    assert "does **not** run on page load" in text
    assert "Not financial advice" in text
    assert "MIT" in text
    assert "not a hunter or auto-poster" in text
