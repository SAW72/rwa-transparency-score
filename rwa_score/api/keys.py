"""Create / list / revoke API keys. Prints the raw secret only at create time."""

from __future__ import annotations

import argparse
import sys

from .settings import ApiSettings
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage RAT Score API keys.")
    parser.add_argument(
        "--db",
        default="",
        help="SQLite path (default: RWA_API_DB_PATH or data/rat_api.sqlite)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    create = sub.add_parser("create", help="Mint a new key (printed once).")
    create.add_argument("--name", required=True)
    create.add_argument("--tier", choices=["free", "paid"], default="free")

    sub.add_parser("list", help="List key prefixes and tiers (never the secret).")

    revoke = sub.add_parser("revoke", help="Revoke by printed prefix (first 12 chars).")
    revoke.add_argument("--prefix", required=True)

    args = parser.parse_args(argv)
    settings = ApiSettings.from_env()
    path = args.db or str(settings.db_path)
    store = Store(path)
    try:
        if args.cmd == "create":
            raw = store.create_key(name=args.name, tier=args.tier)
            rec = store.lookup_key(raw)
            assert rec is not None
            print(f"name={rec.name} tier={rec.tier} prefix={rec.key_prefix}")
            print(raw)
            print("Store this secret now. It is not written to the database in plaintext.", file=sys.stderr)
            return 0
        if args.cmd == "list":
            for rec in store.list_keys():
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
