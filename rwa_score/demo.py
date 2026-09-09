"""Side-by-side demo: one solid issuer, one mid issuer, one thin wrapper.

Run (no API key):  python -m rwa_score.demo
"""

from .client import create_client
from .scorer import TransparencyScorer

# Fixture bundle covers these three; swap for any CMC RWA symbol in live mode.
DEMO_TICKERS = ["NVDA", "TSLA", "AAPL"]


def main() -> None:
    client = create_client()
    scorer = TransparencyScorer(client)
    print(f"source={client.source}")
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
