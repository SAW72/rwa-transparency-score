"""Health payload and Render live-mode blueprint checks (offline)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from rwa_score.chainlink_por import (
    HEALTH_POR_FEED,
    LATEST_ROUND_DATA_SELECTOR,
    eth_call_payload,
)
from rwa_score.health import build_health_payload
from tests.test_verifiers import FakeResponse, encode_latest_round

ROOT = Path(__file__).resolve().parents[1]


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
    monkeypatch.setenv("POLYGON_RPC_URL", "https://rpc.example.test")
    called: list[str] = []

    def fake_post(url: str, json=None, timeout: float | None = None, **_kwargs):
        called.append(url)
        assert url == "https://rpc.example.test"
        assert timeout is not None and timeout <= 3
        payload = json or {}
        assert payload.get("method") == "eth_call"
        params = (payload.get("params") or [{}])[0]
        assert params.get("to") == HEALTH_POR_FEED.proxy
        assert params.get("data") == LATEST_ROUND_DATA_SELECTOR
        hex_result = encode_latest_round(answer=100 * 10**HEALTH_POR_FEED.decimals)
        return FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "result": hex_result})

    frozen = datetime(2026, 9, 10, 1, 13, 0, tzinfo=timezone.utc)
    payload = build_health_payload(poster=fake_post, now=frozen)
    assert payload["fixtures"] is False
    assert payload["verifiers_live"] is True
    assert payload["backed_feed"] == "ok"
    assert payload["timestamp"] == "2026-09-10T01:13:00Z"
    assert set(payload) == {"fixtures", "verifiers_live", "backed_feed", "timestamp"}
    assert called


def test_health_payload_backed_feed_down_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RWA_USE_FIXTURES", "0")
    monkeypatch.setenv("POLYGON_RPC_URL", "https://rpc.example.test")

    def boom(_url: str, json=None, timeout: float | None = None, **_kwargs):
        raise OSError("offline")

    payload = build_health_payload(poster=boom)
    assert payload["fixtures"] is False
    assert payload["verifiers_live"] is True
    assert payload["backed_feed"] == "down"
    datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))


def test_eth_call_payload_targets_latest_round() -> None:
    body = eth_call_payload(HEALTH_POR_FEED.proxy, LATEST_ROUND_DATA_SELECTOR)
    assert body["method"] == "eth_call"
    assert body["params"][0]["to"] == HEALTH_POR_FEED.proxy
    assert body["params"][1] == "latest"
