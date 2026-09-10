"""X (Twitter) API v2 client for user-triggered score-card posts.

OAuth 1.0a user context from env:

    X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET

Twitter-prefixed aliases are accepted. Missing credentials skip the post —
they must never crash the UI or fire a network call. This module does not
post unless ``post_image`` / ``create_post`` is invoked (button click).
"""

from __future__ import annotations

import binascii
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, urlparse, urlunparse

import requests
from dotenv import load_dotenv

TWEET_URL = "https://api.x.com/2/tweets"
MEDIA_UPLOAD_URL = "https://api.x.com/2/media/upload"
DEFAULT_TIMEOUT = 20.0

API_KEY_ENVS = ("X_API_KEY", "TWITTER_API_KEY", "TWITTER_CONSUMER_KEY")
API_SECRET_ENVS = ("X_API_SECRET", "TWITTER_API_SECRET", "TWITTER_CONSUMER_SECRET")
ACCESS_TOKEN_ENVS = ("X_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN")
ACCESS_SECRET_ENVS = ("X_ACCESS_TOKEN_SECRET", "TWITTER_ACCESS_TOKEN_SECRET")

MISSING_CREDS_MESSAGE = (
    "X post skipped: set X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, and "
    "X_ACCESS_TOKEN_SECRET (Render dashboard / Bitwarden) to publish. "
    "The PNG is still available to download."
)


def _first_env(*names: str) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def percent_encode(value: str) -> str:
    """RFC 3986 unreserved set, as required by OAuth 1.0a."""
    return quote(str(value), safe="~")


def oauth1_signature_base(
    method: str,
    url: str,
    params: dict[str, str],
) -> str:
    parsed = urlparse(url)
    base_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    pieces = [f"{percent_encode(k)}={percent_encode(v)}" for k, v in sorted(params.items())]
    param_string = "&".join(pieces)
    return "&".join(
        (
            method.upper(),
            percent_encode(base_url),
            percent_encode(param_string),
        )
    )


def oauth1_sign(base: str, consumer_secret: str, token_secret: str) -> str:
    key = f"{percent_encode(consumer_secret)}&{percent_encode(token_secret)}"
    digest = hmac.new(key.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).digest()
    return binascii.b2a_base64(digest, newline=False).decode("ascii")


def collect_oauth_params(
    url: str,
    extra: dict[str, str] | None,
    *,
    consumer_key: str,
    token: str,
    nonce: str,
    timestamp: str,
) -> dict[str, str]:
    params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp,
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    parsed = urlparse(url)
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        params[key] = value
    if extra:
        params.update({str(k): str(v) for k, v in extra.items()})
    return params


def oauth1_authorization_header(
    method: str,
    url: str,
    *,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    extra_params: dict[str, str] | None = None,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    oauth_nonce = nonce or secrets.token_hex(16)
    oauth_ts = timestamp or str(int(time.time()))
    params = collect_oauth_params(
        url,
        extra_params,
        consumer_key=consumer_key,
        token=token,
        nonce=oauth_nonce,
        timestamp=oauth_ts,
    )
    base = oauth1_signature_base(method, url, params)
    signature = oauth1_sign(base, consumer_secret, token_secret)
    oauth_fields = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": oauth_nonce,
        "oauth_signature": signature,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": oauth_ts,
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    packed = ", ".join(
        f'{percent_encode(k)}="{percent_encode(v)}"' for k, v in sorted(oauth_fields.items())
    )
    return f"OAuth {packed}"


@dataclass(frozen=True)
class XCredentials:
    api_key: str = ""
    api_secret: str = ""
    access_token: str = ""
    access_token_secret: str = ""

    @property
    def complete(self) -> bool:
        return bool(
            self.api_key and self.api_secret and self.access_token and self.access_token_secret
        )

    @classmethod
    def from_env(cls) -> XCredentials:
        load_dotenv()
        return cls(
            api_key=_first_env(*API_KEY_ENVS),
            api_secret=_first_env(*API_SECRET_ENVS),
            access_token=_first_env(*ACCESS_TOKEN_ENVS),
            access_token_secret=_first_env(*ACCESS_SECRET_ENVS),
        )


@dataclass
class XPostResult:
    posted: bool
    skipped: bool
    message: str
    url: str | None = None
    tweet_id: str | None = None
    media_id: str | None = None


def x_credentials_ready(creds: XCredentials | None = None) -> bool:
    return (creds or XCredentials.from_env()).complete


def _json_payload(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _media_id_from(payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for key in ("id", "media_id_string", "media_id"):
        value = data.get(key) if isinstance(data, dict) else None
        if value is not None and str(value):
            return str(value)
    return ""


def _tweet_id_from(payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    value = data.get("id") if isinstance(data, dict) else None
    return str(value) if value else ""


class XClient:
    """Thin OAuth 1.0a wrapper around X API v2 media + tweets."""

    def __init__(
        self,
        creds: XCredentials | None = None,
        *,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.creds = creds if creds is not None else XCredentials.from_env()
        self.session = session or requests.Session()
        self.timeout = timeout

    @classmethod
    def from_env(cls, **kwargs: Any) -> XClient:
        return cls(XCredentials.from_env(), **kwargs)

    def _signed_headers(
        self,
        method: str,
        url: str,
        extra_params: dict[str, str] | None = None,
    ) -> dict[str, str]:
        header = oauth1_authorization_header(
            method,
            url,
            consumer_key=self.creds.api_key,
            consumer_secret=self.creds.api_secret,
            token=self.creds.access_token,
            token_secret=self.creds.access_token_secret,
            extra_params=extra_params,
        )
        return {"Authorization": header}

    def _post(
        self,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        extra_params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        sign_params = dict(extra_params or {})
        if data is not None and files is None and json_body is None:
            sign_params.update({str(k): str(v) for k, v in data.items()})
        req_headers = self._signed_headers("POST", url, sign_params or None)
        if headers:
            req_headers.update(headers)
        return self.session.post(
            url,
            data=data,
            json=json_body,
            files=files,
            headers=req_headers,
            timeout=self.timeout,
        )

    def upload_png(self, png_bytes: bytes) -> str:
        """Chunked v2 media upload (INIT / APPEND / FINALIZE). Returns media id."""
        init_data = {
            "command": "INIT",
            "total_bytes": str(len(png_bytes)),
            "media_type": "image/png",
            "media_category": "tweet_image",
        }
        init_resp = self._post(MEDIA_UPLOAD_URL, data=init_data)
        init_payload = _json_payload(init_resp)
        if getattr(init_resp, "status_code", 0) >= 400:
            raise RuntimeError(
                f"X media INIT failed ({init_resp.status_code}): "
                f"{(getattr(init_resp, 'text', '') or '')[:240]}"
            )
        media_id = _media_id_from(init_payload)
        if not media_id:
            raise RuntimeError(f"X media INIT returned no media id: {init_payload!r}")

        files = {"media": ("score-card.png", png_bytes, "image/png")}
        append_data = {
            "command": "APPEND",
            "media_id": media_id,
            "segment_index": "0",
        }
        append_resp = self._post(MEDIA_UPLOAD_URL, data=append_data, files=files)
        if getattr(append_resp, "status_code", 0) >= 400:
            raise RuntimeError(
                f"X media APPEND failed ({append_resp.status_code}): "
                f"{(getattr(append_resp, 'text', '') or '')[:240]}"
            )

        fin_resp = self._post(
            MEDIA_UPLOAD_URL,
            data={"command": "FINALIZE", "media_id": media_id},
        )
        if getattr(fin_resp, "status_code", 0) >= 400:
            raise RuntimeError(
                f"X media FINALIZE failed ({fin_resp.status_code}): "
                f"{(getattr(fin_resp, 'text', '') or '')[:240]}"
            )
        finalized = _media_id_from(_json_payload(fin_resp)) or media_id
        return finalized

    def create_post(self, text: str, media_id: str) -> tuple[str, str | None]:
        body = {"text": text, "media": {"media_ids": [media_id]}}
        resp = self._post(
            TWEET_URL,
            json_body=body,
            headers={"Content-Type": "application/json"},
        )
        payload = _json_payload(resp)
        if getattr(resp, "status_code", 0) >= 400:
            raise RuntimeError(
                f"X tweet create failed ({resp.status_code}): "
                f"{(getattr(resp, 'text', '') or '')[:240]}"
            )
        tweet_id = _tweet_id_from(payload)
        if not tweet_id:
            raise RuntimeError(f"X tweet create returned no id: {payload!r}")
        return tweet_id, f"https://x.com/i/web/status/{tweet_id}"

    def post_image(self, png_bytes: bytes, text: str) -> XPostResult:
        """Upload PNG and create a v2 post. Skip (no network) when creds missing."""
        if not self.creds.complete:
            return XPostResult(posted=False, skipped=True, message=MISSING_CREDS_MESSAGE)
        if not png_bytes:
            return XPostResult(
                posted=False,
                skipped=True,
                message="X post skipped: score card image was empty.",
            )
        try:
            media_id = self.upload_png(png_bytes)
            tweet_id, url = self.create_post(text, media_id)
        except Exception as exc:  # noqa: BLE001 — caller / UI must not crash
            return XPostResult(
                posted=False,
                skipped=False,
                message=f"X post skipped: {exc}",
            )
        return XPostResult(
            posted=True,
            skipped=False,
            message=f"Posted to X: {url}",
            url=url,
            tweet_id=tweet_id,
            media_id=media_id,
        )
