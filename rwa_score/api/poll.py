"""One scoring cycle over every saved watchlist ticker.

Webhooks also fire inline after ``GET /v1/score`` / compare / watchlist.
This poller is the background/cron path for the same crossing check.

Side effects are applied per (key, ticker) so tenant A's watchlist cycle
never updates tenant B's last_bands or fires tenant B's webhooks.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict

from rwa_score.client import create_client
from rwa_score.scorer import ScoreError, TransparencyScorer

from .app import _decorate
from .settings import ApiSettings
from .store import Store
from .webhooks import apply_score_side_effects


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score watchlists and fire band-cross webhooks.")
    parser.add_argument("--db", default="")
    parser.add_argument("--fixtures", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    settings = ApiSettings.from_env()
    store = Store(args.db or str(settings.db_path))
    client = create_client(use_fixtures_mode=True if args.fixtures else None)
    scorer = TransparencyScorer(client)
    by_ticker: dict[str, list[int]] = defaultdict(list)
    for key_id, ticker in store.watchlist_entries():
        by_ticker[ticker].append(key_id)
    rows: list[dict] = []
    try:
        for ticker, key_ids in by_ticker.items():
            try:
                report = _decorate(scorer.score(ticker))
                for key_id in key_ids:
                    apply_score_side_effects(
                        store,
                        report,
                        key_id=key_id,
                        timeout=settings.webhook_timeout_seconds,
                    )
                rows.append(
                    {
                        "ticker": report["ticker"],
                        "score": report["score"],
                        "band": report["band"],
                        "score_hash": report["attestation"]["score_hash"],
                    }
                )
            except (ScoreError, Exception) as exc:  # noqa: BLE001
                rows.append({"ticker": ticker, "error": str(exc)})
    finally:
        store.close()

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        if not rows:
            print("No watchlist tickers.")
        for row in rows:
            if "error" in row:
                print(f"{row['ticker']}: ERROR — {row['error']}")
            else:
                print(f"{row['ticker']:<6} {row['score']:5.1f} [{row['band']}] {row['score_hash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
