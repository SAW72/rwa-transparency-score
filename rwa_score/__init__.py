"""RWA Transparency Score — risk radar for tokenized stocks."""

from .client import CMCClient, CMCError, create_client
from .scorer import PILLAR_HELP, WEIGHTS, TransparencyScorer

__version__ = "0.2.0"

__all__ = [
    "CMCClient",
    "CMCError",
    "PILLAR_HELP",
    "TransparencyScorer",
    "WEIGHTS",
    "create_client",
    "__version__",
]
