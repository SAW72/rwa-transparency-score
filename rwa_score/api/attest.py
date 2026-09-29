"""Canonical score-hash for on-chain attestation.

The chain stores this digest only — never the raw score, band, or pillars.
The hash function is unchanged: SHA-256 over the canonical JSON bytes below,
returned as ``0x`` plus 64 hex characters. The API and the sample client
match without extra dependencies.

Canonical JSON (the bytes that are hashed and, once attested, stored):

- UTF-8 encoding of ``json.dumps``
- object keys sorted at every level (``sort_keys=True``)
- compact separators ``(",", ":")`` — no spaces
- ``ensure_ascii=True`` so non-ASCII is ``\\uXXXX`` (the resulting text is
  ASCII, which is valid UTF-8). Key order in the input dict does not matter.

``scorer_version`` is the git SHA, resolved once per process and cached:
``RENDER_GIT_COMMIT`` (Render sets this at runtime), else one
``git rev-parse HEAD``, else ``unknown``. A warning is logged when the
result is ``unknown``. It is not truncated, and it does not read
``SOURCE_VERSION`` or ``GIT_COMMIT``. ``/health`` still uses its own short
SHA via :func:`rwa_score.health.deploy_git_sha`. A report that already
carries ``scorer_version`` keeps that value.

``as_of`` is the attest time: the Unix second (UTC) at which this canonical
payload is hashed. It is not a provider observation timestamp. A report that
already carries ``as_of`` keeps that value. Fixture scores
(``data_source == "fixture"``) use ``0`` so a fixture hash stays stable —
that ``0`` is not a calendar time, and it must not be read as one.
Every other score uses one clock reading for that request.

``data_as_of`` is separate. It is the latest provider observation time
already on the report (Chainlink PoR ``updated_at``), or ``null`` when the
report has none. Fixtures have none. It is not fetched again at hash time.

``inputs_digest`` is the same SHA-256-over-canonical-JSON function applied to
an allowlist of scoring inputs: the CMC price and basis blocks, identity
fields, issuer heuristic flags, and each pillar's verifier ``meta`` (not only
Chainlink PoR). It is not a hash of raw provider HTTP bodies or of
explanation prose. Headers, API keys, tokens, and URLs are not on the
allowlist, so they are never hashed or stored. See :func:`attestation_inputs`.
Those input bytes are returned on the POST body so
:func:`recompute_inputs_digest` can rebuild the digest later.

Object key order and the absence of insignificant whitespace match RFC 8785.
``NaN`` and ``Infinity`` are rejected (``allow_nan=False``) because RFC 8785
does not allow them. Number formatting is Python ``json.dumps``, not the
full RFC 8785 numeric profile.

Live Base Sepolia attestations use contract
``0x2F073a3628D498d92956e7eFE2b26633eDa75b00``.

The payload always includes every live pillar in ``WEIGHTS``, including
**basis**. Omitting basis from a cited breakdown changes the hash.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from rwa_score.scorer import WEIGHTS

# rwa_score/api/attest.py → repository root.
_REPO_ROOT = Path(__file__).resolve().parents[2]

ATTESTATION_ALGO = "sha256"
# Live ScoreAttestation on Base Sepolia. Verify defaults to this address.
PINNED_ATTESTATION_CONTRACT = "0x2F073a3628D498d92956e7eFE2b26633eDa75b00"
_log = logging.getLogger(__name__)
_scorer_version_cache: str | None = None
ATTESTATION_FIELDS = (
    "ticker",
    "rwa_id",
    "issuer",
    "score",
    "band",
    "subscores",
    "weights",
    "cik",
    "data_source",
    "verification",
    "basis",
    "as_of",
    "data_as_of",
    "scorer_version",
    "inputs_digest",
)
PILLAR_KEYS = tuple(WEIGHTS)
# Fixture / offline scores have no live observation time.
FIXTURE_AS_OF = 0
_POR_INPUT_KEYS = (
    "symbol",
    "chain",
    "proxy",
    "reserves",
    "circulating_supply",
    "round_id",
    "updated_at",
    "unit",
)
# Keys that are transport or credentials. They are not scoring inputs.
# Matching is exact after lowercasing and turning hyphens into underscores.
# ``token`` is exact so the scoring field ``tokens`` stays.
_FORBIDDEN_INPUT_KEYS = frozenset(
    {
        "rpc_url",
        "url",
        "docs_url",
        "in_kind_docs_url",
        "market_url",
        "uri",
        "href",
        "endpoint",
        "headers",
        "header",
        "authorization",
        "cookie",
        "api_key",
        "apikey",
        "x_api_key",
        "access_token",
        "refresh_token",
        "token",
        "bearer",
        "secret",
        "password",
        "private_key",
    }
)
_DROP = object()
_QUOTE_ROW_SPEC: dict[str, Any] = {
    "crypto_id": None,
    "symbol": None,
    "price": None,
    "volume_24h": None,
    "venues": None,
    "issuer": None,
    "source": None,
}
_EXCHANGE_SPEC: dict[str, Any] = {"slug": None, "name": None, "exchange_id": None}
_TRADFI_SPEC: dict[str, Any] = {"exchange": _EXCHANGE_SPEC, "ticker": None}
_PRICE_SPEC: dict[str, Any] = {
    "available": None,
    "source": None,
    "fallback": None,
    "crypto_id": None,
    "percent_change_24h": None,
    "price": None,
    "average_tokenized_price": None,
    "tokenized_market_cap": None,
    "tokenized_volume_24h": None,
    "max_deviation_pct": None,
    "avg_basis": None,
    "tokens": [_QUOTE_ROW_SPEC],
    "tradfi_markets": [_TRADFI_SPEC],
    "tradfi_venue_count": None,
    "rwa_quotes_error": None,
    "volume_24h": None,
}
_BASIS_SPEC: dict[str, Any] = {
    "available": None,
    "wrapper_count": None,
    "percent_spread": None,
    "min_price": None,
    "max_price": None,
    "wrappers": [_QUOTE_ROW_SPEC],
    "source": None,
    "tradfi_markets": [_TRADFI_SPEC],
    "plan_blocked": None,
    "unavailable_reason": None,
}
_HEURISTIC_SPEC: dict[str, Any] = {
    "backed": None,
    "audited": None,
    "redeemable": None,
    "source": None,
    "labeled": None,
}
_META_SPEC: dict[str, Any] = {
    key: None
    for key in (
        "pillar",
        "matched",
        "issuer",
        "symbol",
        "chain",
        "proxy",
        "reserves",
        "circulating_supply",
        "collateralization_ratio",
        "ratio_ignored",
        "round_id",
        "updated_at",
        "unit",
        "ticker",
        "in_kind_to_shares",
        "kyc_or_whitelist",
        "settlement",
        "wrapper",
        "audit_firm",
        "has_big4",
        "has_alpaca",
        "has_one_to_one",
        "custody_provider",
        "has_redemption",
        "has_burn",
        "has_issuance",
        "has_brokerage",
    )
}


def _full_pillars(src: dict[str, Any] | None) -> dict[str, Any]:
    """Every live pillar, including basis. Missing keys stay explicit ``None``."""
    data = src or {}
    return {key: data.get(key) for key in PILLAR_KEYS}


def _as_of(report: dict[str, Any], now: float | None) -> int:
    if report.get("as_of") is not None:
        return int(report["as_of"])
    if str(report.get("data_source") or "") == "fixture":
        return FIXTURE_AS_OF
    clock = time.time() if now is None else float(now)
    return int(clock)


def _git_rev_parse_head() -> str:
    """Full ``git rev-parse HEAD`` output, or empty when git cannot answer."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_REPO_ROOT),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def clear_scorer_version_cache() -> None:
    """Drop the process cache. Tests use this; production resolves once."""
    global _scorer_version_cache
    _scorer_version_cache = None


def resolve_scorer_version() -> str:
    """Git SHA, once per process: ``RENDER_GIT_COMMIT``, else git, else ``unknown``."""
    global _scorer_version_cache
    if _scorer_version_cache is not None:
        return _scorer_version_cache
    env = (os.environ.get("RENDER_GIT_COMMIT") or "").strip()
    version = env or _git_rev_parse_head() or "unknown"
    _scorer_version_cache = version
    if version == "unknown":
        _log.warning(
            "scorer_version is unknown: RENDER_GIT_COMMIT is unset and git rev-parse HEAD failed"
        )
    return version


def _scorer_version(report: dict[str, Any]) -> str:
    """Git SHA for this report. An explicit ``scorer_version`` on the report wins."""
    raw = report.get("scorer_version")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return resolve_scorer_version()


def _input_key_forbidden(key: str) -> bool:
    norm = str(key).strip().lower().replace("-", "_")
    if norm in _FORBIDDEN_INPUT_KEYS:
        return True
    if "url" in norm or "header" in norm or "api_key" in norm:
        return True
    if norm == "token" or norm.endswith("_token"):
        return True
    return False


def _identity_field(value: Any) -> Any:
    """Scalar identity field. URLs and nested objects become ``None``."""
    projected = _project_scalar(value)
    if projected is _DROP:
        return None
    return projected


def _project_scalar(value: Any) -> Any:
    """Keep a scoring scalar. A URL string is dropped."""
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped.startswith(("http://", "https://", "ws://", "wss://")):
            return _DROP
    if isinstance(value, (dict, list)):
        return _DROP
    return value


def _project(value: Any, spec: Any) -> Any:
    """Copy ``value`` through an allowlist spec.

    A dict spec keeps only those keys. A one-item list spec projects each
    list element with that item spec. ``None`` keeps a scalar. Anything
    else, including headers, API keys, tokens, and URLs, is left out.
    """
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            return {}
        out: dict[str, Any] = {}
        for key, child in spec.items():
            if key not in value or _input_key_forbidden(key):
                continue
            projected = _project(value[key], child)
            if projected is _DROP:
                continue
            out[key] = projected
        return out
    if isinstance(spec, list):
        if not isinstance(value, list) or not spec:
            return []
        item_spec = spec[0]
        rows: list[Any] = []
        for item in value:
            projected = _project(item, item_spec)
            if projected is _DROP:
                continue
            rows.append(projected)
        return rows
    return _project_scalar(value)


def data_as_of(report: dict[str, Any]) -> int | None:
    """Latest provider observation time already on the report, or ``None``.

    Reads Chainlink PoR ``updated_at`` only. Does not call the clock and
    does not fetch a provider. Fixtures and scores with no round timestamp
    return ``None``.
    """
    verification = report.get("verification") or {}
    latest: int | None = None
    if not isinstance(verification, dict):
        return None
    for pillar in PILLAR_KEYS:
        block = verification.get(pillar) or {}
        if not isinstance(block, dict):
            continue
        meta = block.get("meta") or {}
        if not isinstance(meta, dict) or meta.get("updated_at") is None:
            continue
        try:
            stamp = int(meta["updated_at"])
        except (TypeError, ValueError):
            continue
        if latest is None or stamp > latest:
            latest = stamp
    return latest


def attestation_inputs(report: dict[str, Any]) -> dict[str, Any]:
    """Every scoring input retained on the report.

    The scorer does not keep raw HTTP bodies. The CMC block is the quote
    and market-pair values it kept (``price``, ``basis``) plus ``cik``,
    ``rwa_id``, ``issuer``, and ``data_source``. The process-local call
    journal is not included: a warm directory cache drops endpoints on
    the next score of the same ticker, and that must not change the hash.

    ``heuristics`` is the issuer name-list flags that set backing, reserves,
    and redemption when the live verifier did not run.

    ``verifiers`` is one object per pillar, including non-PoR pillars.
    Each object is ``source`` plus ``meta`` (the verifier's raw observations).
    Explanation prose (``evidence``, ``notes``, ``explanations``) is an
    output, not an input, and is left out.

    ``chainlink_por`` repeats the feed and round for each pillar whose
    ``source`` is ``chainlink_por``, so a PoR round can be read without
    walking every pillar. Only allowlisted scoring fields are copied.
    Headers, API keys, tokens, and URLs are left out.
    """
    verification = report.get("verification") or {}
    verifiers: dict[str, Any] = {}
    por: list[dict[str, Any]] = []
    if isinstance(verification, dict):
        for pillar in PILLAR_KEYS:
            block = verification.get(pillar) or {}
            if not isinstance(block, dict):
                block = {}
            meta = _project(block.get("meta"), _META_SPEC)
            source = block.get("source")
            if _project_scalar(source) is _DROP:
                source = None
            verifiers[pillar] = {"source": source, "meta": meta}
            if block.get("source") == "chainlink_por":
                por.append(
                    {"pillar": pillar, **{key: meta.get(key) for key in _POR_INPUT_KEYS}}
                )
    heuristics = report.get("heuristics")
    if isinstance(heuristics, dict):
        heuristics = _project(heuristics, _HEURISTIC_SPEC)
    else:
        heuristics = None
    return {
        "cmc": {
            "price": _project(report.get("price"), _PRICE_SPEC),
            "basis": _project(report.get("basis"), _BASIS_SPEC),
            "cik": _identity_field(report.get("cik")),
            "rwa_id": _identity_field(report.get("rwa_id")),
            "issuer": _identity_field(report.get("issuer")),
            "data_source": _identity_field(report.get("data_source")),
        },
        "heuristics": heuristics,
        "verifiers": verifiers,
        "chainlink_por": por,
    }


def inputs_digest(report: dict[str, Any]) -> str:
    """``0x`` + SHA-256 of the canonical scoring-input JSON."""
    return recompute_inputs_digest(attestation_inputs(report))


def recompute_inputs_digest(inputs: dict[str, Any]) -> str:
    """``0x`` + SHA-256 of already-built scoring inputs.

    ``verify`` calls this on the input bytes from ``--payload-file``. The
    result must equal ``payload["inputs_digest"]``.
    """
    return _hash_bytes(canonical_bytes(inputs))


def inputs_bytes(report: dict[str, Any]) -> bytes:
    """Canonical JSON bytes of :func:`attestation_inputs`. Returned on the POST body."""
    return canonical_bytes(attestation_inputs(report))


def attestation_payload(
    report: dict[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Stable subset of a scorer report. Editing a cited score changes the hash."""
    verification_in = report.get("verification") or {}
    verification: dict[str, Any] = {}
    for key in PILLAR_KEYS:
        block = verification_in.get(key) or {}
        source = block.get("source")
        if _project_scalar(source) is _DROP:
            source = None
        verification[key] = {
            "score": block.get("score"),
            "level": block.get("level"),
            "source": source,
        }
    return {
        "ticker": _identity_field(report.get("ticker")),
        "rwa_id": report.get("rwa_id"),
        "issuer": _identity_field(report.get("issuer")),
        "score": report.get("score"),
        "band": report.get("band"),
        "subscores": _full_pillars(report.get("subscores")),
        "weights": _full_pillars(report.get("weights") or dict(WEIGHTS)),
        "cik": _identity_field(report.get("cik")),
        "data_source": _identity_field(report.get("data_source")),
        "verification": verification,
        "basis": (
            _project(report.get("basis"), _BASIS_SPEC)
            if isinstance(report.get("basis"), dict)
            else report.get("basis")
        ),
        "as_of": _as_of(report, now),
        "data_as_of": data_as_of(report),
        "scorer_version": _scorer_version(report),
        "inputs_digest": inputs_digest(report),
    }


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Canonical JSON bytes. See the module docstring for the exact rules.

    ``allow_nan=False`` rejects ``NaN`` and ``Infinity`` (not valid JSON;
    excluded by RFC 8785).
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _peek_seal(report: dict[str, Any]) -> tuple[dict[str, Any], bytes, str] | None:
    """Return the payload already sealed on this report, if one build exists."""
    raw = getattr(report, "canonical", None)
    payload = getattr(report, "attestation_payload", None)
    if not isinstance(raw, (bytes, bytearray)) or not isinstance(payload, dict):
        return None
    blob = bytes(raw)
    return payload, blob, _hash_bytes(blob)


def canonical_for(
    report: dict[str, Any],
    *,
    now: float | None = None,
) -> tuple[dict[str, Any], bytes, str]:
    """One payload, the exact bytes that were hashed, and ``hash_canonical`` of those bytes.

    A report that already carries ``canonical`` / ``attestation_payload``
    (see :func:`rwa_score.api.app._decorate`) is not built again, so a later
    clock tick cannot fork the hash. Pass ``now`` only to force a new build.
    """
    if now is None:
        sealed = _peek_seal(report)
        if sealed is not None:
            return sealed
    payload = attestation_payload(report, now=now)
    raw = canonical_bytes(payload)
    return payload, raw, _hash_bytes(raw)


def _hash_bytes(raw: bytes) -> str:
    return "0x" + hashlib.sha256(raw).hexdigest()


def score_hash(report: dict[str, Any], *, now: float | None = None) -> str:
    """Return ``0x`` + 32-byte hex digest of the attestation payload."""
    _payload, _raw, digest = canonical_for(report, now=now)
    return digest


def hash_canonical(raw: bytes) -> str:
    """SHA-256 of already-canonical JSON bytes. Same function as :func:`score_hash`."""
    return _hash_bytes(raw)


def history_json(report: dict[str, Any]) -> str:
    """Canonical JSON text. Uses the sealed bytes when this request already built them."""
    _payload, raw, _digest = canonical_for(report)
    return raw.decode("ascii")
