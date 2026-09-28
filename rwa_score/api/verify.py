"""Check a stored attestation payload against an optional on-chain record.

Does not re-score. Does not send transactions. The canonical JSON must
already have been stored by ``GET /v1/attest/{ticker}`` (or
``Store.save_attested_payload``). Hashes attested before those bytes were
stored, or lost when Render's free disk spun down, have no row. This
command exits non-zero and does not invent a payload.

Exit codes:

- ``0`` stored bytes match their hash, stored inputs recompute
  ``inputs_digest``, and the chain read matched. ``--offline`` is also
  ``0`` when the local checks pass; that mode prints that nothing was
  checked on-chain.
- ``1`` the database path does not exist, or the file is not a SQLite database
- ``2`` nothing stored (including a pre-fix attestation with no payload row)
- ``3`` stored bytes do not match the hash key, the payload is malformed,
  or stored inputs do not recompute ``inputs_digest``
- ``4`` the payload ticker, or the ticker on a ``--hash`` row, does not
  match the request; chain id is not 84532; verify() is false; or the
  attester mismatches
- ``5`` RPC / cast call failed
- ``6`` a chain read was requested but ``cast`` is not on ``PATH``
- ``7`` no ``--rpc-url`` and ``BASE_SEPOLIA_RPC_URL`` is unset, and
  ``--offline`` was not passed. Nothing was checked on-chain.
- ``8`` the row has no ``inputs_json``, so ``inputs_digest`` cannot be
  re-derived

On-chain read uses ``cast chain-id`` and ``cast call`` when ``--rpc-url``
(or ``BASE_SEPOLIA_RPC_URL``) is set. The contract defaults to the pinned
Base Sepolia deployment. Without a URL, pass ``--offline`` to check
stored bytes only.
The digest passed to ``verify`` is recomputed from the stored bytes.
``--fixtures``, ``--api-url``, and ``--api-key`` are obsolete: they warn
on stderr and do not re-score.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from .attest import (
    PINNED_ATTESTATION_CONTRACT,
    canonical_bytes,
    hash_canonical,
    recompute_inputs_digest,
)
from .settings import BASE_SEPOLIA_CHAIN_ID, ApiSettings
from .store import Store, open_store

EXIT_OK = 0
EXIT_DB = 1
EXIT_NOT_STORED = 2
EXIT_HASH_MISMATCH = 3
EXIT_NO_MATCH = 4
EXIT_RPC_ERROR = 5
EXIT_CAST_MISSING = 6
EXIT_CHAIN_UNCHECKED = 7
EXIT_INPUTS_MISSING = 8

# Type signature is unchanged. The uint256 is the contract's trusted
# attestedAt (block.timestamp at attest), not the attester's claimedAt.
# JSON `attested_at` is that int. cast 1.8.3 prints `1700000000 [1.7e9]`.
VERIFY_SIG = "verify(bytes32,string)(bool,uint256,address)"

NOTHING_STORED = (
    "pre-fix attestation, stored payload unavailable. "
    "No stored attestation payload for this ticker. "
    "verify does not re-score and will not rebuild a hash attested before "
    "canonical bytes were stored. "
    "GET /v1/attest/{ticker} stores a new payload; as_of is attest time, "
    "so that new hash differs from the pre-fix hash. "
    "Hashes dropped when Render's free disk spun down are the same case: "
    "the bytes are gone. "
    "A persistent disk (paid plan) or Postgres keeps the bytes across spin-down."
)

_LEGACY_FIELDS = ("as_of", "data_as_of", "scorer_version", "inputs_digest")

_OBSOLETE_FLAGS = (
    "WARNING: --fixtures, --api-url, and --api-key are Ignored. They do not re-score and are obsolete. "
    "verify only checks canonical bytes already stored by GET /v1/attest. "
    "Older on-chain hashes with no stored row cannot be rebuilt from fixtures or the API."
)


class _RpcError(Exception):
    """cast failed or returned something we cannot parse. Message is redacted."""


def _redact_rpc(text: str, rpc_url: str) -> str:
    if rpc_url:
        text = text.replace(rpc_url, "[rpc]")
    return text[:500]


def _cast_output(cast: str, args: list[str], rpc_url: str) -> str:
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = getattr(exc, "output", None) or str(exc)
        raise _RpcError(_redact_rpc(str(detail), rpc_url)) from None


def on_chain_verify(
    *,
    contract: str,
    rpc_url: str,
    digest: str,
    ticker: str,
    expected_attester: str = "",
) -> dict[str, Any]:
    """Read chain id and ``verify()``. Never returns success for a non-84532 chain.

    ``error`` is set for cast-missing and RPC failures. A wrong chain id or
    attester is ``ok: False`` without ``error``, so the caller exits
    ``EXIT_NO_MATCH`` rather than ``EXIT_RPC_ERROR``.
    """
    cast = shutil.which("cast")
    if not cast:
        return {"ok": False, "error": "cast_missing"}
    try:
        cid_raw = _cast_output(cast, [cast, "chain-id", "--rpc-url", rpc_url], rpc_url)
        chain_id = int(cid_raw.strip().split()[0])
    except (_RpcError, ValueError, IndexError) as exc:
        return {"ok": False, "error": _redact_rpc(str(exc), rpc_url)}
    if chain_id != BASE_SEPOLIA_CHAIN_ID:
        return {
            "ok": False,
            "chain_id": chain_id,
            "refused": "chain_id",
            "attester_ok": False,
        }
    try:
        out = _cast_output(
            cast,
            [cast, "call", contract, VERIFY_SIG, digest, ticker, "--rpc-url", rpc_url],
            rpc_url,
        )
    except _RpcError as exc:
        return {"ok": False, "chain_id": chain_id, "error": str(exc)}
    parts = [p.strip() for p in out.replace("\n", " ").split() if p.strip()]
    # cast prints bool / uint / address, one per line or space-separated.
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if len(lines) >= 3:
        flag, ts, attester = lines[0], lines[1], lines[2]
    elif len(parts) >= 3:
        flag, ts, attester = parts[0], parts[1], parts[2]
    else:
        return {"ok": False, "chain_id": chain_id, "error": "unparsed cast output", "raw": out.strip()}
    try:
        attested_at = int(ts.split()[0])
    except ValueError:
        return {"ok": False, "chain_id": chain_id, "error": "unparsed attested_at", "raw": out.strip()}
    expected = expected_attester.strip()
    attester_ok = bool(expected) and attester.lower() == expected.lower()
    return {
        "ok": flag.lower() in {"true", "1"},
        "chain_id": chain_id,
        "attested_at": attested_at,
        "attester": attester,
        "attester_ok": attester_ok,
        "expected_attester": expected or None,
        "raw": out.strip(),
    }


def _inputs_match(raw: bytes, claimed: Any) -> tuple[bool, dict[str, Any] | None]:
    """True when ``raw`` is canonical scoring inputs and hashes to ``claimed``."""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False, None
    if not isinstance(parsed, dict):
        return False, None
    try:
        if canonical_bytes(parsed) != raw:
            return False, parsed
        recomputed = recompute_inputs_digest(parsed)
    except (TypeError, ValueError):
        return False, parsed
    return recomputed == claimed, parsed


def _load_stored(store: Store, ticker: str, score_hash: str) -> dict[str, Any] | None:
    if score_hash:
        # A row for another ticker is a mismatch (exit 4), not "not stored".
        return store.get_attested_payload(score_hash)
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
        help="OBSOLETE. Warns and does not re-score. Stored bytes only.",
    )
    parser.add_argument(
        "--api-url",
        default="",
        help="OBSOLETE. Warns and does not re-score. Stored bytes only.",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="OBSOLETE. Warns and does not re-score. Stored bytes only.",
    )
    parser.add_argument(
        "--hash",
        default="",
        dest="score_hash",
        help="Stored score hash to load (default: latest payload for the ticker)",
    )
    parser.add_argument(
        "--db",
        default="",
        help="Force a SQLite file. Unset uses DATABASE_URL when set, else RWA_API_DB_PATH.",
    )
    parser.add_argument(
        "--contract",
        default=os.getenv("RWA_ATTESTATION_CONTRACT", "") or PINNED_ATTESTATION_CONTRACT,
        help=f"ScoreAttestation address (default {PINNED_ATTESTATION_CONTRACT})",
    )
    parser.add_argument(
        "--rpc-url",
        default=os.getenv("BASE_SEPOLIA_RPC_URL", ""),
        help="Base Sepolia RPC (env only; never a private key). Set to read the chain.",
    )
    parser.add_argument(
        "--attester",
        default=os.getenv("RWA_ATTESTER_ADDRESS", ""),
        help="Expected attester address (env RWA_ATTESTER_ADDRESS). Required for a chain match.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Check stored bytes only. Prints that nothing was checked on-chain. "
            "Without this flag, a missing --rpc-url and BASE_SEPOLIA_RPC_URL is an error."
        ),
    )
    args = parser.parse_args(argv)

    ticker = args.ticker.strip().upper()
    contract = (args.contract or "").strip() or PINNED_ATTESTATION_CONTRACT
    ignored = []
    if args.fixtures:
        ignored.append("--fixtures")
    if args.api_url:
        ignored.append("--api-url")
    if args.api_key:
        ignored.append("--api-key")
    ignored_note = ""
    if ignored:
        print(_OBSOLETE_FLAGS, file=sys.stderr)
        ignored_note = " " + _OBSOLETE_FLAGS

    explicit_db = args.db.strip()
    cfg = ApiSettings.from_env()
    use_postgres = (not explicit_db) and bool(cfg.database_url)
    db_path = Path(explicit_db) if explicit_db else cfg.db_path
    if not use_postgres and not db_path.is_file():
        result = {
            "ticker": ticker,
            "stored": False,
            "score_hash": None,
            "payload": None,
            "canonical": None,
            "contract": contract,
            "on_chain": None,
            "match": False,
            "hash_ok": False,
            "error": "database_missing",
            "note": (
                f"Database does not exist ({db_path}). "
                "verify does not create an empty database. "
                "A missing path is not the same as nothing attested."
                + ignored_note
            ),
        }
        _emit(result, as_json=args.json)
        return EXIT_DB

    try:
        if explicit_db:
            store = Store(explicit_db)
        else:
            store = open_store(path=cfg.db_path, database_url=cfg.database_url)
        try:
            row = _load_stored(store, ticker, args.score_hash.strip())
        finally:
            store.close()
    except (sqlite3.Error, RuntimeError):
        result = {
            "ticker": ticker,
            "stored": False,
            "score_hash": None,
            "payload": None,
            "canonical": None,
            "contract": contract,
            "on_chain": None,
            "match": False,
            "hash_ok": False,
            "error": "database_error",
            "note": "Database file is not a valid SQLite database." + ignored_note,
        }
        _emit(result, as_json=args.json)
        return EXIT_DB

    if row is None:
        result: dict[str, Any] = {
            "ticker": ticker,
            "stored": False,
            "score_hash": None,
            "payload": None,
            "canonical": None,
            "contract": contract,
            "on_chain": None,
            "match": False,
            "hash_ok": False,
            "note": NOTHING_STORED + ignored_note,
        }
        _emit(result, as_json=args.json)
        return EXIT_NOT_STORED

    raw = row["canonical"]
    if not isinstance(raw, (bytes, bytearray)):
        raw = str(raw).encode("utf-8")
    raw = bytes(raw)
    recomputed = hash_canonical(raw)
    payload: dict[str, Any] | None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        try:
            recanon = canonical_bytes(parsed) == raw
        except (TypeError, ValueError):
            recanon = False
        payload = parsed
    else:
        recanon = False
        payload = None
    hash_ok = recomputed == row["score_hash"] and recanon
    canonical_text = raw.decode("utf-8", errors="replace")
    result = {
        "ticker": ticker,
        "stored": True,
        "score": None if payload is None else payload.get("score"),
        "band": None if payload is None else payload.get("band"),
        "score_hash": recomputed,
        "stored_hash": row["score_hash"],
        "hash_ok": hash_ok,
        "algo": "sha256",
        "payload": payload,
        "canonical": canonical_text,
        "contract": contract,
        "on_chain": None,
        "match": None,
        "note": (
            "Hash recomputed from the stored canonical JSON. "
            "No live re-score. "
            "inputs_digest covers every scoring input retained on the report, "
            "not raw provider bodies. "
            "as_of is attest time (Unix seconds when the payload was hashed), "
            "not the provider observation time (data_as_of). "
            "The contract stores this hash only — never the raw score. "
            "Mainnet is held; read Base Sepolia only."
            + ignored_note
        ),
    }
    if not hash_ok:
        if not isinstance(parsed, dict):
            result["error"] = "malformed_payload"
            result["note"] += " Malformed stored payload."
        else:
            result["note"] += " Stored bytes do not match the hash key."
        result["match"] = False
        _emit(result, as_json=args.json)
        return EXIT_HASH_MISMATCH

    assert payload is not None
    payload_ticker = str(payload.get("ticker") or "").strip().upper()
    row_ticker = str(row.get("ticker") or "").strip().upper()
    if payload_ticker != ticker or row_ticker != ticker:
        result["match"] = False
        result["ticker_ok"] = False
        result["payload_ticker"] = payload.get("ticker")
        result["note"] += (
            " Payload ticker does not match the requested ticker. "
            f"Requested {ticker}; payload says {payload.get('ticker')!r}; "
            f"row says {row.get('ticker')!r}."
        )
        _emit(result, as_json=args.json)
        return EXIT_NO_MATCH
    result["ticker_ok"] = True

    missing_fields = [name for name in _LEGACY_FIELDS if name not in payload]
    if missing_fields:
        result["legacy"] = True
        result["note"] += (
            " Legacy payload is missing "
            + ", ".join(missing_fields)
            + "."
        )

    inputs_blob = row.get("inputs")
    if isinstance(inputs_blob, (bytes, bytearray)) and inputs_blob:
        inputs_ok, parsed_inputs = _inputs_match(bytes(inputs_blob), payload.get("inputs_digest"))
        result["inputs_stored"] = True
        result["inputs_digest_ok"] = inputs_ok
        result["inputs"] = parsed_inputs
        if not inputs_ok:
            result["match"] = False
            result["note"] += " Stored inputs do not recompute inputs_digest."
            _emit(result, as_json=args.json)
            return EXIT_HASH_MISMATCH
    else:
        result["inputs_stored"] = False
        result["inputs_digest_ok"] = None
        result["match"] = False
        result["note"] += " Scoring inputs were not stored; inputs_digest cannot be re-derived."
        _emit(result, as_json=args.json)
        return EXIT_INPUTS_MISSING

    if args.rpc_url.strip():
        chain = on_chain_verify(
            contract=contract,
            rpc_url=args.rpc_url.strip(),
            digest=recomputed,
            ticker=ticker,
            expected_attester=args.attester,
        )
        result["on_chain"] = chain
        if chain.get("error") == "cast_missing":
            result["match"] = False
            result["note"] += " cast is not on PATH."
            _emit(result, as_json=args.json)
            return EXIT_CAST_MISSING
        if chain.get("error"):
            result["match"] = False
            result["note"] += " RPC read failed."
            _emit(result, as_json=args.json)
            return EXIT_RPC_ERROR
        matched = (
            chain.get("ok") is True
            and chain.get("chain_id") == BASE_SEPOLIA_CHAIN_ID
            and chain.get("attester_ok") is True
        )
        result["match"] = matched
        if not matched:
            if chain.get("refused") == "chain_id":
                result["note"] += f" Refusing eth_chainId {chain.get('chain_id')}; want {BASE_SEPOLIA_CHAIN_ID}."
            elif not args.attester.strip():
                result["note"] += " Set --attester or RWA_ATTESTER_ADDRESS. The attester was not checked."
            elif chain.get("attester_ok") is not True:
                result["note"] += " On-chain attester does not match the expected address."
            else:
                result["note"] += " On-chain verify() is not true."
            _emit(result, as_json=args.json)
            return EXIT_NO_MATCH
    elif args.offline:
        result["match"] = None
        result["offline"] = True
        result["note"] += (
            " Offline: nothing was checked on-chain. "
            "Stored bytes and inputs_digest were checked locally only."
        )
    else:
        result["match"] = False
        result["note"] += (
            " Nothing was checked on-chain. "
            "Pass --rpc-url or set BASE_SEPOLIA_RPC_URL, "
            "or pass --offline to check stored bytes only."
        )
        _emit(result, as_json=args.json)
        return EXIT_CHAIN_UNCHECKED

    _emit(result, as_json=args.json)
    return EXIT_OK


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
        if result.get("offline"):
            print("on-chain   offline — nothing was checked on-chain")
        else:
            print("on-chain   not checked — pass --rpc-url or --offline")
    else:
        print(f"on-chain   {result['on_chain']}")
        print(f"match      {result['match']}")
    print(result["note"])


if __name__ == "__main__":
    raise SystemExit(main())
