"""Transparency scoring logic.

Each pillar returns a 0–100 sub-score. The final score is a weighted average.
Weights and thresholds are documented so judges (and users) can see exactly
why a token landed where it did.

Issuer-name matching is a **heuristic**. Price and disclosure pillars use
CMC fields when present; missing data is flagged instead of silently ignored.
"""

from __future__ import annotations

from typing import Any

from .client import RWAClient
from .issuer_registry import HEURISTIC_NOTE, classify

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
        "GREEN": "GREEN — claims match reality",
        "YELLOW": "YELLOW — verify before trusting",
        "ORANGE": "ORANGE — thin backing, high risk",
        "RED": "RED — likely unbacked or opaque",
    }
    return details[code]


def _band(score: float) -> str:
    """Backward-compatible full band string."""
    return band_detail(score)


class TransparencyScorer:
    def __init__(self, client: RWAClient) -> None:
        self.client = client
        self._map_cache: dict[str, int] | None = None
        # rwa_id -> {issuer_id, issuer_name, crypto_id}
        self._issuer_index: dict[int, dict[str, Any]] | None = None
        self._issuer_detail_cache: dict[str, dict[str, Any]] = {}

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

        backing = 90.0 if flags["backed"] else 35.0
        reserves = 90.0 if flags["audited"] else 30.0
        redemption = 85.0 if flags["redeemable"] else 25.0
        backing_why = (
            f"Heuristic: issuer '{issuer_name or 'unknown'}' matched the fully-backed name list."
            if flags["backed"]
            else f"Heuristic: issuer '{issuer_name or 'unknown'}' did not match known fully-backed issuers."
        )
        reserves_why = (
            f"Heuristic: issuer '{issuer_name or 'unknown'}' matched the independent-PoR name list."
            if flags["audited"]
            else f"Heuristic: issuer '{issuer_name or 'unknown'}' has no independent on-chain PoR match."
        )
        redemption_why = (
            f"Heuristic: issuer '{issuer_name or 'unknown'}' matched the redeemable name list."
            if flags["redeemable"]
            else f"Heuristic: issuer '{issuer_name or 'unknown'}' treated as sell-only (no redemption match)."
        )

        price_score, price_meta, price_flags, price_why = self._price_score(token.get("crypto_id"))

        cik = info.get("cik")
        disclosure = 80.0 if cik else 20.0
        disclosure_why = (
            f"SEC CIK {cik} present on the RWA info record."
            if cik
            else "No SEC CIK on the RWA info record — issuer identity not matchable to filings."
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
            risk_flags.append("No redemption right — you can only sell the token, not claim the share (heuristic).")
        if price_score < 50:
            risk_flags.append("Token price drifting hard from the underlying — possible thin liquidity.")
        risk_flags.extend(price_flags)

        notes = [HEURISTIC_NOTE]
        source = getattr(self.client, "source", "unknown")
        if source == "fixture":
            notes.append(
                "Scores below use bundled DEMO FIXTURE data, not live CoinMarketCap API responses."
            )

        explanations = {
            "backing": backing_why,
            "reserves": reserves_why,
            "redemption": redemption_why,
            "price": price_why,
            "disclosure": disclosure_why,
        }

        return {
            "ticker": ticker.upper(),
            "rwa_id": rwa_id,
            "issuer": issuer_name or "unknown",
            "score": round(final, 1),
            "band": band_code(final),
            "band_label": band_detail(final),
            "subscores": {k: round(v, 1) for k, v in subscores.items()},
            "weights": dict(WEIGHTS),
            "pillars": PILLARS,
            "explanations": explanations,
            "flags": risk_flags,
            "notes": notes,
            "heuristics": {**flags, "source": "issuer_registry", "labeled": True},
            "data_source": source,
            "price": price_meta,
            "cik": cik,
            "summary": f"{issuer_name or 'Unknown issuer'} — {len(risk_flags)} risk flag(s).",
        }
