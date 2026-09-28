"""Authenticated RAT Score HTTP API.

Wraps ``TransparencyScorer`` / ``create_client`` so numbers match Streamlit.
"""

from __future__ import annotations

import time
import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl

from rwa_score import __version__
from rwa_score.client import create_client
from rwa_score.health import build_health_payload
from rwa_score.scorer import ScoreError, TransparencyScorer

from .attest import (
    ATTESTATION_ALGO,
    ATTESTATION_FIELDS,
    canonical_for,
    inputs_bytes,
    resolve_scorer_version,
)
from .auto_attest import AttestWorker, AttesterSettings, on_chain_view
from .confidence import compute_confidence
from .settings import ApiSettings
from .store import ApiKey, Store, open_store
from .webhooks import apply_score_side_effects, assert_public_https_url

BREAKDOWN_KEYS = (
    "ticker",
    "rwa_id",
    "issuer",
    "score",
    "band",
    "band_label",
    "subscores",
    "weights",
    "pillars",
    "explanations",
    "verification",
    "flags",
    "notes",
    "heuristics",
    "data_source",
    "price",
    "basis",
    "cik",
    "issuer_note",
    "summary",
)


class WatchlistBody(BaseModel):
    tickers: list[str] = Field(default_factory=list)


class WebhookBody(BaseModel):
    url: HttpUrl
    secret: str | None = None
    ticker: str | None = None
    trigger: str = "band_cross"


def _http_error(status: int, error: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": error, "message": message, **extra})


class _SealedScore(dict):
    """Score dict plus the one canonical attestation built for this request.

    ``canonical`` and ``attestation_payload`` are attributes, not keys, so
    they stay out of the JSON body. Later history, webhooks, and
    ``GET /v1/attest`` must hash these bytes instead of calling
    ``time.time()`` again.
    """

    canonical: bytes
    attestation_payload: dict[str, Any]


def _decorate(report: dict[str, Any]) -> _SealedScore:
    """Scorer payload plus paid-layer fields. Breakdown keys stay byte-identical.

    The attestation payload is built once here. ``as_of`` is that hashing
    time (Unix seconds), fixed for every consumer of this object.
    """
    payload, raw, digest = canonical_for(report)
    out = _SealedScore(report)
    out.canonical = raw
    out.attestation_payload = payload
    out["confidence"] = compute_confidence(report)
    out["attestation"] = {
        "score_hash": digest,
        "algo": ATTESTATION_ALGO,
        "fields": list(ATTESTATION_FIELDS),
    }
    return out


def _extract_key(
    x_api_key: str | None,
    authorization: str | None,
) -> str:
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def create_app(
    *,
    settings: ApiSettings | None = None,
    store: Store | None = None,
    scorer: TransparencyScorer | None = None,
    poster=None,
    attester: AttesterSettings | None = None,
    chain=None,
    start_worker: bool = True,
) -> FastAPI:
    # Resolve once at process startup. Later scores reuse the cache.
    resolve_scorer_version()
    cfg = settings or ApiSettings.from_env()
    db = store or open_store(path=cfg.db_path, database_url=cfg.database_url)
    if store is None and cfg.bootstrap_key:
        db.ensure_key(cfg.bootstrap_key, name="bootstrap", tier=cfg.bootstrap_tier)
    attester_cfg = attester if attester is not None else AttesterSettings.from_env()
    worker = AttestWorker(
        store=db,
        settings=attester_cfg,
        chain=chain,
        autostart=start_worker,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        worker.kick()
        yield
        worker.stop()

    app = FastAPI(
        title="RAT Score API",
        description=(
            "Paid output layer for RAT Score. Scoring logic stays MIT-open; "
            "this surface is API access, history, webhooks, and attestation hashes."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.store = db
    app.state.scorer = scorer
    app.state.poster = poster
    app.state.attester = attester_cfg
    app.state.worker = worker

    def get_scorer() -> TransparencyScorer:
        if app.state.scorer is None:
            app.state.scorer = TransparencyScorer(create_client())
        return app.state.scorer

    def require_key(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        authorization: str | None = Header(default=None),
    ) -> ApiKey:
        raw = _extract_key(x_api_key, authorization)
        if not raw:
            raise _http_error(401, "unauthorized", "Missing API key. Send X-API-Key or Authorization: Bearer.")
        rec = db.lookup_key(raw)
        if rec is None or rec.revoked:
            raise _http_error(401, "unauthorized", "Invalid or revoked API key.")
        ok, _used = db.try_consume(
            rec.id,
            path=request.url.path,
            limit=None if rec.is_paid else cfg.free_daily_limit,
            window_seconds=cfg.rate_window_seconds,
        )
        if not ok:
            raise _http_error(
                429,
                "rate_limit",
                "Free tier limit reached.",
                tier="free",
                limit=cfg.free_daily_limit,
                window_seconds=cfg.rate_window_seconds,
            )
        return rec

    def require_paid(key: ApiKey = Depends(require_key)) -> ApiKey:
        if not key.is_paid:
            raise _http_error(
                403,
                "paid_required",
                "This endpoint needs a paid key (history, webhooks, attestation).",
            )
        return key

    def score_ticker(symbol: str, key: ApiKey) -> dict[str, Any]:
        try:
            report = get_scorer().score(symbol)
        except ScoreError as exc:
            raise _http_error(404, "not_found", str(exc)) from exc
        decorated = _decorate(report)
        apply_score_side_effects(
            db,
            decorated,
            key_id=key.id,
            poster=app.state.poster,
            timeout=cfg.webhook_timeout_seconds,
        )
        return decorated

    @app.get("/health")
    def health() -> dict[str, Any]:
        payload = build_health_payload()
        payload["api"] = True
        payload["version"] = __version__
        return payload

    @app.get("/v1/me")
    def me(key: ApiKey = Depends(require_key)) -> dict[str, Any]:
        since = time.time() - cfg.rate_window_seconds
        used = db.count_usage(key.id, since=since)
        remaining = None if key.is_paid else max(0, cfg.free_daily_limit - used)
        return {
            "name": key.name,
            "tier": key.tier,
            "key_prefix": key.key_prefix,
            "used": used,
            "limit": None if key.is_paid else cfg.free_daily_limit,
            "remaining": remaining,
            "window_seconds": cfg.rate_window_seconds,
        }

    @app.get("/v1/score/{ticker}")
    def get_score(ticker: str, key: ApiKey = Depends(require_key)) -> dict[str, Any]:
        return score_ticker(ticker, key)

    @app.get("/v1/compare")
    def compare(
        tickers: str = Query(..., description="Comma-separated tickers"),
        key: ApiKey = Depends(require_key),
    ) -> dict[str, Any]:
        symbols = [part.strip().upper() for part in tickers.split(",") if part.strip()]
        if not symbols:
            raise _http_error(400, "bad_request", "tickers query must list at least one symbol.")
        if len(symbols) > cfg.max_compare_tickers:
            raise _http_error(
                400,
                "bad_request",
                f"At most {cfg.max_compare_tickers} tickers per compare.",
            )
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            try:
                rows.append(score_ticker(symbol, key))
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
                rows.append({"ticker": symbol, "error": detail.get("message", "error")})
        return {"tickers": symbols, "scores": rows}

    @app.get("/v1/watchlist")
    def get_watchlist(key: ApiKey = Depends(require_key)) -> dict[str, Any]:
        symbols = db.get_watchlist(key.id)
        scores = []
        for symbol in symbols:
            try:
                scores.append(score_ticker(symbol, key))
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
                scores.append({"ticker": symbol, "error": detail.get("message", "error")})
        return {"tickers": symbols, "scores": scores}

    @app.put("/v1/watchlist")
    def put_watchlist(body: WatchlistBody, key: ApiKey = Depends(require_key)) -> dict[str, Any]:
        if len(body.tickers) > cfg.max_watchlist_tickers:
            raise _http_error(
                400,
                "bad_request",
                f"Watchlist cap is {cfg.max_watchlist_tickers} tickers.",
            )
        symbols = db.set_watchlist(key.id, body.tickers)
        return {"tickers": symbols}

    @app.post("/v1/watchlist")
    def post_watchlist(body: WatchlistBody, key: ApiKey = Depends(require_key)) -> dict[str, Any]:
        current = db.get_watchlist(key.id)
        if len(set(current) | {t.strip().upper() for t in body.tickers if t.strip()}) > cfg.max_watchlist_tickers:
            raise _http_error(
                400,
                "bad_request",
                f"Watchlist cap is {cfg.max_watchlist_tickers} tickers.",
            )
        symbols = db.add_watchlist(key.id, body.tickers)
        return {"tickers": symbols}

    @app.delete("/v1/watchlist/{ticker}")
    def delete_watchlist(ticker: str, key: ApiKey = Depends(require_key)) -> dict[str, Any]:
        db.remove_watchlist(key.id, ticker)
        return {"tickers": db.get_watchlist(key.id)}

    @app.get("/v1/history/{ticker}")
    def history(
        ticker: str,
        limit: int = Query(default=30, ge=1, le=200),
        key: ApiKey = Depends(require_paid),
    ) -> dict[str, Any]:
        return {
            "ticker": ticker.upper(),
            "history": db.get_history(ticker, limit=limit, key_id=key.id),
        }

    @app.post("/v1/webhooks")
    def create_webhook(body: WebhookBody, key: ApiKey = Depends(require_paid)) -> dict[str, Any]:
        if body.trigger not in {"band_cross", "below_orange"}:
            raise _http_error(400, "bad_request", "trigger must be band_cross or below_orange.")
        try:
            assert_public_https_url(str(body.url))
        except ValueError as exc:
            raise _http_error(400, "bad_request", str(exc)) from exc
        secret = body.secret or secrets.token_urlsafe(24)
        hook = db.add_webhook(
            key.id,
            url=str(body.url),
            secret=secret,
            ticker=body.ticker,
            trigger=body.trigger,
        )
        return {
            "id": hook.id,
            "url": hook.url,
            "ticker": hook.ticker,
            "trigger": hook.trigger,
            "secret": secret,
            "created_at": hook.created_at,
        }

    @app.get("/v1/webhooks")
    def list_webhooks(key: ApiKey = Depends(require_paid)) -> dict[str, Any]:
        hooks = [
            {
                "id": h.id,
                "url": h.url,
                "ticker": h.ticker,
                "trigger": h.trigger,
                "active": h.active,
                "created_at": h.created_at,
            }
            for h in db.list_webhooks(key.id)
        ]
        return {"webhooks": hooks}

    @app.delete("/v1/webhooks/{webhook_id}")
    def delete_webhook(webhook_id: int, key: ApiKey = Depends(require_paid)) -> dict[str, Any]:
        ok = db.deactivate_webhook(key.id, webhook_id)
        if not ok:
            raise _http_error(404, "not_found", "Webhook not found.")
        return {"id": webhook_id, "active": False}

    @app.get("/v1/attest/{ticker}")
    def attest(ticker: str, key: ApiKey = Depends(require_paid)) -> dict[str, Any]:
        report = score_ticker(ticker, key)
        # Same bytes history and webhooks just stored. Do not rebuild:
        # a second as_of = time.time() would return a hash with no row.
        payload, raw, digest = canonical_for(report)
        db.save_attested_payload(
            ticker=report["ticker"],
            canonical=raw,
            inputs=inputs_bytes(report),
        )
        if attester_cfg.enabled:
            db.enqueue_attest_job(
                score_hash=digest,
                ticker=report["ticker"],
                claimed_at=int(payload["as_of"]),
            )
            worker.kick()
        chain_view = on_chain_view(db, digest, attester_cfg)
        return {
            "ticker": report["ticker"],
            "score_hash": digest,
            "algo": ATTESTATION_ALGO,
            "payload": payload,
            "chain": cfg.attestation_chain,
            "chain_id": cfg.attestation_chain_id,
            "contract": cfg.attestation_contract or None,
            "stored": True,
            "on_chain": chain_view,
            "note": (
                "Call ScoreAttestation.attest(scoreHash, ticker, timestamp) "
                "on Base Sepolia from an authorized attester (owner or "
                "allowlisted relayer / API-held key). Attester is msg.sender "
                "(not calldata). The timestamp argument is stored only as "
                "claimedAt; the contract records block.timestamp as attestedAt. "
                "Pass payload.as_of as that timestamp. as_of is attest time: "
                "the Unix second when this payload was hashed, not when the "
                "data was observed. Fixture scores use as_of 0, which is not "
                "a calendar time. data_as_of is the latest provider observation "
                "time already on the report, or null. When the attester worker "
                "is enabled it enqueues that call off this response, using "
                "these same canonical bytes. The canonical JSON and scoring "
                "inputs are stored locally (Render free disk is ephemeral). "
                "The contract stores this hash only — never "
                "the raw score. Mainnet is held."
            ),
        }

    @app.get("/v1/attest/{ticker}/status")
    def attest_status(ticker: str, key: ApiKey = Depends(require_paid)) -> dict[str, Any]:
        """Latest stored payload and its on-chain job. Does not score or enqueue."""
        _ = key
        symbol = ticker.strip().upper()
        row = db.latest_attested_payload(symbol)
        if row is None:
            return {
                "ticker": symbol,
                "stored": False,
                "score_hash": None,
                "chain": cfg.attestation_chain,
                "chain_id": cfg.attestation_chain_id,
                "contract": cfg.attestation_contract or None,
                "on_chain": on_chain_view(db, "0x" + "00" * 32, attester_cfg),
            }
        digest = row["score_hash"]
        return {
            "ticker": row["ticker"],
            "stored": True,
            "score_hash": digest,
            "chain": cfg.attestation_chain,
            "chain_id": cfg.attestation_chain_id,
            "contract": cfg.attestation_contract or None,
            "on_chain": on_chain_view(db, digest, attester_cfg),
        }

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict):
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": "error", "message": str(exc.detail)},
        )

    return app


# uvicorn uses ``rwa_score.api.app:create_app --factory``.
