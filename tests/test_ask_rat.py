"""Ask RAT agent — tools over existing clients, fixture honesty, templated fallback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rwa_score.ask_rat import (
    ASK_RAT_CHIPS,
    ASK_RAT_GREETING,
    TOOL_ASSETS_LIST,
    TOOL_ISSUERS,
    TOOL_LOOKUP,
    TOOL_MARKET_PAIRS,
    TOOL_NAMES,
    TOOL_QUOTES_LATEST,
    TOOL_SCORE_TICKER,
    XAI_TOOLS,
    ask,
    client_source,
    execute_tool,
    extract_tickers,
    fallback_answer,
    format_ask_cmc_lines,
    honesty_line,
)
from rwa_score.explainer import AI_FOOTNOTE, XAI_CHAT_URL, XAI_MODEL
from rwa_score.scorer import ALWAYS_SELF_REPORTED, PILLARS, TransparencyScorer
from tests.conftest import RecordingClient
from tests.test_explainer import FakeResponse, FakeSession


def test_greeting_and_chips_are_exact() -> None:
    assert ASK_RAT_GREETING == "Ask RAT — ask anything about a tokenized stock's risk score"
    assert ASK_RAT_CHIPS == (
        "Why is bNVDA greener than NVDA?",
        "What\u2019s weakest on TSLA?",
        "Which pillar is self-reported on NVDA?",
    )
    assert "tts" not in ASK_RAT_GREETING.lower()


def test_extract_tickers_keeps_bnvda_and_nvda_distinct() -> None:
    found = extract_tickers(ASK_RAT_CHIPS[0])
    assert "bNVDA" in found
    assert "NVDA" in found
    assert extract_tickers(ASK_RAT_CHIPS[1]) == ["TSLA"]
    assert extract_tickers(ASK_RAT_CHIPS[2]) == ["NVDA"]


def test_honesty_line_never_claims_live_on_fixture() -> None:
    fixture = honesty_line("fixture")
    assert "FIXTURE" in fixture
    assert "not live" in fixture.lower()
    live = honesty_line("live")
    assert live.startswith("LIVE")
    assert "not fixtures" in live


def test_tool_specs_cover_required_surface() -> None:
    names = {row["function"]["name"] for row in XAI_TOOLS}
    assert names == set(TOOL_NAMES)
    assert TOOL_LOOKUP in names
    assert TOOL_ASSETS_LIST in names
    assert TOOL_QUOTES_LATEST in names
    assert TOOL_MARKET_PAIRS in names
    assert TOOL_ISSUERS in names
    assert TOOL_SCORE_TICKER in names


def test_tools_use_existing_client_not_invented_http(recording_client) -> None:
    scorer = TransparencyScorer(recording_client)
    lookup = execute_tool(TOOL_LOOKUP, {"symbol": "NVDA"}, scorer=scorer)
    assert lookup["ok"] is True
    assert lookup["data_source"] == "live"
    assert lookup["map"]["rwa_id"] == 2
    assert recording_client.calls["rwa_map"] == 1
    assert recording_client.calls["rwa_info"] == 1

    listed = execute_tool(TOOL_ASSETS_LIST, {}, scorer=scorer)
    assert listed["ok"] is True
    assert recording_client.calls["assets_list"] == 1

    quotes = execute_tool(TOOL_QUOTES_LATEST, {"symbol": "NVDA"}, scorer=scorer)
    assert quotes["ok"] is True
    assert recording_client.calls["rwa_quotes"] == 1

    pairs = execute_tool(TOOL_MARKET_PAIRS, {"symbol": "NVDA"}, scorer=scorer)
    assert pairs["ok"] is True
    assert recording_client.calls["market_pairs"] == 1

    issuers = execute_tool(TOOL_ISSUERS, {}, scorer=scorer)
    assert issuers["ok"] is True
    assert recording_client.calls["issuers_list"] == 1

    scored = execute_tool(TOOL_SCORE_TICKER, {"symbol": "NVDA"}, scorer=scorer)
    assert scored["ok"] is True
    assert scored["ticker"] == "NVDA"
    assert scored["band"] in {"GREEN", "YELLOW", "ORANGE", "RED"}


def test_score_ticker_uses_injected_score_fn(fixture_scorer) -> None:
    calls: list[str] = []

    def fake_score(symbol: str) -> dict[str, Any]:
        calls.append(symbol)
        return fixture_scorer.score(symbol)

    payload = execute_tool(
        TOOL_SCORE_TICKER,
        {"symbol": "TSLA"},
        scorer=fixture_scorer,
        score_fn=fake_score,
    )
    assert payload["ok"] is True
    assert calls == ["TSLA"]
    assert payload["weakest"]["key"] == "basis"


def test_fallback_demo_chips_fixture_honest(fixture_scorer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    greener = ask(ASK_RAT_CHIPS[0], fixture_scorer)
    assert greener.polished is False
    assert greener.data_source == "fixture"
    assert greener.skipped_reason
    assert "FIXTURE" in greener.answer
    assert "not live" in greener.answer.lower()
    assert "live CoinMarketCap" not in greener.answer.replace("not live CoinMarketCap", "")
    assert "not greener" in greener.answer.lower() or "same band" in greener.answer.lower()
    assert TOOL_SCORE_TICKER in greener.tools_used
    lines = format_ask_cmc_lines(greener.cmc_calls)
    assert lines[0].startswith("CMC calls this turn — Fixture")
    assert "not" in lines[0].lower() and "live" in lines[0].lower()
    assert not any(" · live" in line for line in lines)

    weakest = ask(ASK_RAT_CHIPS[1], fixture_scorer)
    assert "FIXTURE" in weakest.answer
    assert "Cross-issuer basis" in weakest.answer
    assert "TSLA" in weakest.answer

    reported = ask(ASK_RAT_CHIPS[2], fixture_scorer)
    assert "FIXTURE" in reported.answer
    for key in ALWAYS_SELF_REPORTED:
        assert PILLARS[key]["label"] in reported.answer
    assert "CMC field (self-reported)" in reported.answer
    assert reported.footnote == AI_FOOTNOTE


def test_ask_without_key_never_raises(fixture_scorer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    result = ask("", fixture_scorer)
    assert result.polished is False
    assert "FIXTURE" in result.answer
    assert "not financial advice" in result.answer.lower()


def test_ask_uses_xai_tools_when_key_present(
    fixture_scorer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XAI_API_KEY", "test-key-not-secret")
    session = FakeSession(
        FakeResponse(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": "TSLA's weakest pillar is Cross-issuer basis.",
                        }
                    }
                ]
            },
        )
    )
    result = ask(ASK_RAT_CHIPS[1], fixture_scorer, session=session)
    assert result.polished is True
    assert "FIXTURE" in result.answer
    assert "Cross-issuer basis" in result.answer
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == XAI_CHAT_URL
    assert call["json"]["model"] == XAI_MODEL
    assert call["json"]["tools"]
    assert call["timeout"] <= 8.0


def test_ask_falls_back_on_xai_timeout(fixture_scorer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "test-key-not-secret")

    class SlowSession:
        def post(self, *args, **kwargs):
            raise TimeoutError("too slow")

    result = ask(ASK_RAT_CHIPS[1], fixture_scorer, session=SlowSession())
    assert result.polished is False
    assert "FIXTURE" in result.answer
    assert "Cross-issuer basis" in result.answer
    assert result.skipped_reason


def test_ask_falls_back_when_budget_is_tiny(
    fixture_scorer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XAI_API_KEY", "test-key-not-secret")
    session = FakeSession(
        FakeResponse(200, {"choices": [{"message": {"content": "polished"}}]})
    )
    result = ask(
        ASK_RAT_CHIPS[2],
        fixture_scorer,
        session=session,
        budget_seconds=0.01,
    )
    assert result.polished is False
    assert "CMC field (self-reported)" in result.answer


def test_ask_xai_tool_round_executes_score_ticker(
    fixture_scorer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XAI_API_KEY", "test-key-not-secret")

    class ToolThenAnswer:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def post(self, url, headers=None, json=None, timeout=None):
            self.calls.append({"url": url, "json": json, "timeout": timeout, "headers": headers})
            if len(self.calls) == 1:
                return FakeResponse(
                    200,
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": "call_1",
                                            "type": "function",
                                            "function": {
                                                "name": TOOL_SCORE_TICKER,
                                                "arguments": '{"symbol": "TSLA"}',
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "content": "Weakest is Cross-issuer basis on the fixture score."
                            }
                        }
                    ]
                },
            )

    session = ToolThenAnswer()
    result = ask(ASK_RAT_CHIPS[1], fixture_scorer, session=session)
    assert result.polished is True
    assert TOOL_SCORE_TICKER in result.tools_used
    assert "FIXTURE" in result.answer
    assert len(session.calls) == 2
    tool_msg = session.calls[1]["json"]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert "TSLA" in tool_msg["content"]


def test_fallback_live_client_says_live_not_fixture(recording_client) -> None:
    scorer = TransparencyScorer(recording_client)
    text = fallback_answer("What’s weakest on NVDA?", scorer)
    assert text.startswith("LIVE CoinMarketCap")
    assert "not fixtures" in text
    assert "FIXTURE" not in text


def test_unknown_ticker_does_not_crash(fixture_scorer) -> None:
    result = ask("What’s weakest on NOTATICKER?", fixture_scorer)
    assert result.answer
    assert "FIXTURE" in result.answer
    assert "not financial advice" in result.answer.lower()


def test_app_wires_collapsed_chat_no_tts() -> None:
    import app as demo_app

    assert demo_app.ASK_RAT_GREETING == ASK_RAT_GREETING
    assert demo_app.ASK_RAT_CHIPS == ASK_RAT_CHIPS
    source = Path(demo_app.__file__).read_text(encoding="utf-8")
    assert "ASK_RAT_GREETING" in source
    assert "st.write(ASK_RAT_GREETING)" in source
    assert "st.chat_input" in source
    assert "st.chat_message" in source
    assert 'open_expander("Ask RAT"' in source or 'st.expander("Ask RAT"' in source
    assert "_render_ask_rat(scorer" in source
    render = source.split("def _render_ask_rat", 1)[1].split(
        "def _render_sidebar_controls", 1
    )[0]
    assert "Share score card" not in render
    assert "_maybe_rerun()" not in render
    assert "st.rerun(" not in render
    assert "st.chat_input" in render
    assert "ask_rat_chip_" in render
    assert "ask_rat_prefill" in render
    assert "format_ask_cmc_lines" in render
    lowered = source.lower()
    assert "no voice" in lowered
    assert "text-to-speech" not in lowered
    assert "elevenlabs" not in lowered
    assert "pyttsx" not in lowered
    assert "st.audio(" not in source
    readme = Path("README.md").read_text(encoding="utf-8")
    assert ASK_RAT_GREETING in readme
    assert ASK_RAT_CHIPS[1] in readme
    assert "No TTS" in readme
