"""API configuration. Secrets are read from the environment only."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = Path("data/rat_api.sqlite")
FREE_DAILY_LIMIT = 50
RATE_WINDOW_SECONDS = 86_400.0
BASE_SEPOLIA_CHAIN_ID = 84532


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class ApiSettings:
    db_path: Path = DEFAULT_DB_PATH
    free_daily_limit: int = FREE_DAILY_LIMIT
    rate_window_seconds: float = RATE_WINDOW_SECONDS
    bootstrap_key: str = ""
    bootstrap_tier: str = "paid"
    attestation_contract: str = ""
    attestation_chain: str = "base-sepolia"
    attestation_chain_id: int = BASE_SEPOLIA_CHAIN_ID
    max_compare_tickers: int = 8
    max_watchlist_tickers: int = 50
    webhook_timeout_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> ApiSettings:
        db = _env("RWA_API_DB_PATH")
        limit = _env("RWA_API_FREE_DAILY_LIMIT")
        window = _env("RWA_API_RATE_WINDOW_SECONDS")
        chain_id = _env("RWA_ATTESTATION_CHAIN_ID")
        return cls(
            db_path=Path(db) if db else DEFAULT_DB_PATH,
            free_daily_limit=int(limit) if limit else FREE_DAILY_LIMIT,
            rate_window_seconds=float(window) if window else RATE_WINDOW_SECONDS,
            bootstrap_key=_env("RWA_API_BOOTSTRAP_KEY"),
            bootstrap_tier=(_env("RWA_API_BOOTSTRAP_TIER") or "paid").lower(),
            attestation_contract=_env("RWA_ATTESTATION_CONTRACT"),
            attestation_chain=_env("RWA_ATTESTATION_CHAIN") or "base-sepolia",
            attestation_chain_id=int(chain_id) if chain_id else BASE_SEPOLIA_CHAIN_ID,
        )
