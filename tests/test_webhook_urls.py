"""Webhook targets must be public HTTPS — no localhost / RFC1918 / metadata."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from rwa_score.api.webhooks import assert_public_https_url, default_poster


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.example.com/rat",
        "https://example.test/hook",
    ],
)
def test_public_https_allowed(url: str) -> None:
    assert assert_public_https_url(url, resolve=False) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/rat",
        "https://localhost/hook",
        "https://LOCALHOST/hook",
        "https://app.localhost/hook",
        "https://127.0.0.1/hook",
        "https://127.0.0.1:8443/hook",
        "https://10.0.0.8/hook",
        "https://172.16.0.4/hook",
        "https://192.168.1.10/hook",
        "https://169.254.1.1/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/hook",
        "https://[fe80::1]/hook",
        "https://user:pass@hooks.example.com/hook",
        "ftp://hooks.example.com/hook",
    ],
)
def test_ssrf_shaped_urls_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        assert_public_https_url(url, resolve=False)


def test_rfc1918_edge_172_is_blocked_but_172_15_is_not() -> None:
    with pytest.raises(ValueError):
        assert_public_https_url("https://172.31.255.255/h", resolve=False)
    assert assert_public_https_url("https://172.15.0.1/h", resolve=False) == "https://172.15.0.1/h"


@pytest.mark.parametrize(
    "redirect_to",
    [
        "https://127.0.0.1/steal",
        "https://localhost/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.8/internal",
    ],
)
def test_default_poster_does_not_follow_redirect_to_private(
    monkeypatch: pytest.MonkeyPatch, redirect_to: str
) -> None:
    seen: list[dict[str, Any]] = []

    class _Resp:
        def __init__(self) -> None:
            self.status_code = 302
            self.headers = {"Location": redirect_to}

    def fake_post(url: str, **kwargs: Any) -> _Resp:
        seen.append({"url": url, "allow_redirects": kwargs.get("allow_redirects")})
        blocked = ("127.0.0.1", "localhost", "169.254.169.254", "10.0.0.8", "metadata")
        if any(part in url for part in blocked):
            raise AssertionError(f"SSRF: posted to blocked host {url}")
        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(
        "rwa_score.api.webhooks.assert_public_https_url",
        lambda url, **_kw: url,
    )

    code, ok = default_poster(
        "https://hooks.example.com/rat",
        "{}",
        {"Content-Type": "application/json"},
    )
    assert code == 302
    assert ok is False
    assert seen == [{"url": "https://hooks.example.com/rat", "allow_redirects": False}]


def test_default_poster_records_2xx_without_following(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(url: str, **kwargs: Any) -> Any:
        assert url == "https://hooks.example.com/rat"
        assert kwargs.get("allow_redirects") is False

        class _Resp:
            status_code = 204

        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(
        "rwa_score.api.webhooks.assert_public_https_url",
        lambda url, **_kw: url,
    )
    code, ok = default_poster("https://hooks.example.com/rat", "{}", {})
    assert code == 204
    assert ok is True
