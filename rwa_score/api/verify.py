"""Verify a live (or fixture) score hash against an optional on-chain record.

Does not send transactions. On-chain read uses ``cast call`` when Foundry is
installed and ``--rpc-url`` / ``--contract`` (or env) are set.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any

import requests

from rwa_score.client import create_client
from rwa_score.scorer import ScoreError, TransparencyScorer

from .attest import attestation_payload, canonical_bytes, score_hash

VERIFY_SIG = "verify(bytes32,string)(bool,uint256,address)"


def score_local(ticker: str, *, fixtures: bool | None) -> dict[str, Any]:
    client = create_client(use_fixtures_mode=True if fixtures else None)
    return TransparencyScorer(client).score(ticker)


def score_via_api(ticker: str, *, api_url: str, api_key: str) -> dict[str, Any]:
    url = api_url.rstrip("/") + f"/v1/score/{ticker}"
    resp = requests.get(url, headers={"X-API-Key": api_key}, timeout=30)
    if resp.status_code != 200:
        raise SystemExit(f"API {resp.status_code}: {resp.text[:400]}")
    return resp.json()


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
        "timestamp": ts,
        "attester": attester,
        "raw": out.strip(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Hash a live RAT Score and optionally check Base Sepolia."
    )
    parser.add_argument("ticker", help="Ticker to score, e.g. NVDA")
    parser.add_argument("--fixtures", action="store_true", help="Use bundled demo fixtures")
    parser.add_argument("--api-url", default=os.getenv("RWA_API_URL", ""), help="Score via HTTP API")
    parser.add_argument("--api-key", default=os.getenv("RWA_API_KEY", ""), help="API key (env only)")
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
    try:
        if args.api_url:
            if not args.api_key:
                print("API mode needs --api-key or RWA_API_KEY", file=sys.stderr)
                return 2
            report = score_via_api(ticker, api_url=args.api_url, api_key=args.api_key)
        else:
            report = score_local(ticker, fixtures=True if args.fixtures else None)
    except ScoreError as exc:
        print(f"ERROR — {exc}", file=sys.stderr)
        return 1

    digest = report.get("attestation", {}).get("score_hash") or score_hash(report)
    payload = attestation_payload(report)
    result: dict[str, Any] = {
        "ticker": report.get("ticker") or ticker,
        "score": report.get("score"),
        "band": report.get("band"),
        "score_hash": digest,
        "algo": "sha256",
        "payload": payload,
        "canonical": canonical_bytes(payload).decode("ascii"),
        "on_chain": None,
        "match": None,
        "note": (
            "Re-hash the payload locally and compare to score_hash. "
            "If they differ, the cited breakdown was edited. "
            "The contract stores this hash only — never the raw score. "
            "Mainnet is held; deploy Base Sepolia only."
        ),
    }

    if args.contract and args.rpc_url:
        chain = on_chain_verify(
            contract=args.contract,
            rpc_url=args.rpc_url,
            digest=digest,
            ticker=result["ticker"],
        )
        result["on_chain"] = chain
        if chain and chain.get("ok") is True:
            result["match"] = True
        elif chain and "error" not in chain:
            result["match"] = False
    elif args.contract or args.rpc_url:
        result["note"] += " Set both --contract and --rpc-url (or env) to read the chain."

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{result['ticker']} score={result['score']} [{result['band']}]")
        print(f"score_hash {result['score_hash']}")
        print(f"canonical  {result['canonical']}")
        if result["on_chain"] is None:
            print("on-chain   (skipped — pass --contract and --rpc-url to verify)")
        else:
            print(f"on-chain   {result['on_chain']}")
            print(f"match      {result['match']}")
        print(result["note"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
