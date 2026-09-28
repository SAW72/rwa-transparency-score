#!/usr/bin/env python3
"""Recompute a stored RAT Score hash and optionally read Base Sepolia.

    python scripts/verify_attestation.py NVDA --db data/rat_api.sqlite
    python scripts/verify_attestation.py NVDA \\
        --contract "$RWA_ATTESTATION_CONTRACT" --rpc-url "$BASE_SEPOLIA_RPC_URL"

Does not re-score. The canonical JSON must already be stored (GET /v1/attest).
Exit codes: 0 match (or --offline local check; prints that nothing was
checked on-chain), 1 database missing or not SQLite, 2 nothing stored
(pre-fix attestation, stored payload unavailable), 3 bytes or inputs_digest
mismatch, 4 ticker / chain / verify() / attester mismatch (including
--hash for another ticker), 5 RPC failed, 6 cast missing, 7 no RPC URL
and --offline not passed, 8 inputs_json not stored.
A missing --db path is an error and is not created.
--contract defaults to 0x2F073a3628D498d92956e7eFE2b26633eDa75b00.
--fixtures / --api-url / --api-key warn and do not re-score.
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
