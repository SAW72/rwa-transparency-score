"""Known RWA issuers and their transparency characteristics.

These are starter heuristics, not verified legal truth. CMC issuer endpoints
return name / website / token roster — not custody, audit, or redemption
rights — so we match the issuer name against a documented allow-list.

Cross-check each issuer against the live CMC issuers endpoint and the
issuer's own attestations before treating a score as authoritative.
"""

from __future__ import annotations

import re

# Issuers that hold real shares with a regulated custodian (shareholder-of-record).
FULLY_BACKED = {
    "backed finance",
    "backed assets",
    "backed",
    "xstocks",
    "xstock",
    "securitize",
    "ondo",
    "paxos",
}
# Issuers that publish independent, on-chain proof of reserves (e.g. Chainlink).
AUDITED = {"backed finance", "backed assets", "backed", "ondo", "paxos"}
# Issuers offering true redemption for the underlying share (not sell-only).
REDEEMABLE = {
    "backed finance",
    "backed assets",
    "backed",
    "xstocks",
    "xstock",
    "securitize",
    "ondo",
}


def _word_match(name: str, keywords: set[str]) -> bool:
    """Match keywords on word boundaries so 'backed' does not hit 'Backpack'."""
    haystack = (name or "").strip().lower()
    if not haystack:
        return False
    for key in keywords:
        if re.search(rf"(?<!\w){re.escape(key)}(?!\w)", haystack):
            return True
    return False


def classify(issuer_name: str) -> dict[str, bool]:
    return {
        "backed": _word_match(issuer_name, FULLY_BACKED),
        "audited": _word_match(issuer_name, AUDITED),
        "redeemable": _word_match(issuer_name, REDEEMABLE),
    }
