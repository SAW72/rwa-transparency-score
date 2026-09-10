"""Known RWA issuers and their transparency characteristics.

These are starter **heuristics**, not verified truth. The UI and score report
must label them as heuristics. Cross-check each issuer against the live CMC
issuers endpoint and the issuer's own attestations before treating a score
as authoritative.

Matching is allowlist + word-boundary only. The bare substring ``backed`` is
never treated as a positive signal — that naive ``k in name`` check was the
root of false positives such as "Not Backed At All", "UNBACKED", "feedbacked",
and "Anti-Ondo…". Negative tokens (not-backed, unbacked, anti-, "not backed")
reject before any positive allowlist hit. Live scoring and fixtures share
this path.
"""

from __future__ import annotations

import re

HEURISTIC_NOTE = (
    "Issuer backing / proof-of-reserves / redemption flags are name-matching "
    "heuristics, not audited attestations."
)

# Known-good issuer allowlist. Do NOT include the bare token "backed" —
# that substring matches adversarial / awkward names. Match these phrases
# at word boundaries only (exact name or contained as whole words).
FULLY_BACKED = {
    "backed finance",
    "xstocks",
    "securitize",
    "ondo",
    "paxos",
}
# Issuers that publish independent, on-chain proof of reserves (e.g. Chainlink).
AUDITED = {"backed finance", "ondo", "paxos"}
# Issuers offering true redemption for the underlying share (not sell-only).
REDEEMABLE = {"backed finance", "xstocks", "securitize", "ondo"}

# Applied to whitespace/hyphen-normalized names. Checked before positives.
_NEGATIVE_RE = re.compile(r"\bunbacked\b|\bnot backed\b|\banti\b")


def _normalize(issuer_name: str) -> str:
    name = (issuer_name or "").strip().lower()
    return re.sub(r"[\s_\-]+", " ", name).strip()


def _word_boundary_match(name: str, key: str) -> bool:
    """True if ``key`` appears in ``name`` as a whole word or phrase."""
    return re.search(r"\b" + re.escape(key) + r"\b", name) is not None


def _allowlist_hit(name: str, keys: set[str]) -> bool:
    return any(_word_boundary_match(name, key) for key in keys)


def classify(issuer_name: str) -> dict[str, bool]:
    """Return heuristic backing/audit/redemption flags for an issuer name.

    Negatives reject first. Positives require a word-boundary hit against the
    known-good allowlist — never a bare substring ``backed``.
    """
    name = _normalize(issuer_name)
    if not name or _NEGATIVE_RE.search(name):
        return {"backed": False, "audited": False, "redeemable": False}
    return {
        "backed": _allowlist_hit(name, FULLY_BACKED),
        "audited": _allowlist_hit(name, AUDITED),
        "redeemable": _allowlist_hit(name, REDEEMABLE),
    }
