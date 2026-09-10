#!/usr/bin/env python3
"""Sample client: hash a live/fixture RAT Score and check Base Sepolia.

    RWA_USE_FIXTURES=1 python scripts/verify_attestation.py NVDA --fixtures
    python scripts/verify_attestation.py NVDA --api-url http://127.0.0.1:8000 --api-key "$RWA_API_KEY"
    python scripts/verify_attestation.py NVDA --fixtures \\
        --contract "$RWA_ATTESTATION_CONTRACT" --rpc-url "$BASE_SEPOLIA_RPC_URL"

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
