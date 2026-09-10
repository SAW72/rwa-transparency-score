"""Canonical score-hash for on-chain attestation.

The chain stores this digest only — never the raw score, band, or pillars.
SHA-256 of sorted JSON so the API and the sample client match without extra deps.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

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
)


def attestation_payload(report: dict[str, Any]) -> dict[str, Any]:
    """Stable subset of a scorer report. Editing a cited score changes the hash."""
    verification_in = report.get("verification") or {}
    verification = {
        key: {
            "score": (verification_in.get(key) or {}).get("score"),
            "level": (verification_in.get(key) or {}).get("level"),
            "source": (verification_in.get(key) or {}).get("source"),
        }
        for key in ("backing", "reserves", "redemption", "price", "disclosure")
        if key in verification_in
    }
    return {
        "ticker": report.get("ticker"),
        "rwa_id": report.get("rwa_id"),
        "issuer": report.get("issuer"),
        "score": report.get("score"),
        "band": report.get("band"),
        "subscores": report.get("subscores"),
        "weights": report.get("weights"),
        "cik": report.get("cik"),
        "data_source": report.get("data_source"),
        "verification": verification,
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
