"""Confidence label derived from pillar verification badges — not a new score."""

from __future__ import annotations

from typing import Any

from rwa_score.verifiers import VerificationLevel

LEVEL_WEIGHT = {
    VerificationLevel.ON_CHAIN_POR.value: 1.0,
    VerificationLevel.ATTESTED.value: 0.85,
    VerificationLevel.EXAMINED.value: 0.7,
    VerificationLevel.SELF_REPORTED.value: 0.5,
}


def compute_confidence(report: dict[str, Any]) -> dict[str, Any]:
    parts: list[float] = []
    for block in (report.get("verification") or {}).values():
        source = (block or {}).get("source") or ""
        if source == "heuristic_fallback":
            parts.append(0.3)
            continue
        parts.append(LEVEL_WEIGHT.get((block or {}).get("level"), 0.4))
    avg = sum(parts) / len(parts) if parts else 0.0
    if avg >= 0.75:
        label = "high"
    elif avg >= 0.5:
        label = "medium"
    else:
        label = "low"
    return {"score": round(avg, 3), "label": label}
