#!/usr/bin/env python3
"""Recompute a stored RAT Score hash and optionally read Base Sepolia.

    python scripts/verify_attestation.py NVDA --db data/rat_api.sqlite
    python scripts/verify_attestation.py NVDA \\
        --contract "$RWA_ATTESTATION_CONTRACT" --rpc-url "$BASE_SEPOLIA_RPC_URL"

Does not re-score. The canonical JSON must already be stored (GET /v1/attest).
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
