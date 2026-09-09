from __future__ import annotations

import json

from rwa_score.__main__ import main as cli_main
from rwa_score.demo import main as demo_main


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
