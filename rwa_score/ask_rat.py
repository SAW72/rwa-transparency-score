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

from .client import (
    CMCPlanBlockedError,
    PLAN_BLOCKED_LABEL,
    market_pairs_plan_blocked,
    summarize_call_log,
)
from .explainer import (
    AI_FOOTNOTE,
    XAI_CHAT_URL,
    log_xai_failure,
    redact_xai_error,
    resolve_xai_model,
    xai_headers,
    xai_timeout,
)
from .source_label import source_kind
from .scorer import (
    ALWAYS_SELF_REPORTED,
    PILLARS,
    SOURCE_MARKET_PAIRS_PLAN_BLOCKED,
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

# One model call + live score_ticker (CMC + Chainlink) + a follow-up
# completion does not fit in 8s/15s. grok-4.3 (current 4.1 Fast alias)
# routinely exceeds that, which produced "xAI skipped or timed out".
ASK_XAI_TIMEOUT = 25.0
ASK_XAI_MAX_TOKENS = 800
ASK_MAX_TOOL_ROUNDS = 3
ASK_BUDGET_SECONDS = 55.0
ASK_TOOL_TIMEOUT = 12.0
ASK_THREAD_SLACK = 2.0

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
    "numbers. Only discuss tickers named in the user question or returned by "
    "tools — never invent symbols. Do not treat comparison words (vs, versus, "
    "compare) or pillar names (redemption, basis, PoR) as tickers. Compare "
    "prompts must score and explain only the named tickers on the requested "
    "pillars. When a score_ticker payload includes Chainlink / on-chain PoR "
    "fields (published_por_feed, por_path, por_proxy, level=on-chain PoR), "
    "you MUST cite that PoR evidence — do not omit it for greener / backing / "
    "PoR questions. If a tool result says data_source=fixture, you MUST say "
    "the answer uses bundled demo fixtures and must NOT claim live "
    "CoinMarketCap. If data_source=live, say LIVE CMC (not fixtures). Be "
    "concise (under 180 words). Not financial advice. No voice or TTS."
)

# Comparison / pillar words that look like tickers ("vs" → VS, "basis" → BASIS).
_QUESTION_NOISE = frozenset(
    {
        "VS",
        "VERSUS",
        "COMPARE",
        "COMPARISON",
        "REDEMPTION",
        "BASIS",
        "POR",
        "PROOF",
        "RESERVE",
        "RESERVES",
        "BACKING",
        "BACKED",
        "PILLAR",
        "PILLARS",
        "RIGHTS",
        "MODEL",
        "GREENER",
        "WEAKEST",
        "CHAINLINK",
        "ONCHAIN",
        "TOKENIZED",
        "STOCK",
        "STOCKS",
        "ONLY",
        "ABOUT",
        "FROM",
        "THIS",
        "THAT",
        "WITH",
        "AGAINST",
        "BETWEEN",
        "THAN",
        "LIVE",
        "FIXTURE",
        "CMC",
        "RWA",
        "RPC",
        "FEED",
        "SCORES",
        "ISSUER",
        "CROSS",
    }
)
_PILLAR_WORDS = frozenset(
    {
        "BASIS",
        "REDEMPTION",
        "BACKING",
        "RESERVES",
        "RESERVE",
        "DISCLOSURE",
        "PRICE",
        "POR",
        "PROOF",
        "INTEGRITY",
    }
)
# Answer-side noise for invented-ticker checks. Do NOT include VS — "VS scores"
# is the film-gate hallucination we must reject.
_INVENTED_IGNORE = _STOP_WORDS | _PILLAR_WORDS | frozenset(
    {
        "THIS",
        "THAT",
        "PASS",
        "MODE",
        "LIVE",
        "CHAIN",
        "READ",
        "FEED",
        "GREEN",
        "YELLOW",
        "ORANGE",
        "RED",
        "SAME",
        "BAND",
        "DEMO",
        "DATA",
        "NOT",
        "THE",
        "AND",
        "FOR",
        "HAS",
        "HAD",
        "DOES",
        "DID",
    }
)
_SCORED_SUBJECT = re.compile(
    r"(?<![A-Za-z0-9])(b[A-Za-z]{2,6}|[A-Za-z]{2,5}x?)\s+"
    r"(?:scores?|is|has|was|are|scored)\b",
    re.IGNORECASE,
)
_ON_SUBJECT = re.compile(
    r"\b(?:on|for)\s+(b[A-Za-z]{2,6}|[A-Za-z]{2,5}x?)\b",
    re.IGNORECASE,
)
ON_CHAIN_POR_LEVEL = "on-chain PoR"


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
    drifted: str | None = None
    transport: str | None = None


def client_source(client: object) -> str:
    """Canonical fixture vs live label from the existing client."""
    return source_kind(getattr(client, "source", ""))


def honesty_line(source: str) -> str:
    """First-line mode cue. Fixture/unknown never claim live CMC."""
    kind = source_kind(source)
    if kind == "fixture":
        return "FIXTURE (bundled demo data — not live CoinMarketCap)."
    if kind == "live":
        return "LIVE CoinMarketCap (not fixtures)."
    return "Data source not confirmed — not labeled as live CoinMarketCap."


def format_ask_cmc_lines(block: dict[str, Any] | None) -> list[str]:
    """Evidence strip for this chat turn. Fixture never says live."""
    payload = block or {}
    source = source_kind(payload.get("source"))
    if source == "fixture":
        header = "CMC calls this turn — Fixture (bundled demo, **not** live CMC)"
    elif source == "live":
        header = "CMC calls this turn — Live CMC (not fixtures)"
    else:
        header = "CMC calls this turn — source not confirmed (not labeled live CMC)"
    lines = [header]
    endpoints = payload.get("endpoints") or []
    if not endpoints:
        lines.append("No CMC/fixture directory calls recorded on this turn.")
        return lines
    for row in endpoints:
        endpoint = row.get("endpoint") or ""
        raw_source = row.get("source")
        row_kind = source_kind(raw_source) if raw_source else source
        via = row.get("via") or ("fixture" if row_kind == "fixture" else "network")
        if row_kind == "fixture" or source == "fixture":
            tag = "fixture"
        elif row_kind == "live" or source == "live":
            tag = "cache" if row.get("cached") or via == "cache" else "live"
        else:
            tag = "unconfirmed"
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
        if symbol.upper() in _STOP_WORDS or symbol.upper() in _QUESTION_NOISE:
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


def is_compare_question(question: str) -> bool:
    """True when the user asked to compare named tickers (vs / versus / compare)."""
    text = f" {(question or '').lower()} "
    return bool(
        " vs " in text
        or " vs. " in text
        or " versus " in text
        or re.search(r"\bcompar(?:e|ing|ison)\b", text)
    )


def requested_pillars(question: str) -> list[str]:
    """Pillars named in the question. Empty means no pillar filter."""
    text = (question or "").lower()
    found: list[str] = []
    checks = (
        ("redemption", ("redemption", "redeem")),
        ("basis", ("cross-issuer", "cross issuer", "basis")),
        ("reserves", ("proof of reserve", "por", "reserves", "reserve")),
        ("backing", ("backing", "backed")),
        ("disclosure", ("disclosure",)),
        ("price", ("price integrity", "price")),
    )
    for key, needles in checks:
        if any(needle in text for needle in needles):
            found.append(key)
    return found


def wants_por(question: str) -> bool:
    """Greener / backing / PoR questions must surface score-card PoR when present."""
    text = (question or "").lower()
    return any(
        needle in text
        for needle in (
            "por",
            "proof of reserve",
            "on-chain",
            "on chain",
            "chainlink",
            "backing",
            "greener",
            "green",
        )
    )


def _por_summary(compact: dict[str, Any]) -> dict[str, Any]:
    """Chainlink / on-chain PoR fields from a compact score_ticker payload."""
    existing = compact.get("por")
    if isinstance(existing, dict) and existing.get("present"):
        return existing
    reserves = (compact.get("verification") or {}).get("reserves") or {}
    if not isinstance(reserves, dict):
        return {}
    feed = reserves.get("published_por_feed")
    path = reserves.get("por_path")
    proxy = reserves.get("por_proxy")
    chain = reserves.get("por_chain")
    level = str(reserves.get("level") or "")
    source = str(reserves.get("source") or "")
    on_chain = level == ON_CHAIN_POR_LEVEL or (
        source == "chainlink_por" and path != "fixture_labeled_skip"
    )
    if not feed and not on_chain and path != "fixture_labeled_skip":
        return {}
    return {
        "present": True,
        "on_chain": bool(on_chain),
        "level": level,
        "source": source,
        "feed": feed,
        "path": path,
        "proxy": proxy,
        "chain": chain,
        "reserves": reserves.get("por_reserves"),
        "unit": reserves.get("por_unit"),
        "evidence": (reserves.get("evidence") or "")[:240],
    }


def _por_line(compact: dict[str, Any]) -> str:
    """Templated PoR sentence. Empty when the payload has no PoR fields."""
    por = _por_summary(compact)
    if not por:
        return ""
    ticker = compact.get("ticker")
    feed = por.get("feed") or ticker
    chain = por.get("chain") or ""
    proxy = por.get("proxy") or ""
    loc = f" on {chain}" if chain else ""
    proxy_bit = f" ({proxy})" if proxy else ""
    if por.get("on_chain"):
        extra = ""
        reserves = por.get("reserves")
        unit = por.get("unit") or ""
        if reserves is not None:
            extra = f" Reserves reading {reserves}" + (f" {unit}" if unit else "") + "."
        return (
            f"{ticker} Proof of reserves is on-chain PoR via Chainlink "
            f"{feed}{loc}{proxy_bit}.{extra}"
        )
    if por.get("path") == "fixture_labeled_skip":
        return (
            f"{ticker} has a published Chainlink PoR feed ({feed}{loc}{proxy_bit}) "
            "— live RPC skipped in fixture/offline mode (not an on-chain read)."
        )
    evidence = str(por.get("evidence") or "")
    if "chainlink" in evidence.lower() or "por" in evidence.lower():
        return f"{ticker} PoR evidence: {evidence[:220]}"
    return f"{ticker} published Chainlink PoR feed: {feed}{loc}{proxy_bit}."


def _append_por_lines(parts: list[str], scores: dict[str, dict[str, Any]]) -> None:
    for compact in scores.values():
        line = _por_line(compact)
        if line and line not in parts:
            parts.append(line)


def _compare_lines(
    scores: dict[str, dict[str, Any]], pillars: list[str]
) -> str:
    """Deterministic compare of only the named/scored tickers."""
    names = [str(compact.get("ticker") or key) for key, compact in scores.items()]
    head = "Comparing only " + " and ".join(names)
    keys = [key for key in pillars if key in WEIGHTS] or []
    if keys:
        labels = [PILLARS[key]["label"] for key in keys]
        head += " on " + " / ".join(labels)
    head += "."
    bits = [head]
    for key in keys or list(WEIGHTS):
        label = PILLARS[key]["label"]
        pieces = []
        for compact in scores.values():
            subs = compact.get("subscores") or {}
            raw = subs.get(key)
            ver = (compact.get("verification") or {}).get(key) or {}
            level = ver.get("level") or ""
            score_bit = f"{float(raw):.1f}" if raw is not None else "n/a"
            extra = f" ({level})" if level else ""
            pieces.append(f"{compact.get('ticker')} {score_bit}{extra}")
        bits.append(f"{label}: " + " vs ".join(pieces) + ".")
    return " ".join(bits)


def invented_scored_subjects(answer: str, allowed: set[str]) -> list[str]:
    """Tickers the answer treats as subjects that were not named / tool-returned."""
    allowed_norm = {normalize_ticker(symbol) for symbol in allowed if symbol}
    extra: list[str] = []
    seen: set[str] = set()
    for regex in (_SCORED_SUBJECT, _ON_SUBJECT):
        for match in regex.finditer(answer or ""):
            symbol = normalize_ticker(match.group(1))
            if not symbol:
                continue
            upper = symbol.upper()
            if upper in _INVENTED_IGNORE:
                continue
            if symbol in allowed_norm:
                continue
            if symbol not in seen:
                seen.add(symbol)
                extra.append(symbol)
    return extra


def _answer_cites_por(answer: str) -> bool:
    text = (answer or "").lower()
    return any(
        token in text
        for token in (
            "on-chain por",
            "on chain por",
            "chainlink por",
            "proof of reserve",
            "published chainlink",
            "published por",
        )
    )


def _symbols_from_payload(payload: dict[str, Any]) -> list[str]:
    found: list[str] = []
    if not isinstance(payload, dict):
        return found
    for key in ("ticker", "symbol"):
        if payload.get(key):
            found.append(str(payload[key]))
    for nest in ("map", "info"):
        block = payload.get(nest) or {}
        if isinstance(block, dict) and block.get("symbol"):
            found.append(str(block["symbol"]))
    for row in payload.get("rwa_assets") or []:
        if isinstance(row, dict) and row.get("symbol"):
            found.append(str(row["symbol"]))
    for symbol in payload.get("token_symbols") or []:
        found.append(str(symbol))
    out: list[str] = []
    seen: set[str] = set()
    for raw in found:
        token = normalize_ticker(raw)
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


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
            basis_meta = report.get("basis") or {}
            if key == "basis" and (
                basis_meta.get("plan_blocked")
                or (block.get("meta") or {}).get("plan_blocked")
                or source == SOURCE_MARKET_PAIRS_PLAN_BLOCKED
            ):
                kind = PLAN_BLOCKED_LABEL
            else:
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
        feed = meta.get("published_por_feed")
        if not feed and key == "reserves":
            feed = meta.get("symbol")
        compact_ver[key] = {
            "level": block.get("level"),
            "source": block.get("source"),
            "evidence": (block.get("evidence") or "")[:240],
            "published_por_feed": feed,
            "por_path": meta.get("por_path"),
            "por_proxy": meta.get("por_proxy") or meta.get("proxy"),
            "por_chain": meta.get("por_chain") or meta.get("chain"),
            "por_reserves": meta.get("reserves"),
            "por_unit": meta.get("unit"),
        }
    data_source = source_kind(report.get("data_source"))
    compact = {
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
    por = _por_summary(compact)
    if por:
        compact["por"] = por
    basis = report.get("basis") or {}
    compact["basis"] = {
        "plan_blocked": bool(basis.get("plan_blocked")),
        "available": basis.get("available"),
        "source": basis.get("source"),
        "label": PLAN_BLOCKED_LABEL if basis.get("plan_blocked") else None,
    }
    return compact


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


def _plan_blocked_pairs_result(
    source: str,
    *,
    symbol: str | None = None,
    rwa_id: int | None = None,
    data: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    payload = data if isinstance(data, dict) else {}
    return {
        "ok": False,
        "tool": TOOL_MARKET_PAIRS,
        "data_source": source,
        "plan_blocked": True,
        "unavailable": True,
        "label": PLAN_BLOCKED_LABEL,
        "symbol": payload.get("symbol") or symbol,
        "rwa_id": payload.get("rwa_id") if payload.get("rwa_id") is not None else rwa_id,
        "num_market_pairs": 0,
        "sample_pairs": [],
        "error": error
        or payload.get("error_message")
        or f"CMC market-pairs {PLAN_BLOCKED_LABEL} — not live market-pairs data.",
    }


def _run_market_pairs(
    client: object, *, symbol: str | None = None, rwa_id: int | None = None
) -> dict[str, Any]:
    source = client_source(client)
    try:
        if rwa_id is not None:
            data = client.market_pairs(rwa_id=int(rwa_id))
        else:
            data = client.market_pairs(symbol=normalize_ticker(symbol or ""))
    except CMCPlanBlockedError as exc:
        return _plan_blocked_pairs_result(
            source, symbol=symbol, rwa_id=rwa_id, error=str(exc)
        )
    if market_pairs_plan_blocked(data):
        return _plan_blocked_pairs_result(
            source, symbol=symbol, rwa_id=rwa_id, data=data
        )
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
            "description": (
                "Score a ticker named in the user question with TransparencyScorer "
                "(six pillars + verification, including Chainlink PoR fields when "
                "the report has them). Do not invent symbols; never score 'vs'."
            ),
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
    allowed_symbols: set[str] | None = None,
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
            ticker = normalize_ticker(str(args.get("symbol") or ""))
            if allowed_symbols is not None:
                allowed_norm = {normalize_ticker(item) for item in allowed_symbols if item}
                if ticker not in allowed_norm:
                    return {
                        "ok": False,
                        "tool": TOOL_SCORE_TICKER,
                        "data_source": client_source(client),
                        "ticker": ticker,
                        "error": (
                            f"Refusing to score {ticker}: not named in the user "
                            "question or prior tool results."
                        ),
                    }
            return _run_score_ticker(scorer, ticker, score_fn)
    except Exception as exc:  # noqa: BLE001 — chat must stay up
        payload = {
            "ok": False,
            "tool": name,
            "data_source": client_source(client),
            "error": str(exc),
        }
        if isinstance(exc, CMCPlanBlockedError) or (
            name == TOOL_MARKET_PAIRS and "1006" in str(exc)
        ):
            payload["plan_blocked"] = True
            payload["unavailable"] = True
            payload["label"] = PLAN_BLOCKED_LABEL
        return payload
    return {
        "ok": False,
        "tool": name,
        "data_source": client_source(client),
        "error": f"Unknown tool {name}",
    }


def _skip_reason_from_error(err: str | None) -> str:
    """Map a transport/thread error to a user-facing skip caption."""
    detail = redact_xai_error(err or "no model reply")
    lowered = detail.lower()
    if not err or err == "timeout" or "timeout" in lowered or "timed out" in lowered:
        return "xAI timed out — templated fallback."
    if "401" in detail or "unauthorized" in lowered or "incorrect api key" in lowered:
        return f"xAI unauthorized ({detail}) — templated fallback."
    if "400" in detail or ("invalid" in lowered and "model" in lowered):
        return f"xAI rejected the request ({detail}) — templated fallback."
    return f"xAI failed ({detail}) — templated fallback."


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
        exc = box["err"]
        if isinstance(exc, (TimeoutError, requests.Timeout, requests.ConnectTimeout)):
            return None, "timeout"
        return None, str(exc)
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
    blocked = [row["label"] for row in rows if row.get("kind") == PLAN_BLOCKED_LABEL]
    other = [
        row["label"]
        for row in rows
        if row.get("kind") not in {"CMC field (self-reported)", PLAN_BLOCKED_LABEL}
    ]
    bits = []
    if always:
        bits.append(
            "Always CMC field (self-reported): " + ", ".join(always) + "."
        )
    if blocked:
        bits.append(
            "Cross-issuer basis is plan-blocked / unavailable "
            "(CMC market-pairs not on this plan — not live market-pairs data)."
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
    allowed = set(tickers)
    scores: dict[str, dict[str, Any]] = {}
    for symbol in tickers:
        payload = execute_tool(
            TOOL_SCORE_TICKER,
            {"symbol": symbol},
            scorer=scorer,
            score_fn=score_fn,
            allowed_symbols=allowed or None,
        )
        journal.used.append(TOOL_SCORE_TICKER)
        journal.results[symbol] = payload
        if payload.get("ok"):
            scores[symbol] = payload
    parts = [honesty_line(source)]
    q = (question or "").lower()
    compare = is_compare_question(question)
    pillars = requested_pillars(question)
    if compare and scores:
        if "green" in q and len(scores) >= 2:
            ordered = list(scores.values())
            parts.append(_greener_line(ordered[0], ordered[1]))
        parts.append(_compare_lines(scores, pillars))
        if wants_por(question):
            _append_por_lines(parts, scores)
        elif any(_por_summary(row) for row in scores.values()):
            _append_por_lines(parts, scores)
    elif "green" in q and len(scores) >= 2:
        ordered = list(scores.values())
        parts.append(_greener_line(ordered[0], ordered[1]))
        _append_por_lines(parts, scores)
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
    elif wants_por(question) and scores:
        for compact in scores.values():
            parts.append(
                f"{compact.get('ticker')} scores {compact.get('score')} "
                f"({compact.get('band')})."
            )
            line = _por_line(compact)
            if line:
                parts.append(line)
            else:
                parts.append(
                    f"No Chainlink / on-chain PoR fields on the "
                    f"{compact.get('ticker')} score payload this pass."
                )
    elif scores:
        for compact in scores.values():
            parts.append(
                f"{compact.get('ticker')} scores {compact.get('score')} "
                f"({compact.get('band')})."
            )
            parts.append(_weakest_line(compact))
            if _por_summary(compact) and wants_por(question):
                line = _por_line(compact)
                if line:
                    parts.append(line)
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
) -> dict[str, Any]:
    resp = session.post(
        XAI_CHAT_URL,
        headers=xai_headers(api_key),
        json={
            "model": resolve_xai_model(),
            "max_tokens": ASK_XAI_MAX_TOKENS,
            "tools": XAI_TOOLS,
            "tool_choice": "auto",
            "messages": messages,
        },
        timeout=xai_timeout(max(0.5, float(timeout))),
    )
    status = getattr(resp, "status_code", 0)
    if status != 200:
        body_text = ""
        try:
            body_text = resp.text or ""
        except Exception:  # noqa: BLE001
            body_text = ""
        raise RuntimeError(f"HTTP {status}: {redact_xai_error(body_text)}")
    body = resp.json()
    message = ((body.get("choices") or [{}])[0].get("message")) or None
    if not message:
        raise RuntimeError("empty model reply")
    return message


def _polished_answer(
    question: str,
    scorer: TransparencyScorer,
    *,
    session: requests.Session,
    api_key: str,
    score_fn: Callable[[str], dict[str, Any]] | None,
    deadline: float,
    run: _ToolRun,
    catalog_symbols: list[str] | None = None,
) -> str | None:
    source = client_source(scorer.client)
    named = extract_tickers(question, catalog_symbols)
    compare = is_compare_question(question)
    allowed: set[str] | None = set(named) if named else None
    grounded_scores: dict[str, dict[str, Any]] = {}
    extra_system = ""
    if compare and named:
        extra_system = (
            f" Compare only {', '.join(named)}. Never invent a third symbol "
            "(including 'VS'). Cite redemption/basis/PoR from score_ticker when asked."
        )
    elif wants_por(question):
        extra_system = (
            " Cite Chainlink / on-chain PoR fields from score_ticker when present."
        )
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
            + f" Current client data_source={source}."
            + extra_system,
        },
        {"role": "user", "content": question},
    ]
    for _round in range(ASK_MAX_TOOL_ROUNDS + 1):
        remaining = deadline - time.monotonic()
        if remaining < 1.0:
            run.transport = "xAI budget exhausted — templated fallback."
            return None
        read_budget = min(ASK_XAI_TIMEOUT, remaining)
        message, err = _call_with_timeout(
            lambda: _xai_chat(
                messages, session=session, api_key=api_key, timeout=read_budget
            ),
            timeout=read_budget + ASK_THREAD_SLACK,
        )
        if err or not message:
            run.transport = _skip_reason_from_error(err)
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
                    run.transport = "xAI budget exhausted — templated fallback."
                    return None
                payload, tool_err = _call_with_timeout(
                    lambda n=name, a=args: execute_tool(
                        n,
                        a,
                        scorer=scorer,
                        score_fn=score_fn,
                        allowed_symbols=allowed,
                    ),
                    timeout=min(ASK_TOOL_TIMEOUT, remaining),
                )
                if tool_err == "timeout":
                    run.transport = "score tool timed out — templated fallback."
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
                if isinstance(payload, dict):
                    if payload.get("ok") and name == TOOL_SCORE_TICKER:
                        ticker = str(payload.get("ticker") or "")
                        if ticker:
                            run.results[ticker] = payload
                            grounded_scores[ticker] = payload
                    if allowed is not None and not compare:
                        for symbol in _symbols_from_payload(payload):
                            allowed.add(symbol)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or name,
                        "content": _json_ok(payload if isinstance(payload, dict) else {"error": "empty"}),
                    }
                )
            continue
        if content:
            if (compare or wants_por(question)) and not grounded_scores:
                run.drifted = (
                    "Model skipped tools on a compare/PoR question — templated fallback."
                )
                return None
            permit = set(named)
            permit.update(grounded_scores)
            if allowed:
                permit.update(allowed)
            invented = invented_scored_subjects(content, permit)
            if invented:
                run.drifted = (
                    "Model invented ticker(s) "
                    + ", ".join(invented)
                    + " — templated fallback."
                )
                return None
            needs_por = wants_por(question) and any(
                _por_summary(row) for row in grounded_scores.values()
            )
            if needs_por and not _answer_cites_por(content):
                run.drifted = (
                    "Model omitted score-payload PoR — templated fallback."
                )
                return None
            if compare and named:
                extra = invented_scored_subjects(content, set(named))
                if extra:
                    run.drifted = (
                        "Compare answer drifted off named tickers — templated fallback."
                    )
                    return None
            return content
        run.transport = "xAI returned an empty reply — templated fallback."
        return None
    run.transport = "xAI tool loop exhausted — templated fallback."
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
                        catalog_symbols=catalog_symbols,
                    )
                    if polished_text:
                        answer = f"{honesty_line(source)} {polished_text}"
                        polished = True
                    elif run.drifted:
                        skipped = run.drifted
                    elif run.transport:
                        skipped = run.transport
                        log_xai_failure(run.transport)
                    else:
                        skipped = "xAI skipped or timed out — templated fallback."
                        log_xai_failure(skipped)
                except Exception as exc:  # noqa: BLE001
                    skipped = _skip_reason_from_error(str(exc))
                    log_xai_failure(skipped)
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
