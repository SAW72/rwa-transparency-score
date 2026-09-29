"""Watchlist polling is not available.

Scores, watchlists, and webhook state are not stored. POST /v1/attest
computes a score live and returns the payload for the caller to save.
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "There is no watchlist store. "
            "POST /v1/attest/{ticker} scores live and returns the payload."
        )
    )
    parser.add_argument("--fixtures", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.parse_args(argv)
    print("no watchlist store; nothing to poll")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
