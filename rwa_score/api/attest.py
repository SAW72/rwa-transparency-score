"""Canonical score-hash for on-chain attestation.

The chain stores this digest only — never the raw score, band, or pillars.
SHA-256 of sorted JSON so the API and the sample client match without extra deps.

The payload always includes every live pillar in ``WEIGHTS``, including
**basis**. Omitting basis from a cited breakdown changes the hash.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from rwa_score.scorer import WEIGHTS

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
)
PILLAR_KEYS = tuple(WEIGHTS)


def _full_pillars(src: dict[str, Any] | None) -> dict[str, Any]:
    """Every live pillar, including basis. Missing keys stay explicit ``None``."""
    data = src or {}
    return {key: data.get(key) for key in PILLAR_KEYS}


def attestation_payload(report: dict[str, Any]) -> dict[str, Any]:
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
    }


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def score_hash(report: dict[str, Any]) -> str:
    """Return ``0x`` + 32-byte hex digest of the attestation payload."""
    digest = hashlib.sha256(canonical_bytes(attestation_payload(report))).hexdigest()
    return "0x" + digest


def history_json(report: dict[str, Any]) -> str:
    return canonical_bytes(attestation_payload(report)).decode("ascii")
