"""Side-by-side demo: one solid issuer, one thin wrapper, one mid-tier name.

Defaults to bundled fixtures so judges can run this without a CMC key.

Run:  python -m rwa_score.demo
      python -m rwa_score.demo --live
"""

from __future__ import annotations

import argparse
import sys

from .client import create_client
from .scorer import ScoreError, TransparencyScorer

DEMO_TICKERS = ["NVDA", "TSLA", "AAPL"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Print a three-ticker transparency demo.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Call the live CMC API (requires CMC_API_KEY).",
    )
    args = parser.parse_args(argv)

    try:
        client = create_client(use_fixtures_mode=not args.live)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR — {exc}", file=sys.stderr)
        return 2

    scorer = TransparencyScorer(client)
    source = getattr(client, "source", "unknown")
    print(f"DATA SOURCE: {'bundled DEMO FIXTURES (not live CMC)' if source == 'fixture' else 'live CMC API'}")
    print(f"{'TICKER':<7}{'SCORE':>7}  BAND")
    print("-" * 60)
    for t in DEMO_TICKERS:
        try:
            r = scorer.score(t)
            print(f"{r['ticker']:<7}{r['score']:>6.1f}  {r['band']}")
            for f in r["flags"]:
                print(f"         · {f}")
        except (ScoreError, Exception) as exc:  # noqa: BLE001
            print(f"{t:<7}  —  {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
