"""X API v2 client tests — OAuth signer + mocked HTTP. No network."""

from __future__ import annotations

from typing import Any

import pytest

from rwa_score.x_client import (
    MEDIA_UPLOAD_URL,
    MISSING_CREDS_MESSAGE,
    TWEET_URL,
    XClient,
    XCredentials,
    oauth1_authorization_header,
    oauth1_sign,
    oauth1_signature_base,
    percent_encode,
    x_credentials_ready,
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
        self.text = text or ("" if payload is None else str(payload))

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("no json")
        return dict(self._payload)


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if not self._responses:
            raise AssertionError(f"unexpected POST {url}")
        return self._responses.pop(0)


# Official Twitter OAuth 1.0a signature example
# https://developer.x.com/en/docs/authentication/oauth-1-0a/creating-a-signature
TWITTER_EXAMPLE_METHOD = "POST"
TWITTER_EXAMPLE_URL = "https://api.twitter.com/1.1/statuses/update.json"
TWITTER_EXAMPLE_CONSUMER_SECRET = "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw"
TWITTER_EXAMPLE_TOKEN_SECRET = "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE"
TWITTER_EXAMPLE_PARAMS = {
    "include_entities": "true",
    "oauth_consumer_key": "xvz1evFS4wEEPTGEFPHBog",
    "oauth_nonce": "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
    "oauth_signature_method": "HMAC-SHA1",
    "oauth_timestamp": "1318622958",
    "oauth_token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
    "oauth_version": "1.0",
    "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
}
TWITTER_EXAMPLE_BASE = (
    "POST&https%3A%2F%2Fapi.twitter.com%2F1.1%2Fstatuses%2Fupdate.json&"
    "include_entities%3Dtrue%26oauth_consumer_key%3Dxvz1evFS4wEEPTGEFPHBog%26"
    "oauth_nonce%3DkYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg%26"
    "oauth_signature_method%3DHMAC-SHA1%26oauth_timestamp%3D1318622958%26"
    "oauth_token%3D370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb%26"
    "oauth_version%3D1.0%26"
    "status%3DHello%2520Ladies%2520%252B%2520Gentlemen%252C%2520a%2520signed%2520OAuth%2520request%2521"
)
# HMAC-SHA1 of the documented base string + signing key (verified with OpenSSL).
TWITTER_EXAMPLE_SIGNATURE = "hCtSmYh+iHYCEqBWrE7C7hYmtUk="


def test_percent_encode_oauth_rules() -> None:
    assert percent_encode("Hello Ladies + Gentlemen, a signed OAuth request!") == (
        "Hello%20Ladies%20%2B%20Gentlemen%2C%20a%20signed%20OAuth%20request%21"
    )
    assert percent_encode("a~b") == "a~b"


def test_oauth1_matches_twitter_docs_vector() -> None:
    base = oauth1_signature_base(
        TWITTER_EXAMPLE_METHOD, TWITTER_EXAMPLE_URL, TWITTER_EXAMPLE_PARAMS
    )
    assert base == TWITTER_EXAMPLE_BASE
    signature = oauth1_sign(
        base, TWITTER_EXAMPLE_CONSUMER_SECRET, TWITTER_EXAMPLE_TOKEN_SECRET
    )
    assert signature == TWITTER_EXAMPLE_SIGNATURE


def test_authorization_header_includes_signed_fields() -> None:
    header = oauth1_authorization_header(
        "POST",
        TWITTER_EXAMPLE_URL,
        consumer_key="xvz1evFS4wEEPTGEFPHBog",
        consumer_secret=TWITTER_EXAMPLE_CONSUMER_SECRET,
        token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        token_secret=TWITTER_EXAMPLE_TOKEN_SECRET,
        extra_params={"include_entities": "true", "status": TWITTER_EXAMPLE_PARAMS["status"]},
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
        timestamp="1318622958",
    )
    assert header.startswith("OAuth ")
    assert "oauth_signature=" in header
    assert TWITTER_EXAMPLE_SIGNATURE.replace("/", "%2F") in header or (
        percent_encode(TWITTER_EXAMPLE_SIGNATURE) in header
    )


def _full_creds() -> XCredentials:
    return XCredentials(
        api_key="key",
        api_secret="secret",
        access_token="token",
        access_token_secret="token-secret",
    )


def test_missing_credentials_skip_without_http(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "X_API_KEY",
        "X_API_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
        "TWITTER_API_KEY",
        "TWITTER_API_SECRET",
        "TWITTER_ACCESS_TOKEN",
        "TWITTER_ACCESS_TOKEN_SECRET",
        "TWITTER_CONSUMER_KEY",
        "TWITTER_CONSUMER_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    session = FakeSession([])
    client = XClient.from_env(session=session)
    assert client.creds.complete is False
    assert x_credentials_ready(client.creds) is False
    result = client.post_image(b"\x89PNG", "hello")
    assert result.posted is False
    assert result.skipped is True
    assert result.message == MISSING_CREDS_MESSAGE
    assert session.calls == []


def test_twitter_alias_env_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X_API_KEY", raising=False)
    monkeypatch.delenv("X_API_SECRET", raising=False)
    monkeypatch.delenv("X_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("X_ACCESS_TOKEN_SECRET", raising=False)
    monkeypatch.setenv("TWITTER_API_KEY", "k")
    monkeypatch.setenv("TWITTER_API_SECRET", "s")
    monkeypatch.setenv("TWITTER_ACCESS_TOKEN", "t")
    monkeypatch.setenv("TWITTER_ACCESS_TOKEN_SECRET", "ts")
    creds = XCredentials.from_env()
    assert creds.complete
    assert creds.api_key == "k"


def test_post_image_mocked_happy_path() -> None:
    session = FakeSession(
        [
            FakeResponse(200, {"data": {"id": "media-1"}}),
            FakeResponse(200, {}),
            FakeResponse(200, {"data": {"id": "media-1"}}),
            FakeResponse(201, {"data": {"id": "tweet-9", "text": "RAT Score"}}),
        ]
    )
    client = XClient(_full_creds(), session=session)
    result = client.post_image(b"\x89PNG fake", "RAT Score · NVDA · sig abcd")
    assert result.posted is True
    assert result.tweet_id == "tweet-9"
    assert result.media_id == "media-1"
    assert result.url == "https://x.com/i/web/status/tweet-9"
    assert [c["url"] for c in session.calls] == [
        MEDIA_UPLOAD_URL,
        MEDIA_UPLOAD_URL,
        MEDIA_UPLOAD_URL,
        TWEET_URL,
    ]
    assert session.calls[0]["data"]["command"] == "INIT"
    assert session.calls[1]["data"]["command"] == "APPEND"
    assert session.calls[1]["files"]["media"][1] == b"\x89PNG fake"
    assert session.calls[2]["data"]["command"] == "FINALIZE"
    assert session.calls[3]["json"] == {
        "text": "RAT Score · NVDA · sig abcd",
        "media": {"media_ids": ["media-1"]},
    }
    for call in session.calls:
        assert call["headers"]["Authorization"].startswith("OAuth ")


def test_post_image_http_error_does_not_raise() -> None:
    session = FakeSession([FakeResponse(403, text="forbidden")])
    client = XClient(_full_creds(), session=session)
    result = client.post_image(b"\x89PNG fake", "hi")
    assert result.posted is False
    assert result.skipped is False
    assert "403" in result.message
    assert "forbidden" in result.message


def test_empty_png_skips_without_http() -> None:
    session = FakeSession([])
    client = XClient(_full_creds(), session=session)
    result = client.post_image(b"", "hi")
    assert result.skipped is True
    assert session.calls == []
