"""Authenticated RAT Score HTTP API.

Wraps ``TransparencyScorer`` / ``create_client`` so numbers match Streamlit.
"""

from __future__ import annotations

import base64
import logging
import math
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

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
from .auto_attest import (
    AttestWorker,
    AttesterSettings,
    SubmitResult,
    chain_status,
)
from .confidence import compute_confidence
from .settings import ApiSettings
from .store import ApiKey, Store, open_store

logger = logging.getLogger(__name__)

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


def _http_error(status: int, error: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": error, "message": message, **extra})


class _SealedScore(dict):
    """Score dict plus the one canonical attestation built for this request.

    ``canonical`` and ``attestation_payload`` are attributes, not keys, so
    they stay out of the JSON body. ``POST /v1/attest`` hashes these bytes
    instead of calling ``time.time()`` again.
    """

    canonical: bytes
    attestation_payload: dict[str, Any]


def _first_non_finite(value: Any, path: str = "") -> str | None:
    """Dotted path of the first NaN or Infinity, or ``None`` when every number is finite."""
    if isinstance(value, float) and not math.isfinite(value):
        return path or "value"
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            found = _first_non_finite(item, child)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _first_non_finite(item, f"{path}[{index}]")
            if found:
                return found
    return None


def _sanitize_non_finite(value: Any) -> Any:
    """Copy ``value`` with NaN and Infinity replaced by ``None``.

    The score response uses this so ``/v1/score`` can return 200. Canonical
    attestation bytes still use ``allow_nan=False`` and are not built from
    the unsanitized report.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _sanitize_non_finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_non_finite(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_non_finite(item) for item in value]
    return value


def _finite_score(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


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


def _control_ticker(symbol: str) -> bool:
    """NUL and other control characters must be a 4xx, never a database error."""
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in symbol)


def _rpc_status(worker: AttestWorker, settings: AttesterSettings) -> str:
    """``ok``, ``down``, ``unset``, or ``unknown``. Uses a chain already built."""
    rpc = (getattr(settings, "rpc_url", "") or "").strip()
    chain = getattr(worker, "_chain", None)
    if chain is None:
        return "unset" if not rpc else "unknown"
    reader = getattr(chain, "chain_id", None)
    if not callable(reader):
        return "unknown"
    try:
        live = int(reader())
    except Exception:
        return "down"
    expected = int(getattr(settings, "chain_id", 0) or 0)
    if expected and live != expected:
        return "down"
    return "ok"


def _attester_balance(worker: AttestWorker, _settings: AttesterSettings) -> str:
    """``ok`` or ``unknown``. Does not open a new RPC client."""
    chain = getattr(worker, "_chain", None)
    reader = getattr(chain, "balance_wei", None) if chain is not None else None
    if not callable(reader):
        return "unknown"
    try:
        int(reader())
    except Exception:
        return "unknown"
    return "ok"


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
    db = store or open_store()
    if store is None and cfg.bootstrap_key:
        db.ensure_key(cfg.bootstrap_key, name="bootstrap", tier=cfg.bootstrap_tier)
    attester_cfg = attester if attester is not None else AttesterSettings.from_env()
    logger.info(
        "pending-tx state only backend=%s attester=%s",
        db.backend,
        "enabled" if attester_cfg.enabled else "disabled",
    )
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
            "this surface is API access and live attestation. Scores are not stored."
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
                "This endpoint needs a paid key (attestation).",
            )
        return key

    def score_ticker(symbol: str, key: ApiKey, *, for_attest: bool = False) -> dict[str, Any]:
        if _control_ticker(symbol):
            raise _http_error(400, "bad_ticker", "Ticker contains a control character.")
        try:
            report = get_scorer().score(symbol)
        except ScoreError as exc:
            raise _http_error(404, "not_found", str(exc)) from exc
        bad = _first_non_finite(report)
        if bad:
            if for_attest:
                raise _http_error(
                    422,
                    "non_finite_value",
                    f"Non-finite value in {bad}.",
                    field=bad,
                )
            report = _sanitize_non_finite(report)
        decorated = _decorate(report)
        return decorated

    @app.get("/health", response_model=None)
    def health():
        payload = build_health_payload()
        payload["api"] = True
        payload["version"] = __version__
        payload["process"] = "ok"
        payload["attester"] = "enabled" if attester_cfg.enabled else "disabled"
        payload["chain_id"] = cfg.attestation_chain_id
        payload["rpc"] = _rpc_status(worker, attester_cfg)
        payload["attester_balance"] = _attester_balance(worker, attester_cfg)
        payload["queue_depth"] = db.queue_depth()
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

    def _attest_body(
        report: _SealedScore,
        *,
        result: SubmitResult | None,
    ) -> dict[str, Any]:
        payload = report.attestation_payload
        raw = report.canonical
        digest = str(report["attestation"]["score_hash"])
        inputs = inputs_bytes(report)
        status = "disabled" if result is None else result.status
        tx_hash = None if result is None else result.tx_hash
        body: dict[str, Any] = {
            "ticker": report["ticker"],
            "score": None if not _finite_score(report.get("score")) else report.get("score"),
            "as_of": payload.get("as_of"),
            "score_hash": digest,
            "algo": ATTESTATION_ALGO,
            "canonical_b64": base64.b64encode(raw).decode("ascii"),
            "payload": payload,
            "inputs_b64": base64.b64encode(inputs).decode("ascii"),
            "tx_hash": tx_hash,
            "status": status,
            "chain": cfg.attestation_chain,
            "chain_id": cfg.attestation_chain_id,
            "contract": cfg.attestation_contract or None,
            "note": (
                "Save this JSON and pass it to verify --payload-file. "
                "The contract stores only the hash of these canonical bytes. "
                "The hash is SHA-256 of those bytes, the bytes32 submitted to "
                "attest. The contract does not hash the payload itself. "
                "Save the payload; a restart drops in-flight transaction state "
                "and does not keep a copy of this score. Mainnet is held."
            ),
        }
        if result is not None and result.reason:
            body["reason"] = result.reason
        if result is not None and result.message:
            body["message"] = result.message
        return body

    @app.post("/v1/attest/{ticker}")
    def attest(ticker: str, key: ApiKey = Depends(require_paid)) -> dict[str, Any]:
        """Score live, hash once, check attested, broadcast or return confirmed.

        The request body is not a hash. Only bytes computed here are sent.
        """
        report = score_ticker(ticker, key, for_attest=True)
        if not isinstance(report, _SealedScore):
            raise _http_error(500, "error", "Score was not sealed.")
        payload = report.attestation_payload
        digest = str(report["attestation"]["score_hash"])
        if not attester_cfg.enabled:
            return _attest_body(report, result=SubmitResult(
                status="disabled",
                reason="disabled",
                message=attester_cfg.disabled_reason,
            ))
        result = worker.submit(
            canonical=report.canonical,
            score_hash=digest,
            ticker=str(report["ticker"]),
            claimed_at=int(payload["as_of"]),
        )
        # Redacted detail stays in logs. The HTTP body gets the generic message.
        return _attest_body(report, result=result)

    @app.get("/v1/attest/{ticker}/status")
    def attest_status(
        ticker: str,
        tx_hash: str | None = Query(default=None),
        score_hash: str | None = Query(default=None),
        key: ApiKey = Depends(require_paid),
    ) -> dict[str, Any]:
        """Chain read only. Does not score and does not read process memory."""
        _ = key
        if _control_ticker(ticker):
            raise _http_error(400, "bad_ticker", "Ticker contains a control character.")
        symbol = ticker.strip().upper()
        chain = getattr(worker, "_chain", None)
        if chain is None and attester_cfg.rpc_url and attester_cfg.contract and attester_cfg.enabled:
            try:
                chain = worker.chain()
            except Exception:
                chain = None
        base = {
            "ticker": symbol,
            "chain": cfg.attestation_chain,
            "chain_id": cfg.attestation_chain_id,
            "contract": cfg.attestation_contract or attester_cfg.contract or None,
        }
        if chain is None:
            return {
                **base,
                "status": "unavailable",
                "error": "chain_unset",
                "message": "Status is read from the chain only. No RPC is configured.",
                "on_chain": {
                    "attested": False,
                    "tx": None,
                    "attestedAt": None,
                    "status": "unavailable",
                    "source": "chain",
                    "reason": "chain_unset",
                    "message": "Status is read from the chain only. No RPC is configured.",
                },
            }
        view = chain_status(chain, ticker=symbol, score_hash=score_hash, tx_hash=tx_hash)
        return {**base, "score_hash": view.get("score_hash"), "tx_hash": view.get("tx"), "status": view.get("status"), "on_chain": view}

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
