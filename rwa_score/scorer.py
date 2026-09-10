"""Transparency scoring logic.

Each pillar returns a 0–100 sub-score. The final score is a weighted average.
Weights and thresholds are documented so judges (and users) can see exactly
why a token landed where it did.

Backing and reserves prefer live attestation / PoR verifiers when the issuer
is known. Redemption uses ``verify_redemption`` when that method exists
(Robinhood); Backed and Dinari stay on the name heuristic. Failed verifiers
are never dropped silently — errors are appended to notes/flags and labeled
**heuristic fallback**.
"""

from __future__ import annotations

from typing import Any

import requests

from .client import RWAClient
from .issuer_registry import HEURISTIC_NOTE, classify, issuer_note
from .verifiers import (
    VerificationLevel,
    VerificationResult,
    Verifier,
    build_default_verifiers,
    get_verifier_for_issuer,
    heuristic_result,
)

WEIGHTS: dict[str, float] = {
    "backing": 0.25,
    "reserves": 0.25,
    "redemption": 0.20,
    "price": 0.15,
    "disclosure": 0.15,
}

PILLARS: dict[str, dict[str, str]] = {
    "backing": {
        "label": "Backing model",
        "what": "Real shares with a regulated custodian vs. a thin debt note.",
    },
    "reserves": {
        "label": "Proof of reserves",
        "what": "Independent auditor publishing on-chain (e.g. Chainlink) vs. a promise.",
    },
    "redemption": {
        "label": "Redemption rights",
        "what": "Redeemable for the underlying share vs. sell-only.",
    },
    "price": {
        "label": "Price integrity",
        "what": "On-chain token tracks the stock without wild 24h drift.",
    },
    "disclosure": {
        "label": "Disclosure",
        "what": "Matchable SEC CIK on the RWA info record vs. missing.",
    },
}

# Conservative defaults when a live field is missing — never pretend we measured it.
DEFAULT_PRICE_SCORE = 50.0
MISSING_CRYPTO_PRICE_SCORE = 45.0
QUOTE_ERROR_PRICE_SCORE = 50.0


class ScoreError(RuntimeError):
    """Raised when a ticker cannot be scored (unknown symbol, empty info, etc.)."""


def band_code(score: float) -> str:
    if score >= 75:
        return "GREEN"
    if score >= 50:
        return "YELLOW"
    if score >= 25:
        return "ORANGE"
    return "RED"


def band_detail(score: float) -> str:
    code = band_code(score)
    details = {
        "GREEN": "GREEN — heuristic: stronger transparency signals (still verify)",
        "YELLOW": "YELLOW — heuristic: mixed signals; verify before relying",
        "ORANGE": "ORANGE — heuristic: weaker signals; elevated concern",
        "RED": "RED — heuristic: opaque or thin signals (not a finding of fraud or illegality)",
    }
    return details[code]


def _band(score: float) -> str:
    """Backward-compatible full band string."""
    return band_detail(score)


def _append_verification_notes(
    explanation: str,
    result: VerificationResult,
) -> str:
    bits = [
        explanation,
        f"Verification: {result.level.value}.",
        f"Evidence: {result.evidence}",
    ]
    for note in result.notes:
        if note and note not in bits:
            bits.append(note)
    if result.error:
        bits.append(f"Verifier failure recorded (not dropped): {result.error}")
    return " ".join(bits)


class TransparencyScorer:
    def __init__(
        self,
        client: RWAClient,
        *,
        session: requests.Session | None = None,
        verifiers: dict[str, Verifier] | None = None,
        use_live_verifiers: bool | None = None,
    ) -> None:
        self.client = client
        self._map_cache: dict[str, int] | None = None
        # rwa_id -> {issuer_id, issuer_name, crypto_id}
        self._issuer_index: dict[int, dict[str, Any]] | None = None
        self._issuer_detail_cache: dict[str, dict[str, Any]] = {}
        self._session = session
        self._verifiers = verifiers if verifiers is not None else build_default_verifiers(session)
        # Fixture / offline demos skip network attestation calls.
        if use_live_verifiers is None:
            self.use_live_verifiers = getattr(client, "source", "") != "fixture"
        else:
            self.use_live_verifiers = use_live_verifiers

    def _resolve(self, ticker: str) -> int:
        if self._map_cache is None:
            try:
                assets = self.client.rwa_map()
            except Exception as exc:  # noqa: BLE001 — surface map failures
                raise ScoreError(f"Could not load RWA map: {exc}") from exc
            self._map_cache = {
                (a.get("symbol") or "").upper(): int(a["rwa_id"])
                for a in assets
                if a.get("symbol") and a.get("rwa_id") is not None
            }
        rwa_id = self._map_cache.get(ticker.upper())
        if rwa_id is None:
            raise ScoreError(f"{ticker} not found in RWA map")
        return rwa_id

    def _ensure_issuer_index(self) -> None:
        """Walk the issuer directory once per scorer and cache details.

        Live mode: 1 credit for the list + 1 credit per issuer detail, then reuse.
        CMCClient also caches list + detail for the process lifetime, so a new
        scorer sharing that client does not re-hit the API.
        """
        if self._issuer_index is not None:
            return
        self._issuer_index = {}
        try:
            entries = self.client.issuers_list()
        except Exception as exc:  # noqa: BLE001
            raise ScoreError(f"Could not load issuer list: {exc}") from exc

        for entry in entries:
            iid = entry.get("issuer_id")
            if not iid:
                continue
            try:
                detail = self.client.issuer(iid)
            except Exception as exc:  # noqa: BLE001
                raise ScoreError(f"Could not load issuer {iid}: {exc}") from exc
            self._issuer_detail_cache[str(iid)] = detail
            name = detail.get("name") or entry.get("name") or ""
            for tok in detail.get("tokens") or []:
                rid = tok.get("rwa_id")
                if rid is None:
                    continue
                self._issuer_index[int(rid)] = {
                    "issuer_id": iid,
                    "issuer_name": name,
                    "crypto_id": tok.get("crypto_id"),
                }

    def _token_for(self, rwa_id: int) -> dict[str, Any]:
        self._ensure_issuer_index()
        assert self._issuer_index is not None
        return self._issuer_index.get(rwa_id, {})

    def _price_score(self, crypto_id: Any) -> tuple[float, dict[str, Any], list[str], str]:
        """Return (score, quote_meta, flags, explanation). Never swallow errors silently."""
        flags: list[str] = []
        if not crypto_id:
            flags.append("No on-chain crypto_id linked to this RWA — price integrity unverified.")
            return (
                MISSING_CRYPTO_PRICE_SCORE,
                {"available": False, "crypto_id": None, "percent_change_24h": None},
                flags,
                "No token crypto_id in the issuer cache; assigned the missing-quote default "
                f"({MISSING_CRYPTO_PRICE_SCORE:.0f}).",
            )
        try:
            quote = self.client.crypto_quote(int(crypto_id))
        except Exception as exc:  # noqa: BLE001 — record, do not hide
            flags.append(f"Token quote lookup failed: {exc}")
            return (
                QUOTE_ERROR_PRICE_SCORE,
                {"available": False, "crypto_id": int(crypto_id), "percent_change_24h": None},
                flags,
                f"Quote endpoint error; assigned the error default ({QUOTE_ERROR_PRICE_SCORE:.0f}).",
            )

        usd = (quote.get("quote") or {}).get("USD") or {}
        if not usd:
            flags.append("Quote payload had no USD block — price integrity unverified.")
            return (
                DEFAULT_PRICE_SCORE,
                {"available": False, "crypto_id": int(crypto_id), "percent_change_24h": None},
                flags,
                f"Empty USD quote; assigned the unverified default ({DEFAULT_PRICE_SCORE:.0f}).",
            )

        raw_pct = usd.get("percent_change_24h")
        if raw_pct is None:
            flags.append("USD quote missing percent_change_24h — price integrity unverified.")
            return (
                DEFAULT_PRICE_SCORE,
                {"available": False, "crypto_id": int(crypto_id), "percent_change_24h": None},
                flags,
                f"No 24h change in quote; assigned the unverified default ({DEFAULT_PRICE_SCORE:.0f}).",
            )

        pct = abs(float(raw_pct))
        score = max(20.0, 100.0 - pct * 2)
        meta = {
            "available": True,
            "crypto_id": int(crypto_id),
            "percent_change_24h": float(raw_pct),
            "price": usd.get("price"),
            "volume_24h": usd.get("volume_24h"),
        }
        return (
            score,
            meta,
            flags,
            f"24h change {float(raw_pct):+.2f}%; score = max(20, 100 − |Δ| × 2) = {score:.1f}.",
        )

    def _heuristic_redemption(self, issuer_name: str) -> VerificationResult:
        """Name-list redemption for issuers that do not implement verify_redemption."""
        result = heuristic_result(
            "redemption",
            issuer_name,
            reason="Redemption verifier not wired yet (TODO hook); using name heuristic.",
        )
        # Redemption heuristic is intentional, not a failed live call.
        result.ok = True
        result.notes = [
            "heuristic fallback",
            "TODO: redemption attestation verifier",
            HEURISTIC_NOTE,
        ]
        result.evidence = (
            f"heuristic fallback: issuer '{issuer_name or 'unknown'}' redemption "
            f"rights via name list (live redemption verifier pending)."
        )
        return result

    def _verify_pillar(
        self,
        pillar: str,
        *,
        ticker: str,
        issuer_name: str,
    ) -> VerificationResult:
        """Run the issuer's verifier, or heuristic for unknown / offline."""
        if not self.use_live_verifiers:
            if pillar == "redemption":
                return self._heuristic_redemption(issuer_name)
            result = heuristic_result(
                pillar,
                issuer_name,
                reason="Fixture/offline mode — live attestation verifiers skipped.",
            )
            # Fixture path is an intentional skip, keep labeled but ok=True for UX.
            result.ok = True
            return result

        verifier = get_verifier_for_issuer(issuer_name, self._verifiers)
        if pillar == "redemption":
            method = getattr(verifier, "verify_redemption", None) if verifier is not None else None
            if callable(method):
                try:
                    return method(ticker=ticker, issuer_name=issuer_name)
                except Exception as exc:  # noqa: BLE001 — never silently drop
                    return heuristic_result(
                        pillar,
                        issuer_name,
                        reason=f"{verifier.name} verifier raised unexpectedly.",
                        error=str(exc),
                    )
            return self._heuristic_redemption(issuer_name)

        if verifier is None:
            return heuristic_result(
                pillar,
                issuer_name,
                reason="Unknown issuer — no attestation verifier registered.",
            )

        try:
            if pillar == "backing":
                return verifier.verify_backing(ticker=ticker, issuer_name=issuer_name)
            if pillar == "reserves":
                return verifier.verify_reserves(ticker=ticker, issuer_name=issuer_name)
        except Exception as exc:  # noqa: BLE001 — never silently drop
            return heuristic_result(
                pillar,
                issuer_name,
                reason=f"{verifier.name} verifier raised unexpectedly.",
                error=str(exc),
            )

        return heuristic_result(
            pillar,
            issuer_name,
            reason=f"No verifier method for pillar {pillar}.",
        )

    def score(self, ticker: str) -> dict[str, Any]:
        rwa_id = self._resolve(ticker)
        try:
            info = self.client.rwa_info(rwa_id)
        except Exception as exc:  # noqa: BLE001
            raise ScoreError(f"Could not load RWA info for {ticker}: {exc}") from exc
        if not info:
            raise ScoreError(f"No RWA info returned for {ticker} (rwa_id={rwa_id})")

        token = self._token_for(rwa_id)
        issuer_name = (
            token.get("issuer_name")
            or (info.get("issuer") or {}).get("name")
            or ""
        )
        flags = classify(issuer_name)
        symbol = ticker.upper()

        backing_v = self._verify_pillar("backing", ticker=symbol, issuer_name=issuer_name)
        reserves_v = self._verify_pillar("reserves", ticker=symbol, issuer_name=issuer_name)
        redemption_v = self._verify_pillar("redemption", ticker=symbol, issuer_name=issuer_name)

        backing = backing_v.score
        reserves = reserves_v.score
        redemption = redemption_v.score

        backing_why = _append_verification_notes(
            (
                f"Heuristic: issuer '{issuer_name or 'unknown'}' matched the fully-backed name list."
                if flags["backed"] and backing_v.source == "heuristic_fallback"
                else (
                    f"Heuristic: issuer '{issuer_name or 'unknown'}' did not match known fully-backed issuers."
                    if backing_v.source == "heuristic_fallback"
                    else f"Live backing check for '{issuer_name or 'unknown'}'."
                )
            ),
            backing_v,
        )
        reserves_why = _append_verification_notes(
            (
                f"Heuristic: issuer '{issuer_name or 'unknown'}' matched the independent-PoR name list."
                if flags["audited"] and reserves_v.source == "heuristic_fallback"
                else (
                    f"Heuristic: issuer '{issuer_name or 'unknown'}' has no independent on-chain PoR match."
                    if reserves_v.source == "heuristic_fallback"
                    else f"Live reserves check for '{issuer_name or 'unknown'}'."
                )
            ),
            reserves_v,
        )
        redemption_why = _append_verification_notes(
            (
                f"Heuristic: issuer '{issuer_name or 'unknown'}' matched the redeemable name list."
                if flags["redeemable"]
                else f"Heuristic: issuer '{issuer_name or 'unknown'}' treated as sell-only (no redemption match)."
            ),
            redemption_v,
        )

        price_score, price_meta, price_flags, price_why = self._price_score(token.get("crypto_id"))
        price_v = VerificationResult(
            score=price_score,
            level=VerificationLevel.SELF_REPORTED,
            evidence=(
                f"CMC crypto quote crypto_id={price_meta.get('crypto_id')}; "
                f"24hΔ={price_meta.get('percent_change_24h')}"
                if price_meta.get("available")
                else "CMC quote unavailable — self-reported gap."
            ),
            source="cmc_quote",
            notes=[f"verification={VerificationLevel.SELF_REPORTED.value}"],
            ok=bool(price_meta.get("available")),
        )
        price_why = _append_verification_notes(price_why, price_v)

        cik = info.get("cik")
        disclosure = 80.0 if cik else 20.0
        disclosure_v = VerificationResult(
            score=disclosure,
            level=VerificationLevel.SELF_REPORTED,
            evidence=(
                f"SEC CIK {cik} on CMC RWA info record."
                if cik
                else "No SEC CIK on the RWA info record."
            ),
            source="cmc_rwa_info",
            notes=[f"verification={VerificationLevel.SELF_REPORTED.value}"],
            ok=bool(cik),
        )
        disclosure_why = _append_verification_notes(
            (
                f"SEC CIK {cik} present on the RWA info record."
                if cik
                else "No SEC CIK on the RWA info record — issuer identity not matchable to filings."
            ),
            disclosure_v,
        )

        subscores = {
            "backing": backing,
            "reserves": reserves,
            "redemption": redemption,
            "price": price_score,
            "disclosure": disclosure,
        }
        final = sum(subscores[k] * WEIGHTS[k] for k in WEIGHTS)

        risk_flags: list[str] = []
        if disclosure < 50:
            risk_flags.append("No verifiable SEC CIK — issuer identity not matchable to filings.")
        if reserves < 50:
            risk_flags.append("No independent on-chain proof of reserves found (heuristic).")
        if redemption < 50:
            risk_flags.append(
                "No redemption right — you can only sell the token, not claim the share (heuristic)."
            )
        if price_score < 50:
            risk_flags.append("Token price drifting hard from the underlying — possible thin liquidity.")
        risk_flags.extend(price_flags)

        # Never silently drop a failed verifier — promote errors into flags.
        for pillar_key, result in (
            ("backing", backing_v),
            ("reserves", reserves_v),
            ("redemption", redemption_v),
        ):
            if result.error:
                risk_flags.append(
                    f"{pillar_key} verifier failure (heuristic fallback): {result.error}"
                )

        notes = [HEURISTIC_NOTE]
        source = getattr(self.client, "source", "unknown")
        if source == "fixture":
            notes.append(
                "Scores below use bundled DEMO FIXTURE data, not live CoinMarketCap API responses."
            )
        if not self.use_live_verifiers:
            notes.append(
                "Live attestation verifiers skipped (fixture/offline); "
                "backing/reserves use heuristic fallback."
            )
        for result in (backing_v, reserves_v, redemption_v):
            for note in result.notes:
                if note not in notes:
                    notes.append(note)
            if result.error and f"Verifier error: {result.error}" not in notes:
                notes.append(f"Verifier error (not dropped): {result.error}")

        explanations = {
            "backing": backing_why,
            "reserves": reserves_why,
            "redemption": redemption_why,
            "price": price_why,
            "disclosure": disclosure_why,
        }

        verification = {
            "backing": backing_v.as_dict(),
            "reserves": reserves_v.as_dict(),
            "redemption": redemption_v.as_dict(),
            "price": price_v.as_dict(),
            "disclosure": disclosure_v.as_dict(),
        }

        return {
            "ticker": symbol,
            "rwa_id": rwa_id,
            "issuer": issuer_name or "unknown",
            "score": round(final, 1),
            "band": band_code(final),
            "band_label": band_detail(final),
            "subscores": {k: round(v, 1) for k, v in subscores.items()},
            "weights": dict(WEIGHTS),
            "pillars": PILLARS,
            "explanations": explanations,
            "verification": verification,
            "flags": risk_flags,
            "notes": notes,
            "heuristics": {**flags, "source": "issuer_registry", "labeled": True},
            "data_source": source,
            "price": price_meta,
            "cik": cik,
            "issuer_note": issuer_note(issuer_name),
            "summary": f"{issuer_name or 'Unknown issuer'} — {len(risk_flags)} risk flag(s).",
        }
