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

from .client import RWAClient, parse_market_pairs_payload
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
    "backing": 0.20,
    "reserves": 0.20,
    "redemption": 0.15,
    "price": 0.15,
    "disclosure": 0.15,
    "basis": 0.15,
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
    "basis": {
        "label": "Cross-issuer basis",
        "what": "Same underlying ticker, different wrapper prices — spread is wrapper risk.",
    },
}

# Conservative defaults when a live field is missing — never pretend we measured it.
DEFAULT_PRICE_SCORE = 50.0
MISSING_CRYPTO_PRICE_SCORE = 45.0
QUOTE_ERROR_PRICE_SCORE = 50.0
MISSING_BASIS_SCORE = 50.0
SINGLE_WRAPPER_BASIS_SCORE = 55.0
BASIS_ERROR_SCORE = 50.0
BASIS_SCORE_FLOOR = 15.0
BASIS_SPREAD_PENALTY = 10.0


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


def pair_usd_price(pair: dict[str, Any]) -> float | None:
    """Best USD last price on a CMC market-pair row."""
    for block_key in ("quotes", "exchange_reported_quotes"):
        for quote in pair.get(block_key) or []:
            if not isinstance(quote, dict):
                continue
            if (quote.get("symbol") or "").upper() != "USD":
                continue
            raw = quote.get("price")
            if raw is None:
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    usd = (pair.get("quote") or {}).get("USD") or {}
    raw = usd.get("price")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def pair_usd_volume(pair: dict[str, Any]) -> float:
    """24h USD volume on a CMC market-pair row, or 0 if missing."""
    for quote in pair.get("quotes") or []:
        if not isinstance(quote, dict):
            continue
        if (quote.get("symbol") or "").upper() != "USD":
            continue
        raw = quote.get("volume_24h")
        if raw is None:
            continue
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def group_wrapper_quotes(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse exchange rows into one quote per wrapper ``crypto_id``.

    Same wrapper on two venues is one issuer product. Different ``crypto_id``
    values are different wrappers (xStocks vs Ondo vs Dinari). Price is a
    volume-weighted average when 24h volume is present, else a simple mean.
    """
    buckets: dict[int, dict[str, Any]] = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        base = pair.get("market_pair_base") or {}
        raw_id = base.get("crypto_id")
        if raw_id is None:
            continue
        try:
            crypto_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        price = pair_usd_price(pair)
        if price is None or price <= 0:
            continue
        volume = pair_usd_volume(pair)
        bucket = buckets.setdefault(
            crypto_id,
            {
                "crypto_id": crypto_id,
                "symbol": (base.get("symbol") or "").strip() or f"id:{crypto_id}",
                "prices": [],
                "volumes": [],
            },
        )
        if not bucket.get("symbol") or str(bucket["symbol"]).startswith("id:"):
            symbol = (base.get("symbol") or "").strip()
            if symbol:
                bucket["symbol"] = symbol
        bucket["prices"].append(price)
        bucket["volumes"].append(volume)

    wrappers: list[dict[str, Any]] = []
    for bucket in buckets.values():
        prices: list[float] = bucket["prices"]
        volumes: list[float] = bucket["volumes"]
        weighted = sum(p * v for p, v in zip(prices, volumes))
        total_vol = sum(volumes)
        if total_vol > 0:
            representative = weighted / total_vol
        else:
            representative = sum(prices) / len(prices)
        wrappers.append(
            {
                "crypto_id": bucket["crypto_id"],
                "symbol": bucket["symbol"],
                "price": representative,
                "volume_24h": total_vol,
                "venues": len(prices),
            }
        )
    wrappers.sort(key=lambda row: (row["price"], row["crypto_id"]))
    return wrappers


def percent_spread(prices: list[float]) -> float | None:
    """``(max − min) / mid × 100``. ``None`` unless two or more positive prices."""
    clean = [float(p) for p in prices if p is not None and float(p) > 0]
    if len(clean) < 2:
        return None
    low, high = min(clean), max(clean)
    mid = (low + high) / 2.0
    if mid <= 0:
        return None
    return (high - low) / mid * 100.0


def basis_score_from_spread(pct_spread: float) -> float:
    """Map a wrapper percent-spread onto 0–100.

    ``score = max(15, 100 − |spread| × 10)`` so 0.5% → 95, 5% → 50, ≥8.5% → 15.
    """
    return max(BASIS_SCORE_FLOOR, 100.0 - abs(float(pct_spread)) * BASIS_SPREAD_PENALTY)


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

    def _issuer_by_crypto_id(self) -> dict[int, str]:
        """crypto_id → issuer name from the already-built issuer index."""
        self._ensure_issuer_index()
        names: dict[int, str] = {}
        for row in (self._issuer_index or {}).values():
            crypto_id = row.get("crypto_id")
            if crypto_id is None:
                continue
            try:
                names[int(crypto_id)] = str(row.get("issuer_name") or "")
            except (TypeError, ValueError):
                continue
        return names

    def _basis_score(self, rwa_id: int) -> tuple[float, dict[str, Any], list[str], str]:
        """Cross-issuer wrapper spread. Never swallow fetch errors silently."""
        flags: list[str] = []
        empty_meta: dict[str, Any] = {
            "available": False,
            "wrapper_count": 0,
            "percent_spread": None,
            "min_price": None,
            "max_price": None,
            "wrappers": [],
        }
        try:
            raw = self.client.market_pairs(rwa_id=rwa_id)
        except Exception as exc:  # noqa: BLE001 — record, do not hide
            flags.append(f"Market-pairs lookup failed: {exc}")
            return (
                BASIS_ERROR_SCORE,
                empty_meta,
                flags,
                f"Market-pairs endpoint error; assigned the error default "
                f"({BASIS_ERROR_SCORE:.0f}).",
            )

        payload = parse_market_pairs_payload(raw)
        wrappers = group_wrapper_quotes(payload.get("market_pairs") or [])
        issuer_names = self._issuer_by_crypto_id()
        for wrapper in wrappers:
            wrapper["issuer"] = issuer_names.get(int(wrapper["crypto_id"])) or ""

        if not wrappers:
            flags.append(
                "No priced wrapper tokens on CMC market-pairs — cross-issuer basis unverified."
            )
            return (
                MISSING_BASIS_SCORE,
                empty_meta,
                flags,
                "No wrapper USD prices in market-pairs; assigned the missing-pairs default "
                f"({MISSING_BASIS_SCORE:.0f}).",
            )

        if len(wrappers) == 1:
            only = wrappers[0]
            flags.append(
                "Only one wrapper token on CMC market-pairs — cannot compare issuers."
            )
            meta = {
                "available": False,
                "wrapper_count": 1,
                "percent_spread": None,
                "min_price": only["price"],
                "max_price": only["price"],
                "wrappers": wrappers,
            }
            return (
                SINGLE_WRAPPER_BASIS_SCORE,
                meta,
                flags,
                f"Single wrapper {only['symbol']} at {only['price']:.4f}; "
                f"assigned the single-wrapper default ({SINGLE_WRAPPER_BASIS_SCORE:.0f}).",
            )

        prices = [float(w["price"]) for w in wrappers]
        spread = percent_spread(prices)
        if spread is None:
            flags.append("Could not compute a wrapper percent-spread from market-pairs.")
            return (
                MISSING_BASIS_SCORE,
                {
                    "available": False,
                    "wrapper_count": len(wrappers),
                    "percent_spread": None,
                    "min_price": min(prices) if prices else None,
                    "max_price": max(prices) if prices else None,
                    "wrappers": wrappers,
                },
                flags,
                f"Invalid spread inputs; assigned the missing default ({MISSING_BASIS_SCORE:.0f}).",
            )

        score = basis_score_from_spread(spread)
        low, high = min(wrappers, key=lambda w: w["price"]), max(
            wrappers, key=lambda w: w["price"]
        )
        meta = {
            "available": True,
            "wrapper_count": len(wrappers),
            "percent_spread": spread,
            "min_price": low["price"],
            "max_price": high["price"],
            "wrappers": wrappers,
        }
        cheap = f"{low['symbol']} {low['price']:.4f}"
        dear = f"{high['symbol']} {high['price']:.4f}"
        return (
            score,
            meta,
            flags,
            (
                f"{len(wrappers)} wrappers; spread {spread:.2f}% "
                f"({cheap} vs {dear}); "
                f"score = max({BASIS_SCORE_FLOOR:.0f}, 100 − |spread| × "
                f"{BASIS_SPREAD_PENALTY:.0f}) = {score:.1f}."
            ),
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

        basis_score, basis_meta, basis_flags, basis_why = self._basis_score(rwa_id)
        cheap = basis_meta.get("min_price")
        dear = basis_meta.get("max_price")
        spread_pct = basis_meta.get("percent_spread")
        if basis_meta.get("available"):
            basis_evidence = (
                f"CMC market-pairs: {basis_meta.get('wrapper_count')} wrappers; "
                f"spread {spread_pct:.2f}% "
                f"(low {cheap}, high {dear})."
            )
        elif basis_meta.get("wrapper_count") == 1:
            only = (basis_meta.get("wrappers") or [{}])[0]
            basis_evidence = (
                f"CMC market-pairs: single wrapper "
                f"{only.get('symbol') or 'unknown'} — no cross-issuer compare."
            )
        else:
            basis_evidence = "CMC market-pairs unavailable — self-reported gap."
        basis_v = VerificationResult(
            score=basis_score,
            level=VerificationLevel.SELF_REPORTED,
            evidence=basis_evidence,
            source="cmc_market_pairs",
            notes=[f"verification={VerificationLevel.SELF_REPORTED.value}"],
            ok=bool(basis_meta.get("available")),
        )
        basis_why = _append_verification_notes(basis_why, basis_v)

        subscores = {
            "backing": backing,
            "reserves": reserves,
            "redemption": redemption,
            "price": price_score,
            "disclosure": disclosure,
            "basis": basis_score,
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
        if basis_score < 50:
            risk_flags.append(
                "Wide cross-issuer wrapper spread — same ticker, different prices (CMC market-pairs)."
            )
        risk_flags.extend(price_flags)
        risk_flags.extend(basis_flags)

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
            "basis": basis_why,
        }

        verification = {
            "backing": backing_v.as_dict(),
            "reserves": reserves_v.as_dict(),
            "redemption": redemption_v.as_dict(),
            "price": price_v.as_dict(),
            "disclosure": disclosure_v.as_dict(),
            "basis": basis_v.as_dict(),
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
            "basis": basis_meta,
            "cik": cik,
            "issuer_note": issuer_note(issuer_name),
            "summary": f"{issuer_name or 'Unknown issuer'} — {len(risk_flags)} risk flag(s).",
        }
