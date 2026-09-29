#!/usr/bin/env python3
"""Recompute a saved RAT Score hash and optionally read Base Sepolia.

    python scripts/verify_attestation.py NVDA --payload-file nvda.json --offline
    python scripts/verify_attestation.py NVDA \\
        --payload-file nvda.json \\
        --contract "$RWA_ATTESTATION_CONTRACT" --rpc-url "$BASE_SEPOLIA_RPC_URL" \\
        --attester "$RWA_ATTESTER_ADDRESS"

Does not re-score. Save ``canonical_payload`` from ``GET /v1/attest`` (or
status). The API keeps those bytes in memory and drops them on restart.
Exit codes: 0 match (or --offline local check; prints that nothing was
checked on-chain), 1 payload file missing or not a bundle, 2 nothing saved
(note: no stored payload, or the pre-fix wording when that hash is
on-chain), 3 bytes or inputs_digest mismatch, 4 ticker / chain / verify() /
attester mismatch (including --hash for another ticker), 5 RPC failed,
6 cast missing, 7 no RPC URL and --offline not passed, 8 inputs not in the
bundle. A missing --payload-file path is an error and is not created.
--contract defaults to 0x2F073a3628D498d92956e7eFE2b26633eDa75b00.
--fixtures / --api-url / --api-key warn and do not re-score.
The digest is SHA-256 of the canonical bytes, not keccak of the raw JSON.
Never pass a private key. This script only reads.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rwa_score.api.verify import main

if __name__ == "__main__":
    raise SystemExit(main())
