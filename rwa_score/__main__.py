import argparse
import json
import sys

from .client import create_client
from .scorer import TransparencyScorer


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Score tokenized stocks on issuer transparency.")
    parser.add_argument("tickers", nargs="+", help="Tokenized stock tickers, e.g. NVDA TSLA")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="Use canned CMC responses (no API key, no network)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Force the live CoinMarketCap API (requires CMC_API_KEY)",
    )
    args = parser.parse_args(argv)

    if args.fixtures and args.live:
        parser.error("use either --fixtures or --live, not both")

    use_fixtures: bool | None
    if args.fixtures:
        use_fixtures = True
    elif args.live:
        use_fixtures = False
    else:
        use_fixtures = None

    client = create_client(use_fixtures=use_fixtures)
    scorer = TransparencyScorer(client)

    results = scorer.score_many(args.tickers)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(f"source={getattr(client, 'source', 'unknown')}")
        for r in results:
            if "error" in r:
                print(f"{r['ticker']}: ERROR — {r['error']}")
                continue
            print(f"{r['ticker']:<6} score={r['score']:5.1f}  [{r['band']}]  {r['summary']}")
            for flag in r.get("flags", []):
                print(f"         ! {flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
