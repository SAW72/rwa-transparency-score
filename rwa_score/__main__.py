import argparse
import json
import sys

from .client import CMCClient
from .scorer import TransparencyScorer


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Score tokenized stocks on issuer transparency.")
    parser.add_argument("tickers", nargs="+", help="Tokenized stock tickers, e.g. NVDA TSLA")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    client = CMCClient()
    scorer = TransparencyScorer(client)

    results = []
    for ticker in args.tickers:
        try:
            report = scorer.score(ticker)
            results.append(report)
        except Exception as exc:  # noqa: BLE001 — surface per-ticker errors, keep going
            results.append({"ticker": ticker, "error": str(exc)})

    if args.json:
        print(json.dumps(results, indent=2))
    else:
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
