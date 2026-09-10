"""Webhook targets must be public HTTPS — no localhost / RFC1918 / metadata."""

from __future__ import annotations

import pytest

from rwa_score.api.webhooks import assert_public_https_url


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
