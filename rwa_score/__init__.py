"""RAT Score (RWA Transparency Score) — risk radar for tokenized stocks."""

from .client import CMCClient, CMCError, FixtureClient, create_client, use_fixtures
from .scorer import PILLARS, WEIGHTS, ScoreError, TransparencyScorer, band_code
from .verifiers import VerificationLevel

__version__ = "0.3.0"

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
    "create_client",
    "use_fixtures",
]
