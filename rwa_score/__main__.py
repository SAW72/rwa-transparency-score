import argparse
import json
import sys

from .client import create_client
from .scorer import ScoreError, TransparencyScorer


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Score tokenized stocks on issuer transparency.")
    parser.add_argument("tickers", nargs="+", help="Tokenized stock tickers, e.g. NVDA TSLA")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="Use bundled demo fixture data (no CMC_API_KEY, no live credits).",
    )
    args = parser.parse_args(argv)

    try:
        client = create_client(use_fixtures_mode=True if args.fixtures else None)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR — {exc}", file=sys.stderr)
        return 2

    scorer = TransparencyScorer(client)
    results = []
    for ticker in args.tickers:
        try:
            results.append(scorer.score(ticker))
        except (ScoreError, Exception) as exc:  # noqa: BLE001 — surface per-ticker errors
            results.append({"ticker": ticker, "error": str(exc)})

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        source = getattr(client, "source", "unknown")
        if source == "fixture":
            print("DATA SOURCE: bundled DEMO FIXTURES (not live CMC)\n")
        for r in results:
            if "error" in r:
                print(f"{r['ticker']}: ERROR — {r['error']}")
                continue
            print(f"{r['ticker']:<6} score={r['score']:5.1f}  [{r['band']}]  {r['summary']}")
            for flag in r.get("flags", []):
                print(f"         ! {flag}")
            for note in r.get("notes", []):
                print(f"         · {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
