"""Health payload and Render live-mode blueprint checks (offline)."""

from __future__ import annotations

import json
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


def test_render_yaml_share_secrets_are_not_synced() -> None:
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    for key in (
        "SCORE_CARD_SIGNING_SECRET",
        "X_API_KEY",
        "X_API_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
    ):
        assert f"- key: {key}" in text
        idx = text.index(f"- key: {key}")
        following = text[idx : idx + 80]
        assert "sync: false" in following


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


def test_render_yaml_start_command_is_health_launcher_not_streamlit_run() -> None:
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "python -m rwa_score.health" in text
    assert "streamlit run" in text  # documented anti-pattern
    assert "startCommand:" in text
    start = text.split("startCommand:", 1)[1].split("envVars:", 1)[0]
    assert "python -m rwa_score.health" in start
    assert "streamlit run" not in start
    assert "dashboard" in text.lower()
    assert "Blueprint-synced" in text or "blueprint" in text.lower()


def test_readme_documents_dashboard_start_command_must_match() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Dashboard Start Command must match" in text
    assert "python -m rwa_score.health --server.port $PORT --server.address 0.0.0.0 --server.headless true" in text
    assert "streamlit run app.py" in text
    assert "/_stcore/health" in text


def test_application_init_patch_registers_health_ahead_of_spa() -> None:
    """install_health_route prepends /health when Tornado Application is built."""
    from tornado.testing import AsyncHTTPTestCase
    from tornado.web import Application, RequestHandler

    from rwa_score.health import install_health_route

    class SpaCatchAll(RequestHandler):
        def get(self, *_args):
            self.set_header("Content-Type", "text/html")
            self.write("<!doctype html>SPA")

    install_health_route()

    class _InitPatchHTTP(AsyncHTTPTestCase):
        def get_app(self) -> Application:
            return Application([(r"/(.*)", SpaCatchAll)])

        def test_health(self) -> None:
            resp = self.fetch("/health")
            assert resp.code == 200
            assert "application/json" in resp.headers["Content-Type"]
            body = json.loads(resp.body.decode())
            assert set(body) >= {"fixtures", "verifiers_live", "backed_feed"}
            assert b"SPA" not in resp.body

    suite = _InitPatchHTTP("test_health")
    result = suite.run()
    assert result.wasSuccessful(), f"{result.failures} {result.errors}"


def test_install_health_route_patches_streamlit_create_app() -> None:
    from streamlit.web.server.server import Server

    from rwa_score.health import install_health_route

    install_health_route()
    assert getattr(Server, "_rwa_health_patched", False) is True
    assert Server._create_app.__name__ == "_create_app"


def test_attach_registers_health_and_legal_on_streamlit_like_app() -> None:
    """Unit-level proof /health is registered ahead of Streamlit's SPA catch-all."""
    from tornado.testing import AsyncHTTPTestCase
    from tornado.web import Application, RequestHandler

    from rwa_score.health import _attach_custom_routes
    from rwa_score.legal import CONTACT_EMAIL, OPERATOR

    class SpaCatchAll(RequestHandler):
        def get(self, *_args):
            self.set_header("Content-Type", "text/html")
            self.write("<!doctype html><html><body>SPA</body></html>")

    class _HealthHTTP(AsyncHTTPTestCase):
        def get_app(self) -> Application:
            app = Application([(r"/(.*)", SpaCatchAll)])
            _attach_custom_routes(app)
            return app

        def test_health_json(self) -> None:
            resp = self.fetch("/health")
            assert resp.code == 200
            assert "application/json" in resp.headers["Content-Type"]
            body = json.loads(resp.body.decode())
            assert set(body) == {"fixtures", "verifiers_live", "backed_feed", "timestamp"}
            assert body["fixtures"] is False
            assert body["verifiers_live"] is True
            assert body["backed_feed"] in {"ok", "down"}
            assert b"SPA" not in resp.body

        def test_privacy_html(self) -> None:
            resp = self.fetch("/privacy")
            assert resp.code == 200
            assert "text/html" in resp.headers["Content-Type"]
            assert b"<!DOCTYPE html>" in resp.body
            assert CONTACT_EMAIL.encode() in resp.body
            assert OPERATOR.encode() in resp.body
            assert b"SPA" not in resp.body

        def test_terms_html(self) -> None:
            resp = self.fetch("/terms")
            assert resp.code == 200
            assert b"Terms of Service" in resp.body
            assert b"SPA" not in resp.body

        def test_unknown_still_spa(self) -> None:
            resp = self.fetch("/not-a-real-route")
            assert resp.code == 200
            assert b"SPA" in resp.body

    for name in ("test_health_json", "test_privacy_html", "test_terms_html", "test_unknown_still_spa"):
        suite = _HealthHTTP(name)
        result = suite.run()
        assert result.wasSuccessful(), f"{name} failed: {result.failures} {result.errors}"


def test_patched_create_app_registers_health_route() -> None:
    """Patched Server._create_app attaches /health on the returned Tornado app."""
    from tornado.web import Application, RequestHandler

    from rwa_score.health import _attach_custom_routes, install_health_route

    class SpaCatchAll(RequestHandler):
        def get(self, *_args):
            self.write("SPA")

    class _FakeServer:
        def _create_app(self):
            return Application([(r"/(.*)", SpaCatchAll)])

    install_health_route()
    original = _FakeServer._create_app

    def _create_app(self):
        app = original(self)
        _attach_custom_routes(app)
        return app

    _FakeServer._create_app = _create_app
    app = _FakeServer()._create_app()
    rules = app.default_router.rules
    assert rules, "Tornado app has no routes"
    # First-match-wins: our /health rule must be registered.
    from rwa_score.health import _health_handler_class

    handler_types: list[type] = []
    stack = list(rules)
    while stack:
        rule = stack.pop()
        target = getattr(rule, "target", None)
        if isinstance(target, type) and issubclass(target, RequestHandler):
            handler_types.append(target)
        nested = getattr(target, "rules", None)
        if nested:
            stack.extend(nested)
    names = {cls.__name__ for cls in handler_types}
    assert "HealthJSONHandler" in names or _health_handler_class().__name__ in names
    assert any("Privacy" in n or n == "LegalHTMLHandler" for n in names) or "PrivacyHTMLHandler" in names
