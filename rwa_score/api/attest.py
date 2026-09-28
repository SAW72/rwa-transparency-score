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

``scorer_version`` is the git SHA. ``RENDER_GIT_COMMIT`` (Render sets this
at runtime) wins. If that is empty, the value is ``git rev-parse HEAD``.
If that fails, the version is ``unknown``. It is not truncated, and it does
not read ``SOURCE_VERSION`` or ``GIT_COMMIT``. ``/health`` still uses its
own short SHA via :func:`rwa_score.health.deploy_git_sha`.

``as_of`` is unix seconds UTC. A report that already carries ``as_of`` keeps
that value. Fixture scores (``data_source == "fixture"``) use ``0`` because
there is no live observation clock — that keeps a fixture hash stable.
Every other score uses the current UTC unix second.

``inputs_digest`` is the same SHA-256-over-canonical-JSON function applied to
the CMC and Chainlink PoR inputs this score used. See
:func:`attestation_inputs`.

The payload always includes every live pillar in ``WEIGHTS``, including
**basis**. Omitting basis from a cited breakdown changes the hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from rwa_score.scorer import WEIGHTS

# rwa_score/api/attest.py → repository root.
_REPO_ROOT = Path(__file__).resolve().parents[2]

ATTESTATION_ALGO = "sha256"
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


def _scorer_version(report: dict[str, Any]) -> str:
    """Git SHA: ``RENDER_GIT_COMMIT``, then ``git rev-parse HEAD``, else ``unknown``.

    A report that already carries ``scorer_version`` keeps that value.
    """
    raw = report.get("scorer_version")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    env = (os.environ.get("RENDER_GIT_COMMIT") or "").strip()
    if env:
        return env
    return _git_rev_parse_head() or "unknown"


def attestation_inputs(report: dict[str, Any]) -> dict[str, Any]:
    """CMC and Chainlink PoR inputs this score used.

    The scorer does not keep raw HTTP bodies. The CMC block is the quote
    and market-pair values it kept (``price``, ``basis``) plus ``cik``,
    ``rwa_id``, ``issuer``, and ``data_source``. The process-local call
    journal is not included: a warm directory cache drops endpoints on
    the next score of the same ticker, and that must not change the hash.

    The Chainlink block is one object per pillar whose verification
    ``source`` is ``chainlink_por``. Fields are the feed and the round that
    was scored. ``rpc_url`` is left out — it is transport and may embed a
    provider secret.
    """
    verification = report.get("verification") or {}
    por: list[dict[str, Any]] = []
    if isinstance(verification, dict):
        for pillar in PILLAR_KEYS:
            block = verification.get(pillar) or {}
            if not isinstance(block, dict):
                continue
            if block.get("source") != "chainlink_por":
                continue
            meta = block.get("meta") or {}
            if not isinstance(meta, dict):
                meta = {}
            por.append({"pillar": pillar, **{key: meta.get(key) for key in _POR_INPUT_KEYS}})
    return {
        "cmc": {
            "price": report.get("price"),
            "basis": report.get("basis"),
            "cik": report.get("cik"),
            "rwa_id": report.get("rwa_id"),
            "issuer": report.get("issuer"),
            "data_source": report.get("data_source"),
        },
        "chainlink_por": por,
    }


def inputs_digest(report: dict[str, Any]) -> str:
    """``0x`` + SHA-256 of the canonical CMC / Chainlink input JSON."""
    return _hash_bytes(canonical_bytes(attestation_inputs(report)))


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
        verification[key] = {
            "score": block.get("score"),
            "level": block.get("level"),
            "source": block.get("source"),
        }
    return {
        "ticker": report.get("ticker"),
        "rwa_id": report.get("rwa_id"),
        "issuer": report.get("issuer"),
        "score": report.get("score"),
        "band": report.get("band"),
        "subscores": _full_pillars(report.get("subscores")),
        "weights": _full_pillars(report.get("weights") or dict(WEIGHTS)),
        "cik": report.get("cik"),
        "data_source": report.get("data_source"),
        "verification": verification,
        "basis": report.get("basis"),
        "as_of": _as_of(report, now),
        "scorer_version": _scorer_version(report),
        "inputs_digest": inputs_digest(report),
    }


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Canonical JSON bytes. See the module docstring for the exact rules."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _hash_bytes(raw: bytes) -> str:
    return "0x" + hashlib.sha256(raw).hexdigest()


def score_hash(report: dict[str, Any], *, now: float | None = None) -> str:
    """Return ``0x`` + 32-byte hex digest of the attestation payload."""
    return _hash_bytes(canonical_bytes(attestation_payload(report, now=now)))


def hash_canonical(raw: bytes) -> str:
    """SHA-256 of already-canonical JSON bytes. Same function as :func:`score_hash`."""
    return _hash_bytes(raw)


def history_json(report: dict[str, Any]) -> str:
    return canonical_bytes(attestation_payload(report)).decode("ascii")
