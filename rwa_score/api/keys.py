"""Create / list / revoke API keys in this process only.

The store is memory. A key printed here is gone when this process exits.
Set ``RWA_API_BOOTSTRAP_KEY`` so the API process recreates a paid key on boot.
"""

from __future__ import annotations

import argparse
import sys

from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Manage RAT Score API keys in this process. "
            "They are not written to disk. Set RWA_API_BOOTSTRAP_KEY for the API."
        )
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    create = sub.add_parser("create", help="Mint a new key (printed once).")
    create.add_argument("--name", required=True)
    create.add_argument("--tier", choices=["free", "paid"], default="free")

    sub.add_parser("list", help="List key prefixes and tiers (never the secret).")

    revoke = sub.add_parser("revoke", help="Revoke by printed prefix (first 12 chars).")
    revoke.add_argument("--prefix", required=True)

    args = parser.parse_args(argv)
    store = Store()
    try:
        if args.cmd == "create":
            raw = store.create_key(name=args.name, tier=args.tier)
            rec = store.lookup_key(raw)
            assert rec is not None
            print(f"name={rec.name} tier={rec.tier} prefix={rec.key_prefix}")
            print(raw)
            print(
                "This key exists only in this process and is lost on exit. "
                "Set RWA_API_BOOTSTRAP_KEY to this secret and restart the API. "
                "The store keeps a SHA-256 hash, not the plaintext.",
                file=sys.stderr,
            )
            return 0
        if args.cmd == "list":
            found = store.list_keys()
            if not found:
                print("no keys in this process")
                return 0
            for rec in found:
                state = "revoked" if rec.revoked else "active"
                print(f"{rec.key_prefix:14} {rec.tier:4} {state:7} {rec.name}")
            return 0
        n = store.revoke_key(prefix=args.prefix)
        if n == 0:
            print(f"No active key with prefix {args.prefix}", file=sys.stderr)
            return 1
        print(f"revoked {args.prefix}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
