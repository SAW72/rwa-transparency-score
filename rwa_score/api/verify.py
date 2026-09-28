"""Check a stored attestation payload against an optional on-chain record.

Does not re-score. Does not send transactions. The canonical JSON must
already have been stored by ``GET /v1/attest/{ticker}`` (or
``Store.save_attested_payload``). If nothing is stored, the response says
so and stops.

On-chain read uses ``cast call`` when Foundry is installed and
``--rpc-url`` / ``--contract`` (or env) are set. The digest passed to
``verify`` is recomputed from the stored bytes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .attest import canonical_bytes, hash_canonical
from .settings import ApiSettings
from .store import Store

# Type signature is unchanged. The uint256 is the contract's trusted
# attestedAt (block.timestamp at attest), not the attester's claimedAt.
# JSON `attested_at` is that int. cast 1.8.3 prints `1700000000 [1.7e9]`.
VERIFY_SIG = "verify(bytes32,string)(bool,uint256,address)"

NOTHING_STORED = (
    "No stored attestation payload for this ticker. "
    "verify does not re-score. "
    "GET /v1/attest/{ticker} stores the canonical JSON first. "
    "Render free disk is ephemeral, so a spin-down drops this file "
    "unless it lives on a persistent disk."
)


def on_chain_verify(
    *,
    contract: str,
    rpc_url: str,
    digest: str,
    ticker: str,
) -> dict[str, Any] | None:
    cast = shutil.which("cast")
    if not cast:
        return None
    try:
        out = subprocess.check_output(
            [
                cast,
                "call",
                contract,
                VERIFY_SIG,
                digest,
                ticker,
                "--rpc-url",
                rpc_url,
            ],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except (subprocess.CalledProcessError, OSError) as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    parts = [p.strip() for p in out.replace("\n", " ").split() if p.strip()]
    # cast prints bool / uint / address, one per line or space-separated.
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if len(lines) >= 3:
        flag, ts, attester = lines[0], lines[1], lines[2]
    elif len(parts) >= 3:
        flag, ts, attester = parts[0], parts[1], parts[2]
    else:
        return {"ok": False, "raw": out.strip()}
    return {
        "ok": flag.lower() in {"true", "1"},
        "attested_at": int(ts.split()[0]),
        "attester": attester,
        "raw": out.strip(),
    }


def _db_path(explicit: str) -> Path:
    if explicit.strip():
        return Path(explicit.strip())
    return ApiSettings.from_env().db_path


def _load_stored(store: Store, ticker: str, score_hash: str) -> dict[str, Any] | None:
    if score_hash:
        row = store.get_attested_payload(score_hash)
        if row is None:
            return None
        if row["ticker"] != ticker:
            return None
        return row
    return store.latest_attested_payload(ticker)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute a stored RAT Score hash and optionally check Base Sepolia. "
            "Does not re-score."
        )
    )
    parser.add_argument("ticker", help="Ticker whose stored payload to check, e.g. NVDA")
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="Accepted and ignored. This command does not re-score.",
    )
    parser.add_argument(
        "--api-url",
        default="",
        help="Accepted and ignored. This command does not re-score.",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="Accepted and ignored. This command does not re-score.",
    )
    parser.add_argument(
        "--hash",
        default="",
        dest="score_hash",
        help="Stored score hash to load (default: latest payload for the ticker)",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("RWA_API_DB_PATH", ""),
        help="SQLite path (default RWA_API_DB_PATH or data/rat_api.sqlite)",
    )
    parser.add_argument(
        "--contract",
        default=os.getenv("RWA_ATTESTATION_CONTRACT", ""),
        help="ScoreAttestation address (Base Sepolia)",
    )
    parser.add_argument(
        "--rpc-url",
        default=os.getenv("BASE_SEPOLIA_RPC_URL", ""),
        help="Base Sepolia RPC (env only; never a private key)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    ticker = args.ticker.strip().upper()
    db_path = _db_path(args.db)
    store = Store(db_path)
    try:
        row = _load_stored(store, ticker, args.score_hash.strip())
    finally:
        store.close()

    ignored = []
    if args.fixtures:
        ignored.append("--fixtures")
    if args.api_url:
        ignored.append("--api-url")
    if args.api_key:
        ignored.append("--api-key")
    ignored_note = ""
    if ignored:
        ignored_note = " Ignored " + ", ".join(ignored) + " (no live re-score)."

    if row is None:
        result: dict[str, Any] = {
            "ticker": ticker,
            "stored": False,
            "score_hash": None,
            "payload": None,
            "canonical": None,
            "on_chain": None,
            "match": None,
            "note": NOTHING_STORED + ignored_note,
        }
        _emit(result, as_json=args.json)
        return 0

    raw = row["canonical"]
    recomputed = hash_canonical(raw)
    payload = json.loads(raw.decode("utf-8"))
    # Re-canonicalizing after a key-order shuffle must match the stored bytes.
    hash_ok = recomputed == row["score_hash"] and canonical_bytes(payload) == raw
    result = {
        "ticker": payload.get("ticker") or ticker,
        "stored": True,
        "score": payload.get("score"),
        "band": payload.get("band"),
        "score_hash": recomputed,
        "stored_hash": row["score_hash"],
        "hash_ok": hash_ok,
        "algo": "sha256",
        "payload": payload,
        "canonical": raw.decode("ascii"),
        "on_chain": None,
        "match": None,
        "note": (
            "Hash recomputed from the stored canonical JSON. "
            "No live re-score. "
            "The contract stores this hash only — never the raw score. "
            "Mainnet is held; read Base Sepolia only."
            + ignored_note
        ),
    }
    if not hash_ok:
        result["note"] += " Stored bytes do not match the hash key."

    if args.contract and args.rpc_url:
        chain = on_chain_verify(
            contract=args.contract,
            rpc_url=args.rpc_url,
            digest=recomputed,
            ticker=str(result["ticker"]),
        )
        result["on_chain"] = chain
        if chain and chain.get("ok") is True and hash_ok:
            result["match"] = True
        elif chain and "error" not in chain:
            result["match"] = False
    elif args.contract or args.rpc_url:
        result["note"] += " Set both --contract and --rpc-url (or env) to read the chain."

    _emit(result, as_json=args.json)
    return 0


def _emit(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2))
        return
    if not result.get("stored"):
        print(f"{result['ticker']}: nothing stored")
        print(result["note"])
        return
    print(f"{result['ticker']} score={result['score']} [{result['band']}]")
    print(f"score_hash {result['score_hash']}")
    print(f"canonical  {result['canonical']}")
    if result["on_chain"] is None:
        print("on-chain   (skipped — pass --contract and --rpc-url to verify)")
    else:
        print(f"on-chain   {result['on_chain']}")
        print(f"match      {result['match']}")
    print(result["note"])


if __name__ == "__main__":
    raise SystemExit(main())
