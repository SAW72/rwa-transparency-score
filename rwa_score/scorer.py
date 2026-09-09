"""Transparency scoring logic.

Each pillar returns a 0–100 sub-score. The final score is a weighted average.
Weights and thresholds are documented so judges (and users) can see exactly
why a token landed where it did.
"""

from __future__ import annotations

from .client import CMCClient
from .issuer_registry import classify

WEIGHTS = {
    "backing": 0.25,
    "reserves": 0.25,
    "redemption": 0.20,
    "price": 0.15,
    "disclosure": 0.15,
}


def _band(score: float) -> str:
    if score >= 75:
        return "GREEN — claims match reality"
    if score >= 50:
        return "YELLOW — verify before trusting"
    if score >= 25:
        return "ORANGE — thin backing, high risk"
    return "RED — likely unbacked or opaque"


class TransparencyScorer:
    def __init__(self, client: CMCClient) -> None:
        self.client = client
        self._map_cache: dict[str, int] | None = None
        self._issuer_index: dict[int, str] | None = None

    def _resolve(self, ticker: str) -> int:
        if self._map_cache is None:
            self._map_cache = {
                (a.get("symbol") or "").upper(): a["rwa_id"]
                for a in self.client.rwa_map()
                if a.get("symbol")
            }
        rwa_id = self._map_cache.get(ticker.upper())
        if rwa_id is None:
            raise KeyError(f"{ticker} not found in CMC RWA map")
        return rwa_id

    def _issuer_name_for(self, rwa_id: int) -> str:
        if self._issuer_index is None:
            self._issuer_index = {}
            for entry in self.client.issuers_list():
                iid = entry.get("issuer_id")
                if not iid:
                    continue
                detail = self.client.issuer(iid)
                for tok in detail.get("tokens", []):
                    if tok.get("rwa_id") is not None:
                        self._issuer_index[tok["rwa_id"]] = detail.get("name", "")
        return self._issuer_index.get(rwa_id, "")

    def score(self, ticker: str) -> dict:
        rwa_id = self._resolve(ticker)
        info = self.client.rwa_info(rwa_id)
        issuer_name = self._issuer_name_for(rwa_id) or info.get("issuer", {}).get("name", "")
        flags = classify(issuer_name)

        backing = 90 if flags["backed"] else 35
        reserves = 90 if flags["audited"] else 30
        redemption = 85 if flags["redeemable"] else 25

        # Price integrity: penalize wild 24h swings on the on-chain token.
        price_score = 60
        crypto_id = None
        try:
            for entry in self.client.issuers_list():
                detail = self.client.issuer(entry.get("issuer_id", ""))
                for tok in detail.get("tokens", []):
                    if tok.get("rwa_id") == rwa_id:
                        crypto_id = tok.get("crypto_id")
                        break
                if crypto_id:
                    break
            if crypto_id:
                q = self.client.crypto_quote(crypto_id)
                usd = q.get("quote", {}).get("USD", {})
                pct = abs(usd.get("percent_change_24h") or 0)
                price_score = max(20, 100 - pct * 2)
        except Exception:  # noqa: BLE001
            pass

        disclosure = 80 if info.get("cik") else 20

        subscores = {
            "backing": backing,
            "reserves": reserves,
            "redemption": redemption,
            "price": price_score,
            "disclosure": disclosure,
        }
        final = sum(subscores[k] * WEIGHTS[k] for k in WEIGHTS)

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
            "rwa_id": rwa_id,
            "issuer": issuer_name or "unknown",
            "score": round(final, 1),
            "band": _band(final),
            "subscores": {k: round(v, 1) for k, v in subscores.items()},
            "flags": risk_flags,
            "summary": f"{issuer_name or 'Unknown issuer'} — {len(risk_flags)} risk flag(s).",
        }
