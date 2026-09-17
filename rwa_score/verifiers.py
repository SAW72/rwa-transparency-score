"""Attestation / proof-of-reserves verifiers for transparency pillars.

Live sources (no paid APIs, ``requests`` only):
  - Backed / xStocks via Chainlink Proof of Reserve (on-chain AggregatorV3)
  - Backed / xStocks redemption via public issuer docs (markdown, no JS)
  - Dinari dShares marketing page scrape (attestation pending — no signed URL yet)
  - Dinari redemption via public dShare docs (mint/burn = issuance/redemption)

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

from .chainlink_por import (
    CHAINLINK_SMARTDATA_DOCS,
    ChainlinkPorClient,
    PorFeed,
    PorReading,
    resolve_por_feed,
)
from .issuer_registry import HEURISTIC_NOTE, classify

DINARI_DSHARES_URL = "https://dinari.com/dshares"
# Public markdown — not the JS marketing shells. Do not invent claims.
BACKED_REDEMPTION_DOCS_URL = (
    "https://docs.xstocks.fi/docs/issuance-and-redemption.md"
)
BACKED_INKIND_DOCS_URL = (
    "https://docs.xstocks.fi/docs/issuance-and-redemption/in-kind-flow-xport.md"
)
DINARI_DSHARE_DOCS_URL = "https://docs.dinari.com/docs/what-is-dshare.md"

# Documented cash / burn-on-sell redemption is not in-kind share delivery.
CASH_REDEMPTION_SCORE = 80.0
INKIND_REDEMPTION_SCORE = 85.0

DOCS_USER_AGENT = "rwa-transparency-score/0.3 (+hackathon demo)"

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

    def verify_redemption(self, *, ticker: str, issuer_name: str) -> VerificationResult: ...


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


# Live Polygon bToken magnitudes (bIB01 ≈ 80/4442, bNVDA ≈ 26/5385) are
# ~0.02 — same-chain totalSupply is not a verified circulating figure.
MIN_PLAUSIBLE_POR_RATIO = 0.95
RESERVES_ONLY_POR_SCORE = 90.0


def por_ratio_is_plausible(ratio: float) -> bool:
    """True only when reserves and circulating look like the same unit of account."""
    return ratio >= MIN_PLAUSIBLE_POR_RATIO


def visible_text(payload: str) -> str:
    """Strip tags / collapse whitespace so markdown and HTML share one parser."""
    text = re.sub(r"<[^>]+>", " ", payload or "")
    return re.sub(r"\s+", " ", text).strip()


def fetch_public_text(
    session: requests.Session,
    url: str,
    *,
    timeout: float = REQUEST_TIMEOUT,
) -> str:
    """GET a public URL. Raises on non-200 or empty body — caller fails closed."""
    resp = session.get(
        url,
        timeout=timeout,
        headers={"User-Agent": DOCS_USER_AGENT},
    )
    status = int(getattr(resp, "status_code", 0) or 0)
    if status != 200:
        raise RuntimeError(f"{url} HTTP {status}")
    text = resp.text or ""
    if not text.strip():
        raise RuntimeError(f"{url} returned empty body")
    return text


def parse_backed_redemption_docs(text: str) -> dict[str, Any]:
    """Require explicit primary-market redemption language. Never infer it."""
    blob = visible_text(text).lower()
    has_redemption = bool(re.search(r"\bredemption\b|\bredeem(?:able|ed|s)?\b", blob))
    has_primary_market = "primary market" in blob
    has_underlying = "underlying" in blob
    has_kyc = "kyc" in blob
    has_whitelist = "whitelist" in blob
    has_in_kind_heading = bool(re.search(r"\bin-kind\b|\bin kind\b", blob))
    in_kind_to_shares = bool(
        re.search(
            r"redeemed back into shares|tokens to shares|underlying shares",
            blob,
        )
    )
    documented = bool(has_redemption and (has_primary_market or has_underlying))
    missing: list[str] = []
    if not has_redemption:
        missing.append("redemption / redeem language")
    if not (has_primary_market or has_underlying):
        missing.append("primary market or underlying-asset language")
    return {
        "documented": documented,
        "has_redemption": has_redemption,
        "has_primary_market": has_primary_market,
        "has_underlying": has_underlying,
        "has_kyc": has_kyc,
        "has_whitelist": has_whitelist,
        "has_in_kind_heading": has_in_kind_heading,
        "in_kind_to_shares": in_kind_to_shares,
        "missing": missing,
    }


def parse_dinari_redemption_docs(text: str) -> dict[str, Any]:
    """Require mint/burn documented as issuance/redemption. Never infer it."""
    blob = visible_text(text).lower()
    has_redemption = "redemption" in blob
    has_burn = bool(re.search(r"\bburn\b", blob))
    has_issuance = "issuance" in blob or bool(re.search(r"\bmint\b", blob))
    has_brokerage = "brokerage" in blob or "alpaca" in blob
    documented = bool(has_redemption and has_burn)
    missing: list[str] = []
    if not has_redemption:
        missing.append("redemption language")
    if not has_burn:
        missing.append("burn-as-redemption language")
    return {
        "documented": documented,
        "has_redemption": has_redemption,
        "has_burn": has_burn,
        "has_issuance": has_issuance,
        "has_brokerage": has_brokerage,
        "missing": missing,
    }


class BackedVerifier:
    """Chainlink Proof of Reserve (on-chain AggregatorV3) for Backed / xStocks."""

    name = "backed"

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        cache_ttl: float = CACHE_TTL_SECONDS,
        timeout: float = REQUEST_TIMEOUT,
        client: ChainlinkPorClient | None = None,
        feeds: tuple[PorFeed, ...] | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self.feeds = feeds
        self.client = client or ChainlinkPorClient(session=self.session, timeout=timeout)
        # ticker -> (monotonic_ts, reading_or_exc)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._docs_cache: dict[str, tuple[float, Any]] = {}

    def _read_por(self, ticker: str) -> PorReading:
        key = (ticker or "").strip().upper()
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and (now - hit[0]) <= self.cache_ttl:
            cached = hit[1]
            if isinstance(cached, Exception):
                raise cached
            return cached

        try:
            reading = self.client.read(ticker, feeds=self.feeds)
            self._cache[key] = (now, reading)
            return reading
        except Exception as exc:  # noqa: BLE001 — cache + re-raise for fallback path
            wrapped = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
            self._cache[key] = (now, wrapped)
            raise wrapped

    def _from_por(
        self,
        *,
        ticker: str,
        issuer_name: str,
        pillar: str,
    ) -> VerificationResult:
        feed = resolve_por_feed(ticker, self.feeds)
        if feed is None:
            return heuristic_result(
                pillar,
                issuer_name,
                reason=(
                    f"No published Chainlink PoR feed for {ticker}. "
                    "Backed / xStocks assets without a SmartData proxy stay on the name list."
                ),
            )

        try:
            reading = self._read_por(ticker)
        except Exception as exc:  # noqa: BLE001 — never silent
            return heuristic_result(
                pillar,
                issuer_name,
                reason=f"Chainlink PoR call failed for {ticker} ({feed.symbol} on {feed.chain}).",
                error=str(exc),
            )

        reserves = reading.reserves
        circulating = reading.circulating
        if reserves <= 0:
            return heuristic_result(
                pillar,
                issuer_name,
                reason=f"Chainlink PoR reserves for {ticker} were {reserves}.",
                error="reserves <= 0",
            )

        # Default: reserves-only. Same-chain ERC-20 totalSupply is multi-chain /
        # units-unverified (live bIB01 ≈ 80/4442, bNVDA ≈ 26/5385). Never treat
        # that ~0.02 figure as a clean undercollateralized on-chain PoR hit.
        ratio: float | None = None
        used_ratio = False
        if circulating is not None and circulating > 0:
            ratio = reserves / circulating
            if por_ratio_is_plausible(ratio):
                score = por_score_from_ratio(ratio)
                used_ratio = True
                ratio_bit = (
                    f"reserves={reserves} {feed.unit} / circulatingSupply={circulating} "
                    f"→ collateralization_ratio={ratio:.6f} (score {score:.0f})"
                )
            else:
                score = RESERVES_ONLY_POR_SCORE
                ratio_bit = (
                    f"reserves={reserves} {feed.unit}; ignored implausible "
                    f"collateralization_ratio={ratio:.6f} "
                    f"(supply multi-chain or units unverified; score {score:.0f})"
                )
        else:
            score = RESERVES_ONLY_POR_SCORE
            ratio_bit = (
                f"reserves={reserves} {feed.unit} (reserves-only; same-chain "
                f"totalSupply not used for ratio; score {score:.0f})"
            )

        evidence = (
            f"Chainlink PoR {feed.symbol} on {feed.chain} ({feed.proxy}): {ratio_bit}. "
            f"Oracle-verified reserves via AggregatorV3 latestRoundData "
            f"(docs: {CHAINLINK_SMARTDATA_DOCS})"
        )
        notes = [
            f"verification={VerificationLevel.ON_CHAIN_POR.value}",
            f"evidence source: Chainlink PoR {feed.proxy} on {feed.chain}",
            f"Chainlink SmartData: {feed.docs}",
        ]
        if not used_ratio:
            notes.append(
                "Reserves-only score: circulating supply not proven same-unit / global."
            )
        return VerificationResult(
            score=score,
            level=VerificationLevel.ON_CHAIN_POR,
            evidence=evidence,
            source="chainlink_por",
            notes=notes,
            ok=True,
            meta={
                "symbol": feed.symbol,
                "chain": feed.chain,
                "proxy": feed.proxy,
                "reserves": reserves,
                "circulating_supply": circulating,
                "collateralization_ratio": ratio if used_ratio else None,
                "ratio_ignored": bool(ratio is not None and not used_ratio),
                "round_id": reading.round_id,
                "updated_at": reading.updated_at,
                "rpc_url": reading.rpc_url,
                "unit": feed.unit,
            },
        )

    def verify_backing(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        # Live PoR is primarily a reserves signal; treat solid collateralization as
        # corroboration of a real share-backed model.
        result = self._from_por(ticker=ticker, issuer_name=issuer_name, pillar="backing")
        if result.ok and result.level == VerificationLevel.ON_CHAIN_POR:
            # Cap backing slightly below reserves when ratio is excellent — still high.
            result.score = min(result.score, 92.0)
            result.notes.append("Backing corroborated by Chainlink PoR collateralization.")
        return result

    def verify_reserves(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        return self._from_por(ticker=ticker, issuer_name=issuer_name, pillar="reserves")

    def _fetch_doc(self, url: str) -> str:
        now = time.monotonic()
        hit = self._docs_cache.get(url)
        if hit is not None and (now - hit[0]) <= self.cache_ttl:
            cached = hit[1]
            if isinstance(cached, Exception):
                raise cached
            return cached
        try:
            text = fetch_public_text(self.session, url, timeout=self.timeout)
            self._docs_cache[url] = (now, text)
            return text
        except Exception as exc:  # noqa: BLE001 — cache + re-raise for fallback
            wrapped = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
            self._docs_cache[url] = (now, wrapped)
            raise wrapped

    def verify_redemption(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        """Live hook: public xStocks/Backed issuance + redemption markdown.

        Fails closed into labeled heuristic fallback. Does not claim in-kind
        share delivery unless that language is actually on the fetched page.
        """
        try:
            overview = self._fetch_doc(BACKED_REDEMPTION_DOCS_URL)
            claims = parse_backed_redemption_docs(overview)
            in_kind_error: str | None = None
            try:
                in_kind_text = self._fetch_doc(BACKED_INKIND_DOCS_URL)
                in_kind_claims = parse_backed_redemption_docs(in_kind_text)
                if in_kind_claims["in_kind_to_shares"]:
                    claims["in_kind_to_shares"] = True
            except Exception as exc:  # noqa: BLE001 — optional page
                in_kind_error = str(exc)
        except Exception as exc:  # noqa: BLE001 — never silent
            return heuristic_result(
                "redemption",
                issuer_name,
                reason=(
                    f"Backed / xStocks redemption docs fetch failed for {ticker}."
                ),
                error=str(exc),
            )

        if not claims["documented"]:
            missing = ", ".join(claims["missing"]) or "required redemption language"
            return heuristic_result(
                "redemption",
                issuer_name,
                reason=(
                    "Backed / xStocks docs fetched but missing required "
                    f"redemption language: {missing}."
                ),
                error=f"incomplete redemption signals: {missing}",
            )

        in_kind = bool(claims["in_kind_to_shares"])
        score = INKIND_REDEMPTION_SCORE if in_kind else CASH_REDEMPTION_SCORE
        bits = [
            "public docs: primary-market issuance and redemption",
        ]
        if claims["has_kyc"] or claims["has_whitelist"]:
            bits.append("onboarding / KYC / whitelist required (not a free on-chain claim)")
        if in_kind:
            bits.append("xPort in-kind flow: tokens redeemable back into shares (Alpaca)")
        else:
            bits.append(
                "cash / stablecoin settlement on the documented market flow "
                "— not treated as in-kind share delivery"
            )
        evidence = (
            f"Backed / xStocks redemption docs for {ticker}: " + "; ".join(bits) + ". "
            f"Sources: {BACKED_REDEMPTION_DOCS_URL}"
            + (f"; {BACKED_INKIND_DOCS_URL}" if in_kind else "")
        )
        notes = [
            f"verification={VerificationLevel.SELF_REPORTED.value}",
            "live redemption hook: issuer public docs",
            f"evidence source: {BACKED_REDEMPTION_DOCS_URL}",
        ]
        if in_kind:
            notes.append(f"in-kind source: {BACKED_INKIND_DOCS_URL}")
        elif in_kind_error:
            notes.append(f"In-kind docs not confirmed (not dropped): {in_kind_error}")
        return VerificationResult(
            score=score,
            level=VerificationLevel.SELF_REPORTED,
            evidence=evidence,
            source="backed_redemption_docs",
            notes=notes,
            ok=True,
            meta={
                "ticker": ticker,
                "issuer": issuer_name,
                "docs_url": BACKED_REDEMPTION_DOCS_URL,
                "in_kind_docs_url": BACKED_INKIND_DOCS_URL if in_kind else None,
                "in_kind_to_shares": in_kind,
                "kyc_or_whitelist": bool(claims["has_kyc"] or claims["has_whitelist"]),
                "settlement": "in-kind-shares" if in_kind else "cash-or-stablecoin",
            },
        )


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
        self._docs_cache: dict[str, tuple[float, Any]] = {}

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

    def _fetch_doc(self, url: str) -> str:
        now = time.monotonic()
        hit = self._docs_cache.get(url)
        if hit is not None and (now - hit[0]) <= self.cache_ttl:
            cached = hit[1]
            if isinstance(cached, Exception):
                raise cached
            return cached
        try:
            text = fetch_public_text(self.session, url, timeout=self.timeout)
            self._docs_cache[url] = (now, text)
            return text
        except Exception as exc:  # noqa: BLE001 — cache + re-raise for fallback
            wrapped = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
            self._docs_cache[url] = (now, wrapped)
            raise wrapped

    def verify_redemption(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        """Live hook: Dinari public dShare docs (burn = redemption).

        Fails closed into labeled heuristic fallback. Does not claim in-kind
        share delivery — the published flow burns tokens and transfers funds.
        """
        try:
            text = self._fetch_doc(DINARI_DSHARE_DOCS_URL)
            claims = parse_dinari_redemption_docs(text)
        except Exception as exc:  # noqa: BLE001 — never silent
            return heuristic_result(
                "redemption",
                issuer_name,
                reason=f"Dinari dShare redemption docs fetch failed for {ticker}.",
                error=str(exc),
            )

        if not claims["documented"]:
            missing = ", ".join(claims["missing"]) or "required redemption language"
            return heuristic_result(
                "redemption",
                issuer_name,
                reason=(
                    "Dinari docs fetched but missing required redemption language: "
                    f"{missing}."
                ),
                error=f"incomplete redemption signals: {missing}",
            )

        evidence = (
            f"Dinari dShare docs for {ticker}: mint (issuance) / burn (redemption) "
            "only after a brokerage fill; sell burns dShares and transfers funds. "
            "Cash/proceeds settlement — not treated as in-kind share delivery. "
            f"Source: {DINARI_DSHARE_DOCS_URL}"
        )
        notes = [
            f"verification={VerificationLevel.SELF_REPORTED.value}",
            "live redemption hook: issuer public docs",
            f"evidence source: {DINARI_DSHARE_DOCS_URL}",
            "Burn-on-sell / funds transfer — not in-kind share delivery.",
        ]
        return VerificationResult(
            score=CASH_REDEMPTION_SCORE,
            level=VerificationLevel.SELF_REPORTED,
            evidence=evidence,
            source="dinari_redemption_docs",
            notes=notes,
            ok=True,
            meta={
                "ticker": ticker,
                "issuer": issuer_name,
                "docs_url": DINARI_DSHARE_DOCS_URL,
                "settlement": "cash-or-funds",
                "in_kind_to_shares": False,
                **{k: claims[k] for k in ("has_redemption", "has_burn", "has_issuance", "has_brokerage")},
            },
        )


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
