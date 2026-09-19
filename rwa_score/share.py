"""Build a fixture (or live) score card and optionally post it to X.

Film / operator CLI — does not run on Streamlit page load.

    python -m rwa_score.share --fixtures NVDA
    python -m rwa_score.share --fixtures bNVDA

Posts only when ``X_API_KEY``, ``X_API_SECRET``, ``X_ACCESS_TOKEN``, and
``X_ACCESS_TOKEN_SECRET`` are set in the environment. Never prints secrets.
On success prints the tweet URL. Does not invent a URL when the post is skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .client import create_client
from .score_card import share_score_card
from .scorer import ScoreError, TransparencyScorer
from .x_client import (
    MISSING_CREDS_MESSAGE,
    X_POST_UNAVAILABLE_MESSAGE,
    XClient,
    x_credentials_ready,
)


def main(argv: list[str] | None = None, *, x_client: Any | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a RAT score-card PNG and post to X when credentials are set."
    )
    parser.add_argument(
        "ticker",
        nargs="?",
        default="NVDA",
        help="Ticker to score (default NVDA). bNVDA is also valid on fixtures.",
    )
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="Use bundled demo fixtures (no CMC_API_KEY, no live credits).",
    )
    parser.add_argument(
        "--no-post",
        action="store_true",
        help="Build the PNG only. Do not call the X API.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable result (no secrets).",
    )
    args = parser.parse_args(argv)

    try:
        client = create_client(use_fixtures_mode=True if args.fixtures else None)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR — {exc}", file=sys.stderr)
        return 2

    scorer = TransparencyScorer(client)
    try:
        report = scorer.score(args.ticker)
    except (ScoreError, Exception) as exc:  # noqa: BLE001
        print(f"ERROR — {args.ticker}: {exc}", file=sys.stderr)
        return 2

    post_to_x = not args.no_post
    active_x = x_client
    if post_to_x and active_x is None:
        active_x = XClient.from_env()
        creds_ready = x_credentials_ready(active_x.creds)
    elif post_to_x:
        creds = getattr(active_x, "creds", None)
        creds_ready = creds.complete if creds is not None else True
    else:
        creds_ready = False

    card = share_score_card(
        report,
        post_to_x=post_to_x,
        x_client=active_x if post_to_x else None,
    )

    payload = _result_payload(
        card,
        report,
        posted_attempted=post_to_x,
        creds_ready=creds_ready,
    )
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_text(payload)

    if not card.png_bytes:
        return 1
    if not post_to_x:
        return 0
    if card.x_posted and card.x_url:
        return 0
    if card.x_message == MISSING_CREDS_MESSAGE or not creds_ready:
        return 2
    return 1


def _result_payload(
    card: Any,
    report: dict[str, Any],
    *,
    posted_attempted: bool,
    creds_ready: bool,
) -> dict[str, Any]:
    posted = bool(getattr(card, "x_posted", False))
    url = getattr(card, "x_url", None) if posted else None
    message = str(getattr(card, "x_message", "") or "")
    if posted and url and not message.startswith("Posted to X:"):
        message = f"Posted to X: {url}"
    return {
        "ticker": str(report.get("ticker") or ""),
        "score": report.get("score"),
        "band": report.get("band"),
        "png_bytes": len(getattr(card, "png_bytes", b"") or b""),
        "filename": getattr(card, "filename", ""),
        "fingerprint": getattr(card, "fingerprint", ""),
        "x_posted": posted,
        "x_url": url,
        "x_message": message,
        "x_credentials_ready": creds_ready,
        "posted_attempted": posted_attempted,
    }


def _print_text(payload: dict[str, Any]) -> None:
    print(
        f"{payload['ticker']} score={payload['score']} [{payload['band']}] "
        f"png={payload['png_bytes']}B file={payload['filename']}"
    )
    if payload["x_posted"] and payload["x_url"]:
        print(payload["x_url"])
        return
    if not payload["posted_attempted"]:
        print("X post skipped (disabled).")
        return
    if not payload["x_credentials_ready"]:
        print(MISSING_CREDS_MESSAGE)
        return
    print(payload["x_message"] or X_POST_UNAVAILABLE_MESSAGE)


if __name__ == "__main__":
    sys.exit(main())
