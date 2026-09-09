"""Side-by-side demo: one solid issuer, one thin wrapper, one dead volume.

Run:  python -m rwa_score.demo
"""

from .client import CMCClient
from .scorer import TransparencyScorer

DEMO_TICKERS = ["NVDA", "TSLA", "AAPL"]  # swap for real RWA symbols once mapped


def main() -> None:
    scorer = TransparencyScorer(CMCClient())
    print(f"{'TICKER':<7}{'SCORE':>7}  BAND")
    print("-" * 60)
    for t in DEMO_TICKERS:
        try:
            r = scorer.score(t)
            print(f"{r['ticker']:<7}{r['score']:>6.1f}  {r['band']}")
            for f in r["flags"]:
                print(f"         · {f}")
        except Exception as exc:  # noqa: BLE001
            print(f"{t:<7}  —  {exc}")


if __name__ == "__main__":
    main()
