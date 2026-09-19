"""Ask RAT — text chat grounded in scores / CMC / PoR via existing clients.

xAI (same family as ``explainer.py``) may call tools. Missing ``XAI_API_KEY``,
HTTP failure, or a timeout returns a templated answer from the same tools —
never raises, never invents live CMC when the client is a fixture, no TTS.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from .client import summarize_call_log
from .explainer import AI_FOOTNOTE, XAI_CHAT_URL, XAI_MODEL
from .scorer import (
    ALWAYS_SELF_REPORTED,
    PILLARS,
    WEIGHTS,
    ScoreError,
    TransparencyScorer,
    band_code,
)
from .ticker_search import normalize_ticker

ASK_RAT_GREETING = "Ask RAT — ask anything about a tokenized stock's risk score"
ASK_RAT_CHIPS = (
    "Why is bNVDA greener than NVDA?",
    "What\u2019s weakest on TSLA?",
    "Which pillar is self-reported on NVDA?",
)

ASK_XAI_TIMEOUT = 8.0
ASK_XAI_MAX_TOKENS = 400
ASK_MAX_TOOL_ROUNDS = 3
ASK_BUDGET_SECONDS = 15.0
ASK_TOOL_TIMEOUT = 8.0

TOOL_LOOKUP = "lookup"
TOOL_ASSETS_LIST = "assets_list"
TOOL_QUOTES_LATEST = "quotes_latest"
TOOL_MARKET_PAIRS = "market_pairs"
TOOL_ISSUERS = "issuers"
TOOL_SCORE_TICKER = "score_ticker"
TOOL_NAMES = (
    TOOL_LOOKUP,
    TOOL_ASSETS_LIST,
    TOOL_QUOTES_LATEST,
    TOOL_MARKET_PAIRS,
    TOOL_ISSUERS,
    TOOL_SCORE_TICKER,
)

_KNOWN_TICKERS = (
    "NVDA",
    "TSLA",
    "AAPL",
    "META",
    "XOM",
    "PLD",
    "bNVDA",
    "bIB01",
    "bCSPX",
    "bC3M",
    "bIBTA",
)
_TICKER_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(b[A-Za-z]{2,6}|[A-Za-z]{2,5}x?)(?![A-Za-z0-9])"
)
_STOP_WORDS = frozenset(
    {
        "WHY",
        "IS",
        "THAN",
        "WHAT",
        "WHATS",
        "WHICH",
        "PILLAR",
        "ON",
        "THE",
        "FOR",
        "AND",
        "ASK",
        "RAT",
        "GREENER",
        "WEAKEST",
        "SELF",
        "REPORTED",
        "TOKEN",
        "SCORE",
        "RISK",
    }
)

SYSTEM_PROMPT = (
    "You are Ask RAT, a text-only helper for RWA transparency scores. "
    "Use the provided tools (lookup/map/info, assets/list, quotes/latest, "
    "market-pairs, issuers, score_ticker). Do not invent HTTP endpoints or "
    "numbers. If a tool result says data_source=fixture, you MUST say the "
    "answer uses bundled demo fixtures and must NOT claim live CoinMarketCap. "
    "If data_source=live, say LIVE CMC (not fixtures). Be concise (under 180 "
    "words). Not financial advice. No voice or TTS."
)


@dataclass
class AskResult:
    """One Ask RAT turn. ``polished`` is False when the templated path ran."""

    answer: str
    polished: bool
    data_source: str
    tools_used: tuple[str, ...]
    cmc_calls: dict[str, Any]
    footnote: str = AI_FOOTNOTE
    skipped_reason: str | None = None
    prefill: str = ""


@dataclass
class _ToolRun:
    used: list[str] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)


def client_source(client: object) -> str:
    """Canonical fixture vs live label from the existing client."""
    return "fixture" if getattr(client, "source", "") == "fixture" else "live"


def honesty_line(source: str) -> str:
    """First-line mode cue. Fixture never claims live CMC."""
    if source == "fixture":
        return "FIXTURE (bundled demo data — not live CoinMarketCap)."
    return "LIVE CoinMarketCap (not fixtures)."


def format_ask_cmc_lines(block: dict[str, Any] | None) -> list[str]:
    """Evidence strip for this chat turn. Fixture never says live."""
    payload = block or {}
    source = "fixture" if payload.get("source") == "fixture" else "live"
    if source == "fixture":
        header = "CMC calls this turn — Fixture (bundled demo, **not** live CMC)"
    else:
        header = "CMC calls this turn — Live CMC (not fixtures)"
    lines = [header]
    endpoints = payload.get("endpoints") or []
    if not endpoints:
        lines.append("No CMC/fixture directory calls recorded on this turn.")
        return lines
    for row in endpoints:
        endpoint = row.get("endpoint") or ""
        via = row.get("via") or ("fixture" if source == "fixture" else "network")
        tag = (
            "fixture"
            if source == "fixture"
            else ("cache" if row.get("cached") or via == "cache" else "live")
        )
        lines.append(f"`GET {endpoint}` · {tag}")
    return lines


def extract_tickers(
    question: str, catalog_symbols: list[str] | None = None
) -> list[str]:
    """Pull ticker-like tokens. Longest catalog hits first so bNVDA ≠ NVDA."""
    text = question or ""
    found: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        symbol = normalize_ticker(raw)
        if not symbol or symbol in seen:
            return
        if symbol.upper() in _STOP_WORDS:
            return
        seen.add(symbol)
        found.append(symbol)

    catalog = list(catalog_symbols or [])
    for known in _KNOWN_TICKERS:
        if known not in catalog:
            catalog.append(known)
    catalog.sort(key=len, reverse=True)
    upper = text.upper()
    for symbol in catalog:
        token = normalize_ticker(symbol)
        if not token:
            continue
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        if pattern.search(text) or pattern.search(upper):
            _add(token)
    for match in _TICKER_TOKEN.finditer(text):
        _add(match.group(1))
    return found


def _log_start(client: object) -> int:
    fetch = getattr(client, "call_log", None)
    if not callable(fetch):
        return 0
    try:
        return len(fetch())
    except Exception:  # noqa: BLE001
        return 0


def _calls_since(client: object, start: int) -> dict[str, Any]:
    source = client_source(client)
    fetch = getattr(client, "call_log", None)
    extra: list[dict[str, Any]] = []
    if callable(fetch):
        try:
            extra = list(fetch())[start:]
        except Exception:  # noqa: BLE001
            extra = []
    return summarize_call_log(extra, client_source=source)


def _compact_score(report: dict[str, Any]) -> dict[str, Any]:
    subs = report.get("subscores") or {}
    verification = report.get("verification") or {}
    weakest_key = ""
    if subs:
        weakest_key = min(WEIGHTS, key=lambda item: float(subs.get(item) or 0))
    self_reported = []
    for key in WEIGHTS:
        block = verification.get(key) or {}
        level = str(block.get("level") or "")
        source = str(block.get("source") or "")
        if key in ALWAYS_SELF_REPORTED or level == "self-reported":
            kind = (
                "CMC field (self-reported)"
                if key in ALWAYS_SELF_REPORTED
                else (
                    "heuristic fallback (self-reported)"
                    if source == "heuristic_fallback"
                    else "self-reported"
                )
            )
            self_reported.append({"pillar": key, "label": PILLARS[key]["label"], "kind": kind})
    compact_ver = {}
    for key, block in verification.items():
        if not isinstance(block, dict):
            continue
        meta = block.get("meta") or {}
        compact_ver[key] = {
            "level": block.get("level"),
            "source": block.get("source"),
            "evidence": (block.get("evidence") or "")[:240],
            "published_por_feed": meta.get("published_por_feed"),
            "por_path": meta.get("por_path"),
        }
    data_source = "fixture" if report.get("data_source") == "fixture" else "live"
    return {
        "ticker": report.get("ticker"),
        "issuer": report.get("issuer"),
        "score": report.get("score"),
        "band": report.get("band") or band_code(float(report.get("score") or 0)),
        "data_source": data_source,
        "live_verifiers": bool(report.get("live_verifiers")),
        "subscores": {key: subs.get(key) for key in WEIGHTS},
        "weakest": {
            "key": weakest_key,
            "label": PILLARS[weakest_key]["label"] if weakest_key else "",
            "score": float(subs.get(weakest_key) or 0) if weakest_key else None,
        },
        "self_reported": self_reported,
        "verification": compact_ver,
        "issuer_note": report.get("issuer_note") or "",
        "flags": list(report.get("flags") or [])[:4],
        "notes": [str(n) for n in (report.get("notes") or [])[:4]],
    }


def _json_ok(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)


def _run_lookup(client: object, symbol: str) -> dict[str, Any]:
    ticker = normalize_ticker(symbol)
    source = client_source(client)
    mapped = list(client.rwa_map(ticker) or [])
    if not mapped:
        return {
            "ok": False,
            "tool": TOOL_LOOKUP,
            "data_source": source,
            "error": f"{ticker} not on the CMC/fixture map.",
        }
    row = mapped[0] if isinstance(mapped[0], dict) else {}
    rwa_id = row.get("rwa_id")
    info: dict[str, Any] = {}
    if rwa_id is not None:
        try:
            info = dict(client.rwa_info(int(rwa_id)) or {})
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "tool": TOOL_LOOKUP,
                "data_source": source,
                "error": f"rwa_info failed: {exc}",
            }
    issuer = info.get("issuer") if isinstance(info.get("issuer"), dict) else {}
    return {
        "ok": True,
        "tool": TOOL_LOOKUP,
        "data_source": source,
        "map": {
            "symbol": row.get("symbol") or ticker,
            "rwa_id": rwa_id,
            "name": row.get("name") or info.get("name") or "",
        },
        "info": {
            "symbol": info.get("symbol") or ticker,
            "name": info.get("name") or "",
            "cik": info.get("cik"),
            "issuer": issuer.get("name") or "",
        },
    }


def _run_assets_list(client: object, asset_type: str | None = None) -> dict[str, Any]:
    source = client_source(client)
    kwargs: dict[str, Any] = {"limit": 8}
    if asset_type:
        kwargs["asset_type"] = asset_type
    data = client.assets_list(**kwargs)
    rows = []
    for row in (data.get("rwa_assets") or [])[:8]:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "symbol": row.get("symbol"),
                "name": row.get("name"),
                "asset_type": row.get("asset_type"),
                "rwa_rank": row.get("rwa_rank"),
            }
        )
    return {
        "ok": True,
        "tool": TOOL_ASSETS_LIST,
        "data_source": source,
        "total_size": data.get("total_size"),
        "rwa_assets": rows,
    }


def _run_quotes_latest(
    client: object, *, symbol: str | None = None, rwa_id: int | None = None
) -> dict[str, Any]:
    source = client_source(client)
    if rwa_id is not None:
        data = client.rwa_quotes(rwa_id=int(rwa_id))
    else:
        data = client.rwa_quotes(symbol=normalize_ticker(symbol or ""))
    tokens = data.get("tokens") or []
    return {
        "ok": True,
        "tool": TOOL_QUOTES_LATEST,
        "data_source": source,
        "symbol": data.get("symbol"),
        "rwa_id": data.get("rwa_id"),
        "average_tokenized_price": data.get("average_tokenized_price"),
        "tokenized_market_cap": data.get("tokenized_market_cap"),
        "tokenized_volume_24h": data.get("tokenized_volume_24h"),
        "token_count": len(tokens) if isinstance(tokens, list) else 0,
    }


def _run_market_pairs(
    client: object, *, symbol: str | None = None, rwa_id: int | None = None
) -> dict[str, Any]:
    source = client_source(client)
    if rwa_id is not None:
        data = client.market_pairs(rwa_id=int(rwa_id))
    else:
        data = client.market_pairs(symbol=normalize_ticker(symbol or ""))
    pairs = data.get("market_pairs") or []
    sample = []
    if isinstance(pairs, list):
        for row in pairs[:5]:
            if not isinstance(row, dict):
                continue
            market = row.get("market_pair") or row.get("pair") or ""
            sample.append(str(market)[:80])
    return {
        "ok": True,
        "tool": TOOL_MARKET_PAIRS,
        "data_source": source,
        "symbol": data.get("symbol"),
        "rwa_id": data.get("rwa_id"),
        "num_market_pairs": data.get("num_market_pairs"),
        "sample_pairs": sample,
    }


def _run_issuers(client: object, issuer_id: str | None = None) -> dict[str, Any]:
    source = client_source(client)
    if issuer_id:
        detail = dict(client.issuer(str(issuer_id)) or {})
        tokens = []
        for row in detail.get("tokens") or []:
            if isinstance(row, dict) and row.get("symbol"):
                tokens.append(row.get("symbol"))
        return {
            "ok": True,
            "tool": TOOL_ISSUERS,
            "data_source": source,
            "issuer_id": issuer_id,
            "name": detail.get("name") or "",
            "token_symbols": tokens[:8],
        }
    rows = []
    for row in (client.issuers_list() or [])[:10]:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "issuer_id": row.get("issuer_id"),
                "name": row.get("name"),
            }
        )
    return {
        "ok": True,
        "tool": TOOL_ISSUERS,
        "data_source": source,
        "issuers": rows,
    }


def _run_score_ticker(
    scorer: TransparencyScorer,
    symbol: str,
    score_fn: Callable[[str], dict[str, Any]] | None,
) -> dict[str, Any]:
    ticker = normalize_ticker(symbol)
    source = client_source(scorer.client)
    try:
        report = score_fn(ticker) if score_fn is not None else scorer.score(ticker)
    except ScoreError as exc:
        return {
            "ok": False,
            "tool": TOOL_SCORE_TICKER,
            "data_source": source,
            "ticker": ticker,
            "error": str(exc),
        }
    compact = _compact_score(report if isinstance(report, dict) else {})
    compact.update({"ok": True, "tool": TOOL_SCORE_TICKER})
    return compact


XAI_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": TOOL_LOOKUP,
            "description": "Resolve a ticker via existing rwa_map + rwa_info (lookup/map/info).",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_ASSETS_LIST,
            "description": "Ranked RWA directory via existing assets/list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "asset_type": {
                        "type": "string",
                        "description": "Optional CMC asset_type filter (stock, etf, …).",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_QUOTES_LATEST,
            "description": "RWA quotes/latest via the existing client (not raw HTTP).",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "rwa_id": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_MARKET_PAIRS,
            "description": "Wrapper markets via existing market-pairs/list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "rwa_id": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_ISSUERS,
            "description": "Issuer directory (issuers/list) or one issuer by id.",
            "parameters": {
                "type": "object",
                "properties": {"issuer_id": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_SCORE_TICKER,
            "description": "Score a ticker with TransparencyScorer (six pillars + verification).",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        },
    },
]


def execute_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    scorer: TransparencyScorer,
    score_fn: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Dispatch one tool onto the existing client / scorer. Never invents HTTP."""
    client = scorer.client
    args = arguments if isinstance(arguments, dict) else {}
    try:
        if name == TOOL_LOOKUP:
            return _run_lookup(client, str(args.get("symbol") or ""))
        if name == TOOL_ASSETS_LIST:
            kind = args.get("asset_type")
            return _run_assets_list(client, str(kind) if kind else None)
        if name == TOOL_QUOTES_LATEST:
            raw_id = args.get("rwa_id")
            rwa_id = int(raw_id) if raw_id not in (None, "") else None
            return _run_quotes_latest(
                client, symbol=str(args.get("symbol") or "") or None, rwa_id=rwa_id
            )
        if name == TOOL_MARKET_PAIRS:
            raw_id = args.get("rwa_id")
            rwa_id = int(raw_id) if raw_id not in (None, "") else None
            return _run_market_pairs(
                client, symbol=str(args.get("symbol") or "") or None, rwa_id=rwa_id
            )
        if name == TOOL_ISSUERS:
            issuer_id = args.get("issuer_id")
            return _run_issuers(client, str(issuer_id) if issuer_id else None)
        if name == TOOL_SCORE_TICKER:
            return _run_score_ticker(scorer, str(args.get("symbol") or ""), score_fn)
    except Exception as exc:  # noqa: BLE001 — chat must stay up
        return {
            "ok": False,
            "tool": name,
            "data_source": client_source(client),
            "error": str(exc),
        }
    return {
        "ok": False,
        "tool": name,
        "data_source": client_source(client),
        "error": f"Unknown tool {name}",
    }


def _call_with_timeout(fn: Callable[[], Any], timeout: float) -> tuple[Any, str | None]:
    """Run ``fn`` with a wall-clock cap. Does not block the caller past ``timeout``."""
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["ok"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["err"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(max(0.05, float(timeout)))
    if worker.is_alive():
        return None, "timeout"
    if "err" in box:
        return None, str(box["err"])
    return box.get("ok"), None


def _parse_tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _weakest_line(compact: dict[str, Any]) -> str:
    weak = compact.get("weakest") or {}
    label = weak.get("label") or weak.get("key") or "unknown pillar"
    score = weak.get("score")
    if score is None:
        return f"Weakest pillar is not available on {compact.get('ticker')}."
    return f"Weakest on {compact.get('ticker')}: {label} {float(score):.0f}/100."


def _self_reported_line(compact: dict[str, Any]) -> str:
    rows = compact.get("self_reported") or []
    if not rows:
        return f"No self-reported pillars recorded on {compact.get('ticker')}."
    always = [row["label"] for row in rows if row.get("kind") == "CMC field (self-reported)"]
    other = [row["label"] for row in rows if row.get("kind") != "CMC field (self-reported)"]
    bits = []
    if always:
        bits.append(
            "Always CMC field (self-reported): " + ", ".join(always) + "."
        )
    if other:
        bits.append(
            "Also labeled self-reported on this pass: " + ", ".join(other) + "."
        )
    return " ".join(bits)


def _greener_line(left: dict[str, Any], right: dict[str, Any]) -> str:
    rank = {"GREEN": 3, "YELLOW": 2, "ORANGE": 1, "RED": 0}
    l_band = str(left.get("band") or "")
    r_band = str(right.get("band") or "")
    l_score = float(left.get("score") or 0)
    r_score = float(right.get("score") or 0)
    l_name = left.get("ticker")
    r_name = right.get("ticker")
    if rank.get(l_band, -1) > rank.get(r_band, -1) or (
        l_band == r_band and l_score > r_score + 0.05
    ):
        lead = (
            f"{l_name} is greener than {r_name}: {l_score:.1f} {l_band} vs "
            f"{r_score:.1f} {r_band}."
        )
    elif rank.get(r_band, -1) > rank.get(l_band, -1) or (
        l_band == r_band and r_score > l_score + 0.05
    ):
        lead = (
            f"{r_name} is greener than {l_name} on this pass: {r_score:.1f} {r_band} vs "
            f"{l_score:.1f} {l_band}."
        )
    else:
        lead = (
            f"{l_name} and {r_name} are the same band on this pass "
            f"({l_score:.1f} {l_band} vs {r_score:.1f} {r_band}) — not greener."
        )
    l_por = ((left.get("verification") or {}).get("reserves") or {}).get(
        "published_por_feed"
    )
    r_por = ((right.get("verification") or {}).get("reserves") or {}).get(
        "published_por_feed"
    )
    if l_por and not r_por:
        lead += (
            f" {l_name} has a published Chainlink PoR feed ({l_por}); "
            f"{r_name} does not."
        )
        if ((left.get("verification") or {}).get("reserves") or {}).get("por_path") == (
            "fixture_labeled_skip"
        ):
            lead += " Fixture/offline mode skipped the live RPC — not an on-chain read."
    return lead


def fallback_answer(
    question: str,
    scorer: TransparencyScorer,
    *,
    score_fn: Callable[[str], dict[str, Any]] | None = None,
    catalog_symbols: list[str] | None = None,
    run: _ToolRun | None = None,
) -> str:
    """Templated reply that still uses tools. Never claims live on fixtures."""
    source = client_source(scorer.client)
    journal = run or _ToolRun()
    tickers = extract_tickers(question, catalog_symbols)
    scores: dict[str, dict[str, Any]] = {}
    for symbol in tickers:
        payload = execute_tool(
            TOOL_SCORE_TICKER, {"symbol": symbol}, scorer=scorer, score_fn=score_fn
        )
        journal.used.append(TOOL_SCORE_TICKER)
        journal.results[symbol] = payload
        if payload.get("ok"):
            scores[symbol] = payload
    parts = [honesty_line(source)]
    q = (question or "").lower()
    if "green" in q and len(scores) >= 2:
        ordered = list(scores.values())
        parts.append(_greener_line(ordered[0], ordered[1]))
    elif "weak" in q and scores:
        first = next(iter(scores.values()))
        parts.append(_weakest_line(first))
        evidence = ((first.get("verification") or {}).get(first.get("weakest", {}).get("key") or "") or {}).get(
            "evidence"
        )
        if evidence:
            parts.append(str(evidence)[:220])
    elif "self-reported" in q or "self reported" in q:
        if scores:
            parts.append(_self_reported_line(next(iter(scores.values()))))
        else:
            labels = [PILLARS[key]["label"] for key in ALWAYS_SELF_REPORTED]
            parts.append(
                "Price integrity, Disclosure, and Cross-issuer basis are always "
                f"CMC field (self-reported): {', '.join(labels)}."
            )
    elif scores:
        for compact in scores.values():
            parts.append(
                f"{compact.get('ticker')} scores {compact.get('score')} "
                f"({compact.get('band')})."
            )
            parts.append(_weakest_line(compact))
    elif tickers:
        parts.append(
            "Could not score "
            + ", ".join(tickers)
            + " from the CMC/fixture directory on this pass."
        )
    else:
        parts.append(
            "Name a ticker (NVDA, TSLA, bNVDA, …) to ground the answer in a score."
        )
    parts.append("This is an automated summary, not financial advice.")
    return " ".join(part for part in parts if part)


def _xai_chat(
    messages: list[dict[str, Any]],
    *,
    session: requests.Session,
    api_key: str,
    timeout: float,
) -> dict[str, Any] | None:
    resp = session.post(
        XAI_CHAT_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": XAI_MODEL,
            "max_tokens": ASK_XAI_MAX_TOKENS,
            "tools": XAI_TOOLS,
            "tool_choice": "auto",
            "messages": messages,
        },
        timeout=max(0.5, float(timeout)),
    )
    if getattr(resp, "status_code", 0) != 200:
        return None
    body = resp.json()
    return ((body.get("choices") or [{}])[0].get("message")) or None


def _polished_answer(
    question: str,
    scorer: TransparencyScorer,
    *,
    session: requests.Session,
    api_key: str,
    score_fn: Callable[[str], dict[str, Any]] | None,
    deadline: float,
    run: _ToolRun,
) -> str | None:
    source = client_source(scorer.client)
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT + f" Current client data_source={source}.",
        },
        {"role": "user", "content": question},
    ]
    for _round in range(ASK_MAX_TOOL_ROUNDS + 1):
        remaining = deadline - time.monotonic()
        if remaining < 1.0:
            return None
        message, err = _call_with_timeout(
            lambda: _xai_chat(
                messages, session=session, api_key=api_key, timeout=min(ASK_XAI_TIMEOUT, remaining)
            ),
            timeout=min(ASK_XAI_TIMEOUT, remaining) + 0.4,
        )
        if err or not message:
            return None
        tool_calls = message.get("tool_calls") or []
        content = (message.get("content") or "").strip()
        if tool_calls:
            messages.append(message)
            for call in tool_calls:
                fn = (call.get("function") or {}) if isinstance(call, dict) else {}
                name = str(fn.get("name") or "")
                args = _parse_tool_args(fn.get("arguments"))
                remaining = deadline - time.monotonic()
                if remaining < 0.4:
                    return None
                payload, tool_err = _call_with_timeout(
                    lambda n=name, a=args: execute_tool(
                        n, a, scorer=scorer, score_fn=score_fn
                    ),
                    timeout=min(ASK_TOOL_TIMEOUT, remaining),
                )
                if tool_err == "timeout":
                    return None
                if tool_err:
                    payload = {
                        "ok": False,
                        "tool": name,
                        "data_source": source,
                        "error": tool_err,
                    }
                if name:
                    run.used.append(name)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or name,
                        "content": _json_ok(payload if isinstance(payload, dict) else {"error": "empty"}),
                    }
                )
            continue
        if content:
            return content
        return None
    return None


def ask(
    question: str,
    scorer: TransparencyScorer,
    *,
    session: requests.Session | None = None,
    api_key: str | None = None,
    score_fn: Callable[[str], dict[str, Any]] | None = None,
    catalog_symbols: list[str] | None = None,
    budget_seconds: float = ASK_BUDGET_SECONDS,
) -> AskResult:
    """Answer one question. Never raises; falls back when the model is missing or slow."""
    client = scorer.client
    source = client_source(client)
    start = _log_start(client)
    run = _ToolRun()
    text = (question or "").strip()
    skipped: str | None = None
    polished = False
    answer = ""
    try:
        if not text:
            answer = (
                f"{honesty_line(source)} Name a ticker to ground the answer in a score. "
                "This is an automated summary, not financial advice."
            )
        else:
            key = os.environ.get("XAI_API_KEY") if api_key is None else api_key
            deadline = time.monotonic() + max(0.0, float(budget_seconds))
            if key:
                try:
                    sess = session or requests.Session()
                    polished_text = _polished_answer(
                        text,
                        scorer,
                        session=sess,
                        api_key=key,
                        score_fn=score_fn,
                        deadline=deadline,
                        run=run,
                    )
                    if polished_text:
                        answer = f"{honesty_line(source)} {polished_text}"
                        polished = True
                    else:
                        skipped = "xAI skipped or timed out — templated fallback."
                except Exception:  # noqa: BLE001
                    skipped = "xAI failed — templated fallback."
            else:
                skipped = "XAI_API_KEY missing — templated fallback."
            if not polished:
                answer = fallback_answer(
                    text,
                    scorer,
                    score_fn=score_fn,
                    catalog_symbols=catalog_symbols,
                    run=run,
                )
    except Exception as exc:  # noqa: BLE001 — UI must never crash
        skipped = "Ask RAT recovered from an internal error."
        answer = (
            f"{honesty_line(source)} Ask RAT could not finish that turn ({exc}). "
            "This is an automated summary, not financial advice."
        )
        polished = False
    calls = _calls_since(client, start)
    return AskResult(
        answer=answer,
        polished=polished,
        data_source=source,
        tools_used=tuple(run.used),
        cmc_calls=calls,
        footnote=AI_FOOTNOTE,
        skipped_reason=skipped,
        prefill=text,
    )
