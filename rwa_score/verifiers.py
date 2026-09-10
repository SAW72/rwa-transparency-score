"""Attestation / proof-of-reserves verifiers for transparency pillars.

Live sources (no paid APIs, ``requests`` only):
  - Backed / xStocks public PoR JSON
  - Dinari dShares marketing page scrape (attestation pending — no signed URL yet)

Failed verifiers never fail silently: the scorer must surface the error and
fall back to the name heuristic labeled **"heuristic fallback"**.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

import requests

from .issuer_registry import HEURISTIC_NOTE, classify

BACKED_POR_URL = "https://api.xstocks.fi/api/v2/public/proof-of-reserves/{symbol}"
DINARI_DSHARES_URL = "https://dinari.com/dshares"

CACHE_TTL_SECONDS = 3600.0
REQUEST_TIMEOUT = 15

# Issuer-name keywords → verifier id. Unknown issuers stay heuristic-only.
# Allowlist phrases only — never the bare token "backed" (false-positives
# like "Not Backed At All"). Word-boundary match + negative-token reject.
# "robinhood" is a whole-word allowlist hit (same harden as Backed).
ISSUER_VERIFIER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "backed": ("backed finance", "xstocks", "xstock"),
    "dinari": ("dinari", "dshares", "dshare"),
    "robinhood": ("robinhood",),
}

_NEGATIVE_VERIFIER_RE = re.compile(r"\bunbacked\b|\bnot backed\b|\banti\b")

BIG4_PATTERNS = (
    "big 4",
    "big-4",
    "big4",
    "deloitte",
    "pwc",
    "pricewaterhouse",
    "ernst & young",
    "ernst and young",
    " kpmg",
    "kpmg ",
    "ey ",
)


class VerificationLevel(str, Enum):
    """Public badge labels shown per pillar."""

    SELF_REPORTED = "self-reported"
    ON_CHAIN_POR = "on-chain PoR"
    ATTESTED = "attested"
    EXAMINED = "examined"


@dataclass
class VerificationResult:
    """Outcome of one pillar verification attempt."""

    score: float
    level: VerificationLevel
    evidence: str
    source: str
    notes: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "evidence": self.evidence,
            "source": self.source,
            "notes": list(self.notes),
            "ok": self.ok,
            "error": self.error,
            "meta": dict(self.meta),
            "score": round(self.score, 1),
        }


class Verifier(Protocol):
    name: str

    def verify_backing(self, *, ticker: str, issuer_name: str) -> VerificationResult: ...

    def verify_reserves(self, *, ticker: str, issuer_name: str) -> VerificationResult: ...


def _heuristic_scores(issuer_name: str) -> dict[str, float]:
    flags = classify(issuer_name)
    return {
        "backing": 90.0 if flags["backed"] else 35.0,
        "reserves": 90.0 if flags["audited"] else 30.0,
        "redemption": 85.0 if flags["redeemable"] else 25.0,
    }


def heuristic_result(
    pillar: str,
    issuer_name: str,
    *,
    reason: str,
    error: str | None = None,
) -> VerificationResult:
    """Name-match fallback. Always labeled ``heuristic fallback``."""
    scores = _heuristic_scores(issuer_name)
    score = scores.get(pillar, 30.0)
    flags = classify(issuer_name)
    matched = {
        "backing": flags["backed"],
        "reserves": flags["audited"],
        "redemption": flags["redeemable"],
    }.get(pillar, False)
    evidence = (
        f"heuristic fallback: issuer '{issuer_name or 'unknown'}' "
        f"{'matched' if matched else 'did not match'} the {pillar} name list. {reason}"
    )
    notes = ["heuristic fallback", HEURISTIC_NOTE]
    if error:
        notes.append(f"Verifier error (not dropped): {error}")
    return VerificationResult(
        score=score,
        level=VerificationLevel.SELF_REPORTED,
        evidence=evidence,
        source="heuristic_fallback",
        notes=notes,
        ok=False if error else True,
        error=error,
        meta={"pillar": pillar, "matched": matched, "issuer": issuer_name},
    )


def resolve_verifier_id(issuer_name: str) -> str | None:
    """Map issuer keywords to a verifier id, or None for unknown issuers.

    Mirrors ``issuer_registry.classify`` hardening: negatives reject first;
    positives require a word-boundary phrase from the allowlist.
    """
    name = re.sub(r"[\s_\-]+", " ", (issuer_name or "").strip().lower()).strip()
    if not name or _NEGATIVE_VERIFIER_RE.search(name):
        return None
    for verifier_id, keywords in ISSUER_VERIFIER_KEYWORDS.items():
        if any(re.search(r"\b" + re.escape(k) + r"\b", name) for k in keywords):
            return verifier_id
    return None


def por_score_from_ratio(ratio: float) -> float:
    if ratio >= 0.999:
        return 95.0
    if ratio >= 0.99:
        return 80.0
    if ratio >= 0.95:
        return 60.0
    return 30.0


def _normalize_por_symbol(ticker: str) -> list[str]:
    """xStocks PoR symbols are case-sensitive (``AAPLx``, not ``AAPLX``).

    Try the raw ticker, uppercase, and the ``{TICKER}x`` form used by the API.
    """
    raw = (ticker or "").strip()
    if not raw:
        return []
    candidates: list[str] = []

    def add(symbol: str) -> None:
        if symbol and symbol not in candidates:
            candidates.append(symbol)

    add(raw)
    add(raw.upper())
    add(raw.lower())
    if len(raw) > 1 and raw[-1].lower() == "x":
        add(raw[:-1].upper() + "x")
    else:
        add(raw.upper() + "x")
    return candidates


class BackedVerifier:
    """Public xStocks / Backed proof-of-reserves JSON (no auth)."""

    name = "backed"

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        cache_ttl: float = CACHE_TTL_SECONDS,
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        self.session = session or requests.Session()
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        # symbol -> (monotonic_ts, payload_or_exc_marker)
        self._cache: dict[str, tuple[float, Any]] = {}

    def _get_por(self, symbol: str) -> dict[str, Any]:
        now = time.monotonic()
        hit = self._cache.get(symbol)
        if hit is not None and (now - hit[0]) <= self.cache_ttl:
            cached = hit[1]
            if isinstance(cached, Exception):
                raise cached
            return dict(cached)

        url = BACKED_POR_URL.format(symbol=symbol)
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                exc = RuntimeError(f"PoR HTTP {resp.status_code} for {symbol}: {resp.text[:200]}")
                self._cache[symbol] = (now, exc)
                raise exc
            payload = resp.json()
            if not isinstance(payload, dict):
                exc = RuntimeError(f"PoR payload for {symbol} was not a JSON object")
                self._cache[symbol] = (now, exc)
                raise exc
            self._cache[symbol] = (now, payload)
            return dict(payload)
        except Exception as exc:  # noqa: BLE001 — cache + re-raise for fallback path
            if symbol not in self._cache or not isinstance(self._cache[symbol][1], Exception):
                self._cache[symbol] = (now, exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
            raise

    def _fetch_first_por(self, ticker: str) -> tuple[str, dict[str, Any]]:
        errors: list[str] = []
        for symbol in _normalize_por_symbol(ticker):
            try:
                return symbol, self._get_por(symbol)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{symbol}: {exc}")
        raise RuntimeError("; ".join(errors) if errors else f"No PoR symbol candidates for {ticker}")

    def _from_por(
        self,
        *,
        ticker: str,
        issuer_name: str,
        pillar: str,
    ) -> VerificationResult:
        try:
            symbol, payload = self._fetch_first_por(ticker)
        except Exception as exc:  # noqa: BLE001 — never silent
            return heuristic_result(
                pillar,
                issuer_name,
                reason=f"Backed PoR call failed for {ticker}.",
                error=str(exc),
            )

        try:
            shares_held = float(payload.get("sharesHeld"))
            circulating = float(payload.get("circulatingSupply"))
        except (TypeError, ValueError) as exc:
            return heuristic_result(
                pillar,
                issuer_name,
                reason=f"Backed PoR payload for {ticker} missing numeric fields.",
                error=str(exc),
            )
        if circulating <= 0:
            return heuristic_result(
                pillar,
                issuer_name,
                reason=f"Backed PoR circulatingSupply was {circulating}.",
                error="circulatingSupply <= 0",
            )

        ratio = shares_held / circulating
        score = por_score_from_ratio(ratio)
        holdings = payload.get("holdings") or []
        providers = sorted(
            {
                str(h.get("provider"))
                for h in holdings
                if isinstance(h, dict) and h.get("provider")
            }
        )
        evidence = (
            f"api.xstocks.fi PoR {symbol}: sharesHeld={shares_held} / "
            f"circulatingSupply={circulating} → collateralization_ratio={ratio:.6f} "
            f"(score {score:.0f})"
            + (f"; custodians={', '.join(providers)}" if providers else "")
        )
        notes = [
            f"verification={VerificationLevel.ON_CHAIN_POR.value}",
            f"evidence source: {BACKED_POR_URL.format(symbol=symbol)}",
        ]
        return VerificationResult(
            score=score,
            level=VerificationLevel.ON_CHAIN_POR,
            evidence=evidence,
            source="backed_por",
            notes=notes,
            ok=True,
            meta={
                "symbol": symbol,
                "shares_held": shares_held,
                "circulating_supply": circulating,
                "collateralization_ratio": ratio,
                "providers": providers,
                "timestamp": payload.get("timestamp"),
            },
        )

    def verify_backing(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        # Live PoR is primarily a reserves signal; treat solid collateralization as
        # corroboration of a real share-backed model.
        result = self._from_por(ticker=ticker, issuer_name=issuer_name, pillar="backing")
        if result.ok and result.level == VerificationLevel.ON_CHAIN_POR:
            # Cap backing slightly below reserves when ratio is excellent — still high.
            result.score = min(result.score, 92.0)
            result.notes.append("Backing corroborated by on-chain PoR collateralization.")
        return result

    def verify_reserves(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        return self._from_por(ticker=ticker, issuer_name=issuer_name, pillar="reserves")


class DinariVerifier:
    """Scrape Dinari dShares page for audit / custody / 1:1 claims.

    Label remains **attestation pending** until a signed report URL is published.
    """

    name = "dinari"

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        cache_ttl: float = CACHE_TTL_SECONDS,
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        self.session = session or requests.Session()
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self._cache: tuple[float, str] | None = None

    def _fetch_html(self) -> str:
        now = time.monotonic()
        if self._cache is not None and (now - self._cache[0]) <= self.cache_ttl:
            return self._cache[1]
        resp = self.session.get(
            DINARI_DSHARES_URL,
            timeout=self.timeout,
            headers={"User-Agent": "rwa-transparency-score/0.3 (+hackathon demo)"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Dinari page HTTP {resp.status_code}")
        html = resp.text or ""
        self._cache = (now, html)
        return html

    @staticmethod
    def _parse_claims(html: str) -> dict[str, Any]:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        lower = text.lower()

        big4_hits = [p.strip() for p in BIG4_PATTERNS if p in lower]
        # Prefer an explicit firm name when present; else "Big 4" phrasing.
        audit_firm: str | None = None
        for firm in ("deloitte", "pwc", "pricewaterhousecoopers", "kpmg", "ernst & young", "ernst and young"):
            if firm in lower:
                audit_firm = firm.title().replace("Pwc", "PwC").replace("Ernst & Young", "Ernst & Young")
                break
        if audit_firm is None and big4_hits:
            audit_firm = "Big 4 accounting firm (unnamed on page)"

        has_alpaca = "alpaca" in lower
        has_one_to_one = bool(re.search(r"\b1\s*:\s*1\b", text)) or "backed 1:1" in lower

        return {
            "audit_firm": audit_firm,
            "has_big4": bool(big4_hits),
            "has_alpaca": has_alpaca,
            "has_one_to_one": has_one_to_one,
            "custody_provider": "Alpaca" if has_alpaca else None,
        }

    def _verify_pillar(
        self,
        *,
        ticker: str,
        issuer_name: str,
        pillar: str,
    ) -> VerificationResult:
        try:
            html = self._fetch_html()
            claims = self._parse_claims(html)
        except Exception as exc:  # noqa: BLE001 — never silent
            return heuristic_result(
                pillar,
                issuer_name,
                reason=f"Dinari dShares scrape failed for {ticker}.",
                error=str(exc),
            )

        all_present = bool(claims["has_big4"] and claims["has_alpaca"] and claims["has_one_to_one"])
        if all_present:
            score = 85.0
            evidence = (
                f"dinari.com/dshares: audit={claims['audit_firm']}; "
                f"custody={claims['custody_provider']}; 1:1 claim present. "
                "attestation pending — no signed report URL yet."
            )
            notes = [
                "attestation pending",
                f"verification={VerificationLevel.ATTESTED.value}",
                f"evidence source: {DINARI_DSHARES_URL}",
            ]
            return VerificationResult(
                score=score,
                level=VerificationLevel.ATTESTED,
                evidence=evidence,
                source="dinari_dshares",
                notes=notes,
                ok=True,
                meta=claims,
            )

        missing = []
        if not claims["has_big4"]:
            missing.append("Big-4 / named audit firm")
        if not claims["has_alpaca"]:
            missing.append("Alpaca custody")
        if not claims["has_one_to_one"]:
            missing.append("1:1 claim")
        return heuristic_result(
            pillar,
            issuer_name,
            reason=(
                "Dinari page scraped but missing required claims: "
                + ", ".join(missing)
                + ". attestation pending."
            ),
            error=f"incomplete attestation signals: {', '.join(missing)}",
        )

    def verify_backing(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        return self._verify_pillar(ticker=ticker, issuer_name=issuer_name, pillar="backing")

    def verify_reserves(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        return self._verify_pillar(ticker=ticker, issuer_name=issuer_name, pillar="reserves")


class RobinhoodVerifier:
    """Static self-reported scores for Robinhood tokenized stocks.

    These tokens are debt securities — creditors of Robinhood Assets Jersey,
    not shareholders of the listed company. CMC labels the issuer "Robinhood".
    Self-reported 1:1, no public proof of reserves. A dedicated verifier
    beats the unknown-issuer heuristic; the low scores are intentional.
    """

    name = "robinhood"

    def verify_backing(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        return VerificationResult(
            score=55.0,
            level=VerificationLevel.SELF_REPORTED,
            evidence="Robinhood 1:1 claim, no public PoR",
            source="robinhood",
            notes=[
                f"verification={VerificationLevel.SELF_REPORTED.value}",
                "Debt security — creditor of Robinhood Assets Jersey, not a shareholder.",
            ],
            ok=True,
            meta={"ticker": ticker, "issuer": issuer_name, "wrapper": "debt"},
        )

    def verify_reserves(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        return VerificationResult(
            score=40.0,
            level=VerificationLevel.SELF_REPORTED,
            evidence="no independent attestation",
            source="robinhood",
            notes=[
                f"verification={VerificationLevel.SELF_REPORTED.value}",
                "No public independent attestation or on-chain PoR.",
            ],
            ok=True,
            meta={"ticker": ticker, "issuer": issuer_name, "wrapper": "debt"},
        )

    def verify_redemption(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        return VerificationResult(
            score=35.0,
            level=VerificationLevel.SELF_REPORTED,
            evidence="debt wrapper, creditor claim only",
            source="robinhood",
            notes=[
                f"verification={VerificationLevel.SELF_REPORTED.value}",
                "Redemption is a creditor claim on the issuer, not the listed share.",
            ],
            ok=True,
            meta={"ticker": ticker, "issuer": issuer_name, "wrapper": "debt"},
        )


def build_default_verifiers(
    session: requests.Session | None = None,
) -> dict[str, Verifier]:
    return {
        "backed": BackedVerifier(session=session),
        "dinari": DinariVerifier(session=session),
        "robinhood": RobinhoodVerifier(),
    }


def get_verifier_for_issuer(
    issuer_name: str,
    registry: dict[str, Verifier] | None = None,
) -> Verifier | None:
    vid = resolve_verifier_id(issuer_name)
    if vid is None:
        return None
    reg = registry if registry is not None else build_default_verifiers()
    return reg.get(vid)
