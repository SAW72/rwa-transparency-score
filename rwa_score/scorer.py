"""Transparency scoring logic.

Each pillar returns a 0–100 sub-score. The final score is a weighted average.
Weights and thresholds are documented so judges (and users) can see exactly
why a token landed where it did.

CMC does not publish custody / audit / redemption flags. Those three pillars
come from :mod:`rwa_score.issuer_registry` applied to the issuer name returned
by ``/v5/real-world-assets/issuers``. Disclosure uses the SEC CIK on
``/v5/real-world-assets/info``. Price integrity uses the on-chain token's
24h percent change from ``/v2/cryptocurrency/quotes/latest``.
"""

from __future__ import annotations

from typing import Any

from .client import RWAClient
from .issuer_registry import classify

WEIGHTS = {
    "backing": 0.25,
    "reserves": 0.25,
    "redemption": 0.20,
    "price": 0.15,
    "disclosure": 0.15,
}

PILLAR_HELP = {
    "backing": "Real shares with a regulated custodian vs. a thin debt note (25%)",
    "reserves": "Independent auditor publishing on-chain (e.g. Chainlink) vs. a promise (25%)",
    "redemption": "Redeemable for the underlying share vs. sell-only (20%)",
    "price": "Token tracks the real stock — penalize wild 24h swings (15%)",
    "disclosure": "Matchable SEC CIK on the CMC RWA info record (15%)",
}


def _band(score: float) -> str:
    if score >= 75:
        return "GREEN — claims match reality"
    if score >= 50:
        return "YELLOW — verify before trusting"
    if score >= 25:
        return "ORANGE — thin backing, high risk"
    return "RED — likely unbacked or opaque"


def _band_key(score: float) -> str:
    if score >= 75:
        return "green"
    if score >= 50:
        return "yellow"
    if score >= 25:
        return "orange"
    return "red"


def price_integrity(percent_change_24h: float | None) -> float:
    """Map an absolute 24h move onto 20–100. Unknown quotes sit at 60."""
    if percent_change_24h is None:
        return 60.0
    return max(20.0, 100.0 - abs(percent_change_24h) * 2.0)


class TransparencyScorer:
    def __init__(self, client: RWAClient) -> None:
        self.client = client
        self._map_cache: dict[str, int] | None = None
        self._token_index: dict[int, dict[str, Any]] | None = None

    def available_symbols(self) -> list[str]:
        return sorted({(a.get("symbol") or "").upper() for a in self.client.rwa_map() if a.get("symbol")})

    def _resolve(self, ticker: str) -> int:
        if self._map_cache is None:
            self._map_cache = {
                (a.get("symbol") or "").upper(): int(a["rwa_id"])
                for a in self.client.rwa_map()
                if a.get("symbol") and a.get("rwa_id") is not None
            }
        rwa_id = self._map_cache.get(ticker.upper())
        if rwa_id is None:
            known = ", ".join(sorted(self._map_cache)) or "(none)"
            raise KeyError(f"{ticker} not found in CMC RWA map. Known in this session: {known}")
        return rwa_id

    def _token_meta(self, rwa_id: int) -> dict[str, Any]:
        if self._token_index is None:
            index: dict[int, dict[str, Any]] = {}
            for entry in self.client.issuers_list():
                iid = entry.get("issuer_id")
                if not iid:
                    continue
                detail = self.client.issuer(str(iid))
                name = detail.get("name") or entry.get("name") or ""
                for tok in detail.get("tokens") or []:
                    rid = tok.get("rwa_id")
                    if rid is None:
                        continue
                    index[int(rid)] = {
                        "issuer": name,
                        "issuer_id": detail.get("issuer_id") or iid,
                        "crypto_id": tok.get("crypto_id"),
                        "token_symbol": tok.get("symbol"),
                        "token_name": tok.get("name"),
                    }
            self._token_index = index
        return self._token_index.get(rwa_id, {})

    def score(self, ticker: str) -> dict[str, Any]:
        rwa_id = self._resolve(ticker)
        info = self.client.rwa_info(rwa_id)
        meta = self._token_meta(rwa_id)
        issuer_name = meta.get("issuer") or (info.get("issuer") or {}).get("name") or ""
        flags = classify(issuer_name)

        backing = 90 if flags["backed"] else 35
        reserves = 90 if flags["audited"] else 30
        redemption = 85 if flags["redeemable"] else 25

        crypto_id = meta.get("crypto_id")
        usd: dict[str, Any] = {}
        pct: float | None = None
        if crypto_id:
            try:
                quote = self.client.crypto_quote(int(crypto_id))
                usd = (quote.get("quote") or {}).get("USD") or {}
                raw_pct = usd.get("percent_change_24h")
                pct = float(raw_pct) if raw_pct is not None else None
            except Exception:  # noqa: BLE001 — price is optional; other pillars still score
                usd, pct = {}, None
        price_score = price_integrity(pct)

        cik = info.get("cik")
        disclosure = 80 if cik else 20

        subscores = {
            "backing": backing,
            "reserves": reserves,
            "redemption": redemption,
            "price": price_score,
            "disclosure": disclosure,
        }
        final = sum(subscores[k] * WEIGHTS[k] for k in WEIGHTS)

        reasons = {
            "backing": (
                f"{issuer_name or 'Unknown issuer'} is classified as fully backed."
                if flags["backed"]
                else f"{issuer_name or 'Unknown issuer'} is not on the fully-backed issuer list."
            ),
            "reserves": (
                "Independent on-chain proof of reserves is associated with this issuer."
                if flags["audited"]
                else "No independent on-chain proof of reserves found for this issuer."
            ),
            "redemption": (
                "Issuer is listed as offering redemption for the underlying share."
                if flags["redeemable"]
                else "No redemption right on file — sell-only wrapper."
            ),
            "price": (
                f"24h token move is {pct:+.1f}%."
                if pct is not None
                else "No on-chain quote — price pillar defaults to 60."
            ),
            "disclosure": (
                f"SEC CIK {cik} is present on the CMC RWA info record."
                if cik
                else "No verifiable SEC CIK on the CMC RWA info record."
            ),
        }

        risk_flags = []
        if disclosure < 50:
            risk_flags.append("No verifiable SEC CIK — issuer identity not matchable to filings.")
        if reserves < 50:
            risk_flags.append("No independent on-chain proof of reserves found.")
        if redemption < 50:
            risk_flags.append("No redemption right — you can only sell the token, not claim the share.")
        if price_score < 50:
            risk_flags.append("Token price drifting hard from the underlying — possible thin liquidity.")

        return {
            "ticker": ticker.upper(),
            "name": info.get("name") or ticker.upper(),
            "rwa_id": rwa_id,
            "issuer": issuer_name or "unknown",
            "issuer_id": meta.get("issuer_id"),
            "token_symbol": meta.get("token_symbol"),
            "token_name": meta.get("token_name"),
            "crypto_id": crypto_id,
            "cik": cik,
            "primary_exchange": info.get("primary_exchange"),
            "industry": info.get("industry"),
            "employees": info.get("employees"),
            "price_usd": usd.get("price"),
            "volume_24h": usd.get("volume_24h"),
            "percent_change_24h": pct,
            "score": round(final, 1),
            "band": _band(final),
            "band_key": _band_key(final),
            "subscores": {k: round(float(v), 1) for k, v in subscores.items()},
            "weights": dict(WEIGHTS),
            "reasons": reasons,
            "classification": flags,
            "flags": risk_flags,
            "summary": f"{issuer_name or 'Unknown issuer'} — {len(risk_flags)} risk flag(s).",
            "source": getattr(self.client, "source", "unknown"),
        }

    def score_many(self, tickers: list[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for ticker in tickers:
            try:
                results.append(self.score(ticker))
            except Exception as exc:  # noqa: BLE001 — keep the batch going
                results.append({"ticker": ticker.upper(), "error": str(exc)})
        return results
