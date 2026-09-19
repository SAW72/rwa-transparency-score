from __future__ import annotations

import json

from rwa_score.__main__ import main as cli_main
from rwa_score.demo import main as demo_main
from rwa_score.share import main as share_main
from rwa_score.x_client import (
    MISSING_CREDS_MESSAGE,
    X_POST_UNAVAILABLE_MESSAGE,
    XCredentials,
)


def test_cli_fixtures_json(capsys) -> None:
    assert cli_main(["--fixtures", "--json", "NVDA", "TSLA"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["ticker"] == "NVDA"
    assert payload[0]["data_source"] == "fixture"
    assert "error" not in payload[0]
    assert payload[1]["ticker"] == "TSLA"


def test_cli_fixtures_text(capsys) -> None:
    assert cli_main(["--fixtures", "AAPL"]) == 0
    out = capsys.readouterr().out
    assert "DEMO FIXTURES" in out
    assert "AAPL" in out


def test_cli_unknown_ticker_json(capsys) -> None:
    assert cli_main(["--fixtures", "--json", "ZZZZ"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "error" in payload[0]


def test_demo_defaults_to_fixtures(capsys) -> None:
    assert demo_main([]) == 0
    out = capsys.readouterr().out
    assert "DEMO FIXTURES" in out
    assert "NVDA" in out
    assert "TSLA" in out
    assert "AAPL" in out


class _ShareXResult:
    def __init__(self, *, posted: bool, message: str, url: str | None = None) -> None:
        self.posted = posted
        self.message = message
        self.url = url


class _ShareXClient:
    def __init__(self, result: _ShareXResult, *, complete: bool = True) -> None:
        self._result = result
        self.creds = XCredentials(
            api_key="k" if complete else "",
            api_secret="s" if complete else "",
            access_token="t" if complete else "",
            access_token_secret="ts" if complete else "",
        )
        self.calls: list[tuple[bytes, str]] = []

    def post_image(self, png_bytes: bytes, text: str) -> _ShareXResult:
        self.calls.append((png_bytes, text))
        return self._result


def test_share_cli_no_post_builds_png(capsys) -> None:
    assert share_main(["--fixtures", "--no-post", "NVDA"]) == 0
    out = capsys.readouterr().out
    assert "NVDA" in out
    assert "X post skipped (disabled)." in out
    assert "https://x.com/" not in out


def test_share_cli_missing_creds(monkeypatch, capsys) -> None:
    for name in (
        "X_API_KEY",
        "X_API_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
        "TWITTER_API_KEY",
        "TWITTER_API_SECRET",
        "TWITTER_ACCESS_TOKEN",
        "TWITTER_ACCESS_TOKEN_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    client = _ShareXClient(
        _ShareXResult(posted=False, message=MISSING_CREDS_MESSAGE),
        complete=False,
    )
    assert share_main(["--fixtures", "--json", "bNVDA"], x_client=client) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ticker"] == "bNVDA"
    assert payload["png_bytes"] > 0
    assert payload["x_posted"] is False
    assert payload["x_url"] is None
    assert payload["x_message"] == MISSING_CREDS_MESSAGE
    assert payload["x_message"] != X_POST_UNAVAILABLE_MESSAGE


def test_share_cli_happy_path_prints_url(capsys) -> None:
    url = "https://x.com/i/web/status/share-cli-1"
    client = _ShareXClient(_ShareXResult(posted=True, message=f"Posted to X: {url}", url=url))
    assert share_main(["--fixtures", "NVDA"], x_client=client) == 0
    out = capsys.readouterr().out
    assert url in out
    assert len(client.calls) == 1
    assert client.calls[0][0].startswith(b"\x89PNG")


def test_share_cli_api_failure_is_not_missing_creds(capsys) -> None:
    client = _ShareXClient(
        _ShareXResult(posted=False, message=X_POST_UNAVAILABLE_MESSAGE),
        complete=True,
    )
    assert share_main(["--fixtures", "--json", "NVDA"], x_client=client) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["x_posted"] is False
    assert payload["x_url"] is None
    assert payload["x_message"] == X_POST_UNAVAILABLE_MESSAGE
    assert payload["x_credentials_ready"] is True
