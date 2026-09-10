"""Health payload and Render live-mode blueprint checks (offline)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from rwa_score.health import build_health_payload
from rwa_score.verifiers import BACKED_POR_URL

ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_render_yaml_fixtures_flag_is_live() -> None:
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "- key: RWA_USE_FIXTURES":
            following = [ln.strip() for ln in lines[index + 1 :] if ln.strip()]
            assert following, "RWA_USE_FIXTURES is missing a value"
            assert following[0] == 'value: "0"'
            return
    raise AssertionError("RWA_USE_FIXTURES not found in render.yaml")


def test_health_payload_fixtures_false_when_env_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RWA_USE_FIXTURES", "0")
    called: list[str] = []

    def fake_get(url: str, timeout: float | None = None, **_kwargs):
        called.append(url)
        assert "proof-of-reserves" in url
        assert url == BACKED_POR_URL.format(symbol="AAPLx")
        assert timeout is not None and timeout <= 3
        return FakeResponse(200)

    frozen = datetime(2026, 9, 10, 1, 13, 0, tzinfo=timezone.utc)
    payload = build_health_payload(getter=fake_get, now=frozen)
    assert payload["fixtures"] is False
    assert payload["verifiers_live"] is True
    assert payload["backed_feed"] == "ok"
    assert payload["timestamp"] == "2026-09-10T01:13:00Z"
    assert called


def test_health_payload_backed_feed_down_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RWA_USE_FIXTURES", "0")

    def boom(_url: str, timeout: float | None = None, **_kwargs):
        raise OSError("offline")

    payload = build_health_payload(getter=boom)
    assert payload["fixtures"] is False
    assert payload["verifiers_live"] is True
    assert payload["backed_feed"] == "down"
    datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
