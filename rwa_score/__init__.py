"""RAT Score (RWA Transparency Score) — risk radar for tokenized stocks."""

from .client import CMCClient, CMCError, FixtureClient, create_client, parse_market_pairs_payload, use_fixtures
from .scorer import (
    PILLARS,
    WEIGHTS,
    ScoreError,
    TransparencyScorer,
    band_code,
    basis_score_from_spread,
    percent_spread,
)
from .verifiers import VerificationLevel

__version__ = "0.4.0"

__all__ = [
    "CMCClient",
    "CMCError",
    "FixtureClient",
    "PILLARS",
    "ScoreError",
    "TransparencyScorer",
    "VerificationLevel",
    "WEIGHTS",
    "__version__",
    "band_code",
    "basis_score_from_spread",
    "create_client",
    "parse_market_pairs_payload",
    "percent_spread",
    "use_fixtures",
]
