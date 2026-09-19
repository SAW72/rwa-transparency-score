"""Print / write the CMC RWA coverage matrix.

Fixture mode (default, no key):

    python -m scripts.cmc_rwa_coverage
    python -m scripts.cmc_rwa_coverage --write docs/CMC_RWA_COVERAGE.md

Live mode (needs CMC_API_KEY, never committed):

    CMC_API_KEY=… python -m scripts.cmc_rwa_coverage --live
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rwa_score.client import create_client
from rwa_score.coverage import coverage_from_client


def main() -> int:
    parser = argparse.ArgumentParser(description="CMC RWA coverage matrix")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use CMC_API_KEY (paginated map + assets/list). Never prints the key.",
    )
    parser.add_argument(
        "--write",
        type=Path,
        help="Write markdown to this path (UTF-8).",
    )
    args = parser.parse_args()
    client = create_client(use_fixtures_mode=not args.live)
    text = coverage_from_client(client)
    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(text, encoding="utf-8")
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
