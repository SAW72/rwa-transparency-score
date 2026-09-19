"""Transparency scoring logic.

Each pillar returns a 0–100 sub-score. The final score is a weighted average.
Weights and thresholds are documented so judges (and users) can see exactly
why a token landed where it did.

Backing and reserves prefer live attestation / PoR verifiers when the issuer
is known. Redemption uses ``verify_redemption`` when that method exists
(Robinhood debt wrapper; Backed / xStocks and Dinari public docs). Failed
verifiers are never dropped silently — errors are appended to notes/flags and
labeled **heuristic fallback**.
"""

from __future__ import annotations

from typing import Any

import requests

from .chainlink_por import (
    PorFeed,
    backed_map_candidates,
    canonical_backed_por_feed,
    fixture_por_skip_note,
)
from .client import (
    CMCPlanBlockedError,
    PLAN_BLOCK_REASON,
    PLAN_BLOCKED_LABEL,
    RWAClient,
    market_pairs_plan_blocked,
    parse_market_pairs_payload,
    parse_rwa_quotes_payload,
    summarize_call_log,
)
from .issuer_registry import HEURISTIC_NOTE, classify, issuer_note
from .verifiers import (
    VerificationLevel,
    VerificationResult,
    Verifier,
    build_default_verifiers,
    get_verifier_for_issuer,
    heuristic_result,
    resolve_verifier_id,
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
        "what": "Issuer tokens track CMC average_tokenized_price; crypto 24hΔ is the labeled fallback.",
    },
    "disclosure": {
        "label": "Disclosure",
        "what": "Matchable SEC CIK on the RWA info record vs. missing.",
    },
    "basis": {
        "label": "Cross-issuer basis",
        "what": "Same ticker, different issuer token prices (RWA quotes tokens[] plus market-pairs).",
    },
}

# Conservative defaults when a live field is missing — never pretend we measured it.
DEFAULT_PRICE_SCORE = 50.0
MISSING_CRYPTO_PRICE_SCORE = 45.0
QUOTE_ERROR_PRICE_SCORE = 50.0
MISSING_BASIS_SCORE = 50.0
SINGLE_WRAPPER_BASIS_SCORE = 55.0
BASIS_ERROR_SCORE = 50.0
PLAN_BLOCKED_BASIS_SCORE = 50.0
BASIS_SCORE_FLOOR = 15.0
BASIS_SPREAD_PENALTY = 10.0
PRICE_SCORE_FLOOR = 20.0
PRICE_DEV_PENALTY = 2.0
ZERO_VOLUME_PRICE_CAP = 45.0
SOURCE_RWA_QUOTES = "cmc_rwa_quotes"
SOURCE_CRYPTO_QUOTE = "cmc_crypto_quote"
SOURCE_MARKET_PAIRS = "cmc_market_pairs"
SOURCE_RWA_AND_PAIRS = "cmc_rwa_quotes+market_pairs"
SOURCE_MARKET_PAIRS_PLAN_BLOCKED = "cmc_market_pairs_plan_blocked"


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


def wrappers_from_rwa_tokens(tokens: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Priced ``tokens[]`` rows from CMC RWA quotes/latest — one wrapper each."""
    wrappers: list[dict[str, Any]] = []
    for row in tokens or []:
        if not isinstance(row, dict):
            continue
        price = row.get("price")
        try:
            price_f = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_f = None
        if price_f is None or price_f <= 0:
            continue
        crypto_id = row.get("crypto_id")
        try:
            cid = int(crypto_id) if crypto_id is not None else None
        except (TypeError, ValueError):
            cid = None
        volume = row.get("volume_24h")
        try:
            vol_f = max(0.0, float(volume)) if volume is not None else 0.0
        except (TypeError, ValueError):
            vol_f = 0.0
        wrappers.append(
            {
                "crypto_id": cid,
                "symbol": (row.get("symbol") or "").strip() or (f"id:{cid}" if cid else "token"),
                "price": price_f,
                "volume_24h": vol_f,
                "venues": 1,
                "issuer": (row.get("issuer_name") or "").strip(),
                "source": SOURCE_RWA_QUOTES,
            }
        )
    wrappers.sort(key=lambda row: (row["price"], row.get("crypto_id") or 0))
    return wrappers


def merge_basis_wrappers(
    quote_wrappers: list[dict[str, Any]],
    pair_wrappers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Union by ``crypto_id`` (else symbol). Quotes supply issuer_name; pairs add venues."""
    merged: dict[tuple[str, Any], dict[str, Any]] = {}

    def _key(row: dict[str, Any]) -> tuple[str, Any]:
        cid = row.get("crypto_id")
        if cid is not None:
            return ("id", int(cid))
        return ("sym", (row.get("symbol") or "").upper())

    for row in pair_wrappers:
        item = dict(row)
        item.setdefault("source", SOURCE_MARKET_PAIRS)
        item.setdefault("issuer", "")
        merged[_key(item)] = item
    for row in quote_wrappers:
        item = dict(row)
        item.setdefault("source", SOURCE_RWA_QUOTES)
        key = _key(item)
        if key not in merged:
            merged[key] = item
            continue
        existing = merged[key]
        out = {**existing, **item}
        out["venues"] = max(int(existing.get("venues") or 1), int(item.get("venues") or 1))
        if existing.get("issuer") and not item.get("issuer"):
            out["issuer"] = existing["issuer"]
        sources = {
            existing.get("source") or SOURCE_MARKET_PAIRS,
            item.get("source") or SOURCE_RWA_QUOTES,
        }
        out["source"] = (
            SOURCE_RWA_AND_PAIRS if len(sources) > 1 else next(iter(sources))
        )
        merged[key] = out
    wrappers = list(merged.values())
    wrappers.sort(key=lambda row: (float(row["price"]), row.get("crypto_id") or 0))
    return wrappers


def price_score_from_deviation(max_deviation_pct: float) -> float:
    """``score = max(20, 100 − |dev%| × 2)`` — same scale as the old 24hΔ formula."""
    return max(PRICE_SCORE_FLOOR, 100.0 - abs(float(max_deviation_pct)) * PRICE_DEV_PENALTY)


def basis_score_from_spread(pct_spread: float) -> float:
    """Map a wrapper percent-spread onto 0–100.

    ``score = max(15, 100 − |spread| × 10)`` so 0.5% → 95, 5% → 50, ≥8.5% → 15.
    """
    return max(BASIS_SCORE_FLOOR, 100.0 - abs(float(pct_spread)) * BASIS_SPREAD_PENALTY)


ALWAYS_SELF_REPORTED = ("price", "disclosure", "basis")
LIVE_OR_HEURISTIC = ("backing", "reserves", "redemption")


def remaining_heuristic_paths(
    verification: dict[str, Any],
    *,
    data_source: str,
    live_verifiers: bool,
) -> list[dict[str, str]]:
    """Labeled leftover heuristic / self-reported paths for UI + API notes."""
    rows: list[dict[str, str]] = []
    if data_source == "fixture":
        rows.append(
            {
                "key": "data_source",
                "label": "CMC / directory",
                "kind": "fixture",
                "note": "Bundled demo fixtures — not a live CoinMarketCap API response.",
            }
        )
    if not live_verifiers:
        rows.append(
            {
                "key": "verifiers",
                "label": "Live verifiers",
                "kind": "offline_skip",
                "note": "Fixture/offline mode skipped live attestation / PoR / redemption hooks.",
            }
        )
    for key in LIVE_OR_HEURISTIC:
        block = verification.get(key) or {}
        source = str(block.get("source") or "")
        evidence = str(block.get("evidence") or "")
        if source == "heuristic_fallback" or "heuristic fallback" in evidence.lower():
            rows.append(
                {
                    "key": key,
                    "label": PILLARS[key]["label"],
                    "kind": "heuristic_fallback",
                    "note": evidence or "Name-list heuristic fallback — not an audited attestation.",
                }
            )
    for key in ALWAYS_SELF_REPORTED:
        block = verification.get(key) or {}
        if key == "basis" and basis_verification_plan_blocked(block):
            rows.append(
                {
                    "key": key,
                    "label": PILLARS[key]["label"],
                    "kind": "plan-blocked",
                    "note": str(
                        block.get("evidence")
                        or (
                            f"CMC market-pairs {PLAN_BLOCKED_LABEL} — "
                            "not live market-pairs data."
                        )
                    ),
                }
            )
            continue
        rows.append(
            {
                "key": key,
                "label": PILLARS[key]["label"],
                "kind": "self-reported",
                "note": str(block.get("evidence") or "Self-reported CMC/fixture field — not independent PoR."),
            }
        )
    return rows


def basis_verification_plan_blocked(block: dict[str, Any] | None) -> bool:
    payload = block if isinstance(block, dict) else {}
    if (payload.get("meta") or {}).get("plan_blocked"):
        return True
    if payload.get("source") == SOURCE_MARKET_PAIRS_PLAN_BLOCKED:
        return True
    evidence = str(payload.get("evidence") or "").lower()
    return PLAN_BLOCKED_LABEL in evidence or PLAN_BLOCK_REASON in evidence


def basis_is_plan_blocked(
    basis_meta: dict[str, Any] | None = None,
    *,
    verification: dict[str, Any] | None = None,
) -> bool:
    meta = basis_meta if isinstance(basis_meta, dict) else {}
    if meta.get("plan_blocked") or meta.get("unavailable_reason") == PLAN_BLOCK_REASON:
        return True
    block = (verification or {}).get("basis") if isinstance(verification, dict) else None
    return basis_verification_plan_blocked(block)


def _exception_is_plan_blocked(exc: BaseException) -> bool:
    if isinstance(exc, CMCPlanBlockedError):
        return True
    text = str(exc).lower()
    return "1006" in text or (
        "http 403" in text
        and ("plan" in text or "market-pairs" in text or "subscription" in text)
    )


def _stamp_basis_meta(meta: dict[str, Any], *, plan_blocked: bool) -> dict[str, Any]:
    out = dict(meta)
    out["plan_blocked"] = bool(plan_blocked)
    out["unavailable_reason"] = PLAN_BLOCK_REASON if plan_blocked else None
    return out


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
        allow_live_on_fixtures: bool = False,
    ) -> None:
        self.client = client
        self._map_cache: dict[str, int] | None = None
        # rwa_id -> {issuer_id, issuer_name, crypto_id}
        self._issuer_index: dict[int, dict[str, Any]] | None = None
        self._issuer_detail_cache: dict[str, dict[str, Any]] = {}
        self._session = session
        self._verifiers = verifiers if verifiers is not None else build_default_verifiers(session)
        # Fixture / offline demos skip network attestation calls.
        source = getattr(client, "source", "")
        if use_live_verifiers is None:
            self.use_live_verifiers = source != "fixture"
        else:
            self.use_live_verifiers = bool(use_live_verifiers)
        # Fail closed: a fixture client must not hit live HTTP unless a test
        # explicitly opts in. Judges running RWA_USE_FIXTURES=1 stay offline.
        self._fixture_live_blocked = False
        if source == "fixture" and self.use_live_verifiers and not allow_live_on_fixtures:
            self.use_live_verifiers = False
            self._fixture_live_blocked = True

    def _load_map_cache(self) -> dict[str, int]:
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
        return self._map_cache

    def _resolve_score_target(self, ticker: str) -> tuple[int | None, PorFeed | None]:
        """Map a ticker to a CMC/fixture row and/or a canonical Backed bToken.

        Canonical bTokens (``bNVDA``) may reuse the underlying unit's map row
        when one exists. If the map has no underlying, ``rwa_id`` is ``None``
        and scoring uses labeled missing-field defaults — never invented
        prices or CIKs.
        """
        cache = self._load_map_cache()
        feed = canonical_backed_por_feed(ticker)
        rwa_id = cache.get(ticker.upper())
        if rwa_id is None and feed is not None:
            for cand in backed_map_candidates(feed):
                mapped = cache.get(cand)
                if mapped is not None:
                    return mapped, feed
        if rwa_id is None and feed is None:
            raise ScoreError(f"{ticker} not found in RWA map")
        return rwa_id, feed

    def _resolve(self, ticker: str) -> int:
        rwa_id, feed = self._resolve_score_target(ticker)
        if rwa_id is None:
            symbol = feed.symbol if feed is not None else ticker
            raise ScoreError(f"{symbol} not found in RWA map")
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

    def _load_rwa_quotes(
        self, rwa_id: int | None
    ) -> tuple[dict[str, Any], list[str]]:
        """Fetch and normalize ``quotes/latest``. Empty dict when unused or missing."""
        flags: list[str] = []
        empty = parse_rwa_quotes_payload({})
        if rwa_id is None:
            return empty, flags
        fetch = getattr(self.client, "rwa_quotes", None)
        if not callable(fetch):
            return empty, flags
        try:
            raw = fetch(rwa_id=rwa_id)
        except Exception as exc:  # noqa: BLE001 — record, do not hide
            flags.append(f"RWA quotes lookup failed: {exc}")
            return empty, flags
        return parse_rwa_quotes_payload(raw), flags

    def _crypto_quote_price(
        self, crypto_id: Any
    ) -> tuple[float, dict[str, Any], list[str], str]:
        """Labeled fallback: ``/v2/cryptocurrency/quotes/latest`` 24h change."""
        flags: list[str] = []
        if not crypto_id:
            flags.append("No on-chain crypto_id linked to this RWA — price integrity unverified.")
            return (
                MISSING_CRYPTO_PRICE_SCORE,
                {
                    "available": False,
                    "crypto_id": None,
                    "percent_change_24h": None,
                    "source": SOURCE_CRYPTO_QUOTE,
                },
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
                {
                    "available": False,
                    "crypto_id": int(crypto_id),
                    "percent_change_24h": None,
                    "source": SOURCE_CRYPTO_QUOTE,
                },
                flags,
                f"Quote endpoint error; assigned the error default ({QUOTE_ERROR_PRICE_SCORE:.0f}).",
            )

        usd = (quote.get("quote") or {}).get("USD") or {}
        if not usd:
            flags.append("Quote payload had no USD block — price integrity unverified.")
            return (
                DEFAULT_PRICE_SCORE,
                {
                    "available": False,
                    "crypto_id": int(crypto_id),
                    "percent_change_24h": None,
                    "source": SOURCE_CRYPTO_QUOTE,
                },
                flags,
                f"Empty USD quote; assigned the unverified default ({DEFAULT_PRICE_SCORE:.0f}).",
            )

        raw_pct = usd.get("percent_change_24h")
        if raw_pct is None:
            flags.append("USD quote missing percent_change_24h — price integrity unverified.")
            return (
                DEFAULT_PRICE_SCORE,
                {
                    "available": False,
                    "crypto_id": int(crypto_id),
                    "percent_change_24h": None,
                    "source": SOURCE_CRYPTO_QUOTE,
                },
                flags,
                f"No 24h change in quote; assigned the unverified default ({DEFAULT_PRICE_SCORE:.0f}).",
            )

        pct = abs(float(raw_pct))
        score = price_score_from_deviation(pct)
        meta = {
            "available": True,
            "crypto_id": int(crypto_id),
            "percent_change_24h": float(raw_pct),
            "price": usd.get("price"),
            "volume_24h": usd.get("volume_24h"),
            "source": SOURCE_CRYPTO_QUOTE,
            "fallback": True,
        }
        return (
            score,
            meta,
            flags,
            (
                f"CMC crypto quote 24h change {float(raw_pct):+.2f}% "
                f"(RWA quotes/latest unused or unusable); "
                f"score = max({PRICE_SCORE_FLOOR:.0f}, 100 − |Δ| × "
                f"{PRICE_DEV_PENALTY:.0f}) = {score:.1f}."
            ),
        )

    def _price_score(
        self,
        rwa_quotes: dict[str, Any],
        crypto_id: Any,
        *,
        quote_flags: list[str] | None = None,
    ) -> tuple[float, dict[str, Any], list[str], str]:
        """Prefer RWA ``quotes/latest``; fall back to crypto 24hΔ. Never invent fields."""
        flags = list(quote_flags or [])
        tokens = wrappers_from_rwa_tokens(rwa_quotes.get("tokens") or [])
        avg = rwa_quotes.get("average_tokenized_price")
        try:
            avg_f = float(avg) if avg is not None else None
        except (TypeError, ValueError):
            avg_f = None
        if (avg_f is None or avg_f <= 0) and tokens:
            avg_f = sum(float(t["price"]) for t in tokens) / len(tokens)
            avg_label = "mean of priced tokens[] (CMC average_tokenized_price missing)"
        else:
            avg_label = "CMC average_tokenized_price"

        if avg_f is not None and avg_f > 0 and tokens:
            deviations = [abs(float(t["price"]) - avg_f) / avg_f * 100.0 for t in tokens]
            max_dev = max(deviations)
            score = price_score_from_deviation(max_dev)
            vol = rwa_quotes.get("tokenized_volume_24h")
            try:
                vol_f = float(vol) if vol is not None else None
            except (TypeError, ValueError):
                vol_f = None
            if vol_f is not None and vol_f == 0:
                flags.append("CMC tokenized_volume_24h is 0 — thin tape.")
                score = min(score, ZERO_VOLUME_PRICE_CAP)
            tradfi = rwa_quotes.get("tradfi_markets") or []
            meta = {
                "available": True,
                "source": SOURCE_RWA_QUOTES,
                "fallback": False,
                "crypto_id": int(crypto_id) if crypto_id else None,
                "percent_change_24h": None,
                "price": avg_f,
                "average_tokenized_price": avg_f,
                "tokenized_market_cap": rwa_quotes.get("tokenized_market_cap"),
                "tokenized_volume_24h": vol_f,
                "max_deviation_pct": max_dev,
                "avg_basis": avg_label,
                "tokens": tokens,
                "tradfi_markets": tradfi,
                "tradfi_venue_count": len(tradfi),
            }
            return (
                score,
                meta,
                flags,
                (
                    f"CMC RWA quotes/latest: {len(tokens)} priced token(s) vs "
                    f"{avg_label} {avg_f:.4f}; max |token−avg|/avg = {max_dev:.2f}%; "
                    f"tokenized mcap={rwa_quotes.get('tokenized_market_cap')}, "
                    f"vol_24h={vol_f}; "
                    f"{len(tradfi)} TradFi venue(s) listed (no TradFi last price in CMC). "
                    f"score = max({PRICE_SCORE_FLOOR:.0f}, 100 − |dev%| × "
                    f"{PRICE_DEV_PENALTY:.0f}) = {score:.1f}."
                ),
            )

        if flags and any("RWA quotes lookup failed" in item for item in flags):
            # Still try the crypto fallback so a quotes outage is not a silent 50.
            fallback_score, fallback_meta, extra, why = self._crypto_quote_price(crypto_id)
            flags.extend(extra)
            fallback_meta = dict(fallback_meta)
            fallback_meta["rwa_quotes_error"] = True
            return fallback_score, fallback_meta, flags, why

        if avg_f is not None and avg_f > 0 and not tokens:
            flags.append(
                "CMC RWA quotes returned average_tokenized_price but no priced tokens[] "
                "— integrity vs wrappers unverified."
            )
            # Display the aggregate; score uses the labeled unverified default.
            meta = {
                "available": False,
                "source": SOURCE_RWA_QUOTES,
                "fallback": False,
                "crypto_id": int(crypto_id) if crypto_id else None,
                "percent_change_24h": None,
                "price": avg_f,
                "average_tokenized_price": avg_f,
                "tokenized_market_cap": rwa_quotes.get("tokenized_market_cap"),
                "tokenized_volume_24h": rwa_quotes.get("tokenized_volume_24h"),
                "tokens": [],
                "tradfi_markets": rwa_quotes.get("tradfi_markets") or [],
            }
            return (
                DEFAULT_PRICE_SCORE,
                meta,
                flags,
                f"RWA quotes have an average ({avg_f:.4f}) but no priced tokens[]; "
                f"assigned the unverified default ({DEFAULT_PRICE_SCORE:.0f}).",
            )

        return self._crypto_quote_price(crypto_id)

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

    def _basis_score(
        self,
        rwa_id: int | None,
        rwa_quotes: dict[str, Any] | None = None,
        *,
        quote_flags: list[str] | None = None,
    ) -> tuple[float, dict[str, Any], list[str], str]:
        """Cross-issuer wrapper spread from RWA quotes tokens[] plus market-pairs."""
        flags = list(quote_flags or [])
        empty_meta: dict[str, Any] = {
            "available": False,
            "wrapper_count": 0,
            "percent_spread": None,
            "min_price": None,
            "max_price": None,
            "wrappers": [],
            "source": None,
            "tradfi_markets": [],
            "plan_blocked": False,
            "unavailable_reason": None,
        }
        if rwa_id is None:
            flags.append(
                "No CMC/fixture RWA row for this Backed bToken — cross-issuer basis unverified."
            )
            return (
                MISSING_BASIS_SCORE,
                empty_meta,
                flags,
                "Catalog-only Backed bToken (no map row); assigned the missing-pairs default "
                f"({MISSING_BASIS_SCORE:.0f}).",
            )

        quote_wrappers = wrappers_from_rwa_tokens((rwa_quotes or {}).get("tokens") or [])
        tradfi = list((rwa_quotes or {}).get("tradfi_markets") or [])
        pair_error: str | None = None
        pair_wrappers: list[dict[str, Any]] = []
        plan_blocked = False
        plan_block_note = (
            f"Cross-issuer basis: {PLAN_BLOCKED_LABEL} "
            "(CMC market-pairs not on this plan — not live market-pairs data)."
        )
        try:
            raw = self.client.market_pairs(rwa_id=rwa_id)
        except Exception as exc:  # noqa: BLE001 — record, do not hide
            pair_error = str(exc)
            if _exception_is_plan_blocked(exc):
                plan_blocked = True
                flags.append(plan_block_note)
            else:
                flags.append(f"Market-pairs lookup failed: {exc}")
        else:
            if market_pairs_plan_blocked(raw):
                plan_blocked = True
                flags.append(plan_block_note)
            else:
                payload = parse_market_pairs_payload(raw)
                pair_wrappers = group_wrapper_quotes(payload.get("market_pairs") or [])
                issuer_names = self._issuer_by_crypto_id()
                for wrapper in pair_wrappers:
                    cid = wrapper.get("crypto_id")
                    if cid is not None and not wrapper.get("issuer"):
                        wrapper["issuer"] = issuer_names.get(int(cid)) or ""
                    wrapper.setdefault("source", SOURCE_MARKET_PAIRS)

        wrappers = merge_basis_wrappers(quote_wrappers, pair_wrappers)
        used_quotes = bool(quote_wrappers)
        used_pairs = bool(pair_wrappers) and not plan_blocked
        if plan_blocked:
            used_pairs = False
        if used_quotes and used_pairs:
            source = SOURCE_RWA_AND_PAIRS
        elif used_quotes:
            source = SOURCE_RWA_QUOTES
        elif used_pairs:
            source = SOURCE_MARKET_PAIRS
        elif plan_blocked:
            source = SOURCE_MARKET_PAIRS_PLAN_BLOCKED
        else:
            source = None

        if not wrappers:
            if plan_blocked:
                return (
                    PLAN_BLOCKED_BASIS_SCORE,
                    _stamp_basis_meta(
                        {
                            **empty_meta,
                            "tradfi_markets": tradfi,
                            "source": SOURCE_MARKET_PAIRS_PLAN_BLOCKED,
                        },
                        plan_blocked=True,
                    ),
                    flags,
                    f"{plan_block_note} Assigned the plan-blocked default "
                    f"({PLAN_BLOCKED_BASIS_SCORE:.0f}).",
                )
            if pair_error and not used_quotes:
                return (
                    BASIS_ERROR_SCORE,
                    _stamp_basis_meta(
                        {**empty_meta, "tradfi_markets": tradfi},
                        plan_blocked=False,
                    ),
                    flags,
                    f"Market-pairs endpoint error; assigned the error default "
                    f"({BASIS_ERROR_SCORE:.0f}).",
                )
            flags.append(
                "No priced wrapper tokens on CMC RWA quotes or market-pairs — "
                "cross-issuer basis unverified."
            )
            return (
                MISSING_BASIS_SCORE,
                _stamp_basis_meta(
                    {**empty_meta, "tradfi_markets": tradfi, "source": source},
                    plan_blocked=False,
                ),
                flags,
                "No wrapper USD prices in RWA quotes tokens[] or market-pairs; "
                f"assigned the missing-pairs default ({MISSING_BASIS_SCORE:.0f}).",
            )

        if len(wrappers) == 1:
            only = wrappers[0]
            flags.append(
                "Only one wrapper token on CMC RWA quotes/market-pairs — cannot compare issuers."
            )
            meta = _stamp_basis_meta(
                {
                    "available": False,
                    "wrapper_count": 1,
                    "percent_spread": None,
                    "min_price": only["price"],
                    "max_price": only["price"],
                    "wrappers": wrappers,
                    "source": source,
                    "tradfi_markets": tradfi,
                },
                plan_blocked=plan_blocked,
            )
            why = (
                f"Single wrapper {only['symbol']} at {only['price']:.4f}; "
                f"assigned the single-wrapper default ({SINGLE_WRAPPER_BASIS_SCORE:.0f})."
            )
            if plan_blocked:
                why = f"{plan_block_note} {why}"
            return (
                SINGLE_WRAPPER_BASIS_SCORE,
                meta,
                flags,
                why,
            )

        prices = [float(w["price"]) for w in wrappers]
        spread = percent_spread(prices)
        if spread is None:
            flags.append("Could not compute a wrapper percent-spread from quotes/market-pairs.")
            return (
                MISSING_BASIS_SCORE,
                _stamp_basis_meta(
                    {
                        "available": False,
                        "wrapper_count": len(wrappers),
                        "percent_spread": None,
                        "min_price": min(prices) if prices else None,
                        "max_price": max(prices) if prices else None,
                        "wrappers": wrappers,
                        "source": source,
                        "tradfi_markets": tradfi,
                    },
                    plan_blocked=plan_blocked,
                ),
                flags,
                f"Invalid spread inputs; assigned the missing default ({MISSING_BASIS_SCORE:.0f}).",
            )

        score = basis_score_from_spread(spread)
        low, high = min(wrappers, key=lambda w: w["price"]), max(
            wrappers, key=lambda w: w["price"]
        )
        meta = _stamp_basis_meta(
            {
                "available": True,
                "wrapper_count": len(wrappers),
                "percent_spread": spread,
                "min_price": low["price"],
                "max_price": high["price"],
                "wrappers": wrappers,
                "source": source,
                "tradfi_markets": tradfi,
            },
            plan_blocked=plan_blocked,
        )
        cheap = f"{low['symbol']} {low['price']:.4f}"
        dear = f"{high['symbol']} {high['price']:.4f}"
        source_note = {
            SOURCE_RWA_AND_PAIRS: "CMC RWA quotes tokens[] + market-pairs",
            SOURCE_RWA_QUOTES: "CMC RWA quotes tokens[]",
            SOURCE_MARKET_PAIRS: "CMC market-pairs",
            SOURCE_MARKET_PAIRS_PLAN_BLOCKED: (
                f"CMC market-pairs {PLAN_BLOCKED_LABEL} — not live market-pairs data"
            ),
        }.get(source or "", "CMC wrapper prices")
        why = (
            f"{source_note}: {len(wrappers)} wrappers; spread {spread:.2f}% "
            f"({cheap} vs {dear}); "
            f"{len(tradfi)} TradFi venue(s) listed (no TradFi last price). "
            f"score = max({BASIS_SCORE_FLOOR:.0f}, 100 − |spread| × "
            f"{BASIS_SPREAD_PENALTY:.0f}) = {score:.1f}."
        )
        if plan_blocked:
            why = f"{plan_block_note} {why}"
        return (
            score,
            meta,
            flags,
            why,
        )

    def _heuristic_redemption(self, issuer_name: str) -> VerificationResult:
        """Name-list redemption when no live hook ran (unknown issuer or offline)."""
        if self.use_live_verifiers:
            reason = (
                "No live redemption verifier registered for this issuer; "
                "using name heuristic."
            )
            skip_note = "No live redemption verifier for this issuer."
        else:
            reason = "Fixture/offline mode — live redemption verifier skipped."
            skip_note = "Fixture/offline mode — live redemption verifier skipped."
        result = heuristic_result("redemption", issuer_name, reason=reason)
        # Intentional skip / unknown issuer — not a failed live call.
        result.ok = True
        result.notes = ["heuristic fallback", skip_note, HEURISTIC_NOTE]
        result.evidence = (
            f"heuristic fallback: issuer '{issuer_name or 'unknown'}' redemption "
            f"rights via name list ({reason.rstrip('.')})."
        )
        return result

    def _annotate_fixture_por_skip(
        self,
        result: VerificationResult,
        ticker: str,
    ) -> VerificationResult:
        """Label a published bToken feed when live RPC is skipped. Not on-chain PoR."""
        feed = canonical_backed_por_feed(ticker)
        if feed is None:
            return result
        note = fixture_por_skip_note(feed)
        if note not in result.notes:
            result.notes.append(note)
        if note not in result.evidence:
            result.evidence = f"{result.evidence} {note}"
        result.meta = {
            **result.meta,
            "published_por_feed": feed.symbol,
            "por_proxy": feed.proxy,
            "por_chain": feed.chain,
            "por_path": "fixture_labeled_skip",
        }
        return result

    def _verifier_for_ticker(self, ticker: str, issuer_name: str) -> Verifier | None:
        """Issuer registry first; canonical Backed bTokens always use BackedVerifier."""
        verifier = get_verifier_for_issuer(issuer_name, self._verifiers)
        if canonical_backed_por_feed(ticker) is None:
            return verifier
        backed = (self._verifiers or {}).get("backed")
        return backed if backed is not None else verifier

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
                return self._annotate_fixture_por_skip(
                    self._heuristic_redemption(issuer_name), ticker
                )
            result = heuristic_result(
                pillar,
                issuer_name,
                reason="Fixture/offline mode — live attestation verifiers skipped.",
            )
            # Fixture path is an intentional skip, keep labeled but ok=True for UX.
            result.ok = True
            return self._annotate_fixture_por_skip(result, ticker)

        verifier = self._verifier_for_ticker(ticker, issuer_name)
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
        log_start = 0
        if hasattr(self.client, "call_log"):
            try:
                log_start = len(self.client.call_log())
            except Exception:  # noqa: BLE001
                log_start = 0
        rwa_id, backed_feed = self._resolve_score_target(ticker)
        catalog_only = rwa_id is None
        if catalog_only:
            assert backed_feed is not None
            info = {
                "symbol": backed_feed.symbol,
                "name": backed_feed.name,
                "issuer": {"name": "Backed Finance"},
                "cik": None,
            }
            token: dict[str, Any] = {"issuer_name": "Backed Finance", "crypto_id": None}
            issuer_name = "Backed Finance"
        else:
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
            if backed_feed is not None and resolve_verifier_id(issuer_name) != "backed":
                issuer_name = "Backed Finance"
        flags = classify(issuer_name)
        symbol = backed_feed.symbol if backed_feed is not None else ticker.upper()

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
        if redemption_v.source == "heuristic_fallback":
            redemption_lead = (
                f"Heuristic: issuer '{issuer_name or 'unknown'}' matched the redeemable name list."
                if flags["redeemable"]
                else (
                    f"Heuristic: issuer '{issuer_name or 'unknown'}' treated as "
                    "sell-only (no redemption match)."
                )
            )
        else:
            redemption_lead = f"Live redemption check for '{issuer_name or 'unknown'}'."
        redemption_why = _append_verification_notes(redemption_lead, redemption_v)

        rwa_quotes, quote_flags = self._load_rwa_quotes(rwa_id)
        price_score, price_meta, price_flags, price_why = self._price_score(
            rwa_quotes, token.get("crypto_id"), quote_flags=quote_flags
        )
        price_source = str(price_meta.get("source") or SOURCE_CRYPTO_QUOTE)
        if price_meta.get("available") and price_source == SOURCE_RWA_QUOTES:
            price_evidence = (
                f"CMC RWA quotes/latest: avg={price_meta.get('average_tokenized_price')}; "
                f"max token deviation {price_meta.get('max_deviation_pct'):.2f}%; "
                f"tokenized mcap={price_meta.get('tokenized_market_cap')}, "
                f"vol_24h={price_meta.get('tokenized_volume_24h')}; "
                f"{price_meta.get('tradfi_venue_count') or 0} TradFi venue(s)."
            )
        elif price_meta.get("available"):
            price_evidence = (
                f"CMC crypto quote crypto_id={price_meta.get('crypto_id')}; "
                f"24hΔ={price_meta.get('percent_change_24h')} "
                f"(labeled fallback — RWA quotes unused or unusable)."
            )
        else:
            price_evidence = "CMC RWA quotes / crypto quote unavailable — self-reported gap."
        price_v = VerificationResult(
            score=price_score,
            level=VerificationLevel.SELF_REPORTED,
            evidence=price_evidence,
            source=price_source,
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

        basis_score, basis_meta, basis_flags, basis_why = self._basis_score(
            rwa_id, rwa_quotes, quote_flags=quote_flags
        )
        cheap = basis_meta.get("min_price")
        dear = basis_meta.get("max_price")
        spread_pct = basis_meta.get("percent_spread")
        plan_blocked = bool(basis_meta.get("plan_blocked"))
        default_source = (
            SOURCE_MARKET_PAIRS_PLAN_BLOCKED if plan_blocked else SOURCE_MARKET_PAIRS
        )
        basis_source = str(basis_meta.get("source") or default_source)
        source_label = {
            SOURCE_RWA_AND_PAIRS: "CMC RWA quotes tokens[] + market-pairs",
            SOURCE_RWA_QUOTES: "CMC RWA quotes tokens[]",
            SOURCE_MARKET_PAIRS: "CMC market-pairs",
            SOURCE_MARKET_PAIRS_PLAN_BLOCKED: (
                f"CMC market-pairs {PLAN_BLOCKED_LABEL} — not live market-pairs data"
            ),
        }.get(basis_source, "CMC wrapper prices")
        if plan_blocked:
            basis_evidence = (
                f"Cross-issuer basis: {PLAN_BLOCKED_LABEL} "
                "(CMC market-pairs not on this plan — not live market-pairs data)."
            )
            if basis_meta.get("available") and spread_pct is not None:
                basis_evidence += (
                    f" Quotes-only spread {spread_pct:.2f}% "
                    f"({basis_meta.get('wrapper_count')} wrappers)."
                )
            elif basis_meta.get("wrapper_count") == 1:
                only = (basis_meta.get("wrappers") or [{}])[0]
                basis_evidence += (
                    f" Quotes-only single wrapper {only.get('symbol') or 'unknown'}."
                )
        elif basis_meta.get("available"):
            basis_evidence = (
                f"{source_label}: {basis_meta.get('wrapper_count')} wrappers; "
                f"spread {spread_pct:.2f}% "
                f"(low {cheap}, high {dear})."
            )
        elif basis_meta.get("wrapper_count") == 1:
            only = (basis_meta.get("wrappers") or [{}])[0]
            basis_evidence = (
                f"{source_label}: single wrapper "
                f"{only.get('symbol') or 'unknown'} — no cross-issuer compare."
            )
        else:
            basis_evidence = "CMC RWA quotes / market-pairs unavailable — self-reported gap."
        basis_notes = [f"verification={VerificationLevel.SELF_REPORTED.value}"]
        if plan_blocked:
            basis_notes.append(PLAN_BLOCKED_LABEL)
        basis_v = VerificationResult(
            score=basis_score,
            level=VerificationLevel.SELF_REPORTED,
            evidence=basis_evidence,
            source=basis_source,
            notes=basis_notes,
            ok=bool(basis_meta.get("available")) and not plan_blocked,
            meta={"plan_blocked": plan_blocked},
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
            if price_meta.get("source") == SOURCE_RWA_QUOTES:
                risk_flags.append(
                    "Issuer tokens drifting from CMC average_tokenized_price — possible thin tape."
                )
            else:
                risk_flags.append(
                    "Token price drifting hard from the underlying — possible thin liquidity."
                )
        if basis_score < 50:
            risk_flags.append(
                "Wide cross-issuer wrapper spread — same ticker, different prices "
                "(CMC RWA quotes / market-pairs)."
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
        if backed_feed is not None and catalog_only:
            notes.append(
                f"{backed_feed.symbol} is a published Backed bToken from BACKED_POR_FEEDS; "
                "no CMC/fixture underlying row — price, disclosure, and basis use missing defaults."
            )
        elif backed_feed is not None and rwa_id is not None:
            notes.append(
                f"Scored {backed_feed.symbol} using the CMC/fixture row for underlying "
                f"{backed_feed.unit} (rwa_id={rwa_id})."
            )
        if not self.use_live_verifiers:
            notes.append(
                "Live attestation verifiers skipped (fixture/offline); "
                "backing/reserves use heuristic fallback."
            )
        if self._fixture_live_blocked:
            notes.append(
                "Fixture client blocked live verifiers "
                "(allow_live_on_fixtures=False) — labeled heuristic fallback."
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
            "verification_mode": "live" if self.use_live_verifiers else "offline_heuristic",
            "live_verifiers": bool(self.use_live_verifiers),
            "cmc_calls": self._cmc_calls_since(log_start),
        }

    def _cmc_calls_since(self, start: int) -> dict[str, Any]:
        source = getattr(self.client, "source", "unknown")
        rows: list[dict[str, Any]] = []
        fetch = getattr(self.client, "call_log", None)
        if callable(fetch):
            try:
                rows = list(fetch())[max(0, int(start)) :]
            except Exception:  # noqa: BLE001
                rows = []
        return summarize_call_log(rows, client_source=str(source))
