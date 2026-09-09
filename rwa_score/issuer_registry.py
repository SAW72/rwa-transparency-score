"""Known RWA issuers and their transparency characteristics.

These are starter **heuristics**, not verified truth. The UI and score report
must label them as heuristics. Cross-check each issuer against the live CMC
issuers endpoint and the issuer's own attestations before treating a score
as authoritative.
"""

from __future__ import annotations

HEURISTIC_NOTE = (
    "Issuer backing / proof-of-reserves / redemption flags are name-matching "
    "heuristics, not audited attestations."
)

# Issuers that hold real shares with a regulated custodian (shareholder-of-record).
FULLY_BACKED = {
    "backed finance",
    "backed",
    "xstocks",
    "securitize",
    "ondo",
    "paxos",
}
# Issuers that publish independent, on-chain proof of reserves (e.g. Chainlink).
AUDITED = {"backed finance", "backed", "ondo", "paxos"}
# Issuers offering true redemption for the underlying share (not sell-only).
REDEEMABLE = {"backed finance", "backed", "xstocks", "securitize", "ondo"}


def classify(issuer_name: str) -> dict[str, bool]:
    name = (issuer_name or "").strip().lower()
    return {
        "backed": any(k in name for k in FULLY_BACKED),
        "audited": any(k in name for k in AUDITED),
        "redeemable": any(k in name for k in REDEEMABLE),
    }
