"""Check a saved attest response against ScoreAttestation.

Does not re-score. Does not send transactions. The API does not keep the
payload. Save the JSON from ``POST /v1/attest/{ticker}`` and pass it as
``--payload-file``.

The digest is SHA-256 of the canonical bytes. That is the ``bytes32``
``attest`` stores. The contract does not keccak the payload. ``verify``
on the contract keccak-hashes the ticker string only, to compare it.
This command checks ``attested``, ``getAttestation``, ``verify``, and,
when the file has ``tx_hash``, the ``ScoreAttested`` log on that receipt.

Exit codes:

- ``0`` canonical bytes match their hash and the chain read matched.
  ``--offline`` is also ``0`` when the local checks pass; that mode
  prints that nothing was checked on-chain. Inputs are checked only
  when the file includes them.
- ``1`` ran without ``--payload-file``. The message is
  ``supply --payload-file (the JSON returned by POST /v1/attest)``.
  The same exit is used when the path does not exist or the file is not
  a bundle. It is not a stored-row error.
- ``3`` bytes do not match the hash, the payload is malformed, or inputs
  in the file do not recompute ``inputs_digest``
- ``4`` ticker mismatch, chain id is not 84532, ``attested`` / ``verify``
  is false, the attester mismatches, or the receipt event does not match
- ``5`` RPC / cast call failed
- ``6`` a chain read was requested but ``cast`` is not on ``PATH``
- ``7`` no ``--rpc-url`` and ``BASE_SEPOLIA_RPC_URL`` is unset, and
  ``--offline`` was not passed

Exit ``2`` (nothing stored / missing row) and exit ``8`` (inputs missing)
are retired. There is no stored row, and a file without inputs is still
checkable from the canonical bytes.

``--fixtures``, ``--api-url``, and ``--api-key`` are obsolete: they warn
on stderr and do not re-score.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from eth_abi import decode as abi_decode
from eth_utils import keccak

from .attest import (
    PINNED_ATTESTATION_CONTRACT,
    canonical_bytes,
    hash_canonical,
    recompute_inputs_digest,
)
from .settings import BASE_SEPOLIA_CHAIN_ID

EXIT_OK = 0
EXIT_DB = 1
# Spoken when the flag is omitted. Exit 1. Not a stored-row error.
PAYLOAD_FILE_SUPPLY = "supply --payload-file (the JSON returned by POST /v1/attest)"
# Retired. Nothing is stored, so a missing file is exit 1, not a missing row.
EXIT_NOT_STORED = 2
EXIT_HASH_MISMATCH = 3
EXIT_NO_MATCH = 4
EXIT_RPC_ERROR = 5
EXIT_CAST_MISSING = 6
EXIT_CHAIN_UNCHECKED = 7
# Retired. A payload file without inputs is still verified from canonical bytes.
EXIT_INPUTS_MISSING = 8

# Type signature is unchanged. The uint256 is the contract's trusted
# attestedAt (block.timestamp at attest), not the attester's claimedAt.
# None of the ScoreAttested fields are indexed. topic0 is the keccak of
# the canonical signature; the hash in the log data is the bytes32 the
# attester submitted (SHA-256 of the canonical payload), not a keccak of it.
_SCORE_ATTESTED_TOPIC = "0x" + keccak(
    text="ScoreAttested(string,bytes32,uint256,uint256,address)"
).hex()
# JSON `attested_at` is that int. cast 1.8.3 prints `1700000000 [1.7e9]`.
VERIFY_SIG = "verify(bytes32,string)(bool,uint256,address)"
ATTESTED_SIG = "attested(bytes32)(bool)"
# A struct return needs the extra parentheses. Without them cast treats the
# first word (the hash) as a string offset and the call fails.
GET_ATTESTATION_SIG = "getAttestation(bytes32)((bytes32,string,uint256,address,uint256))"

_LEGACY_FIELDS = ("as_of", "data_as_of", "scorer_version", "inputs_digest")

_OBSOLETE_FLAGS = (
    "WARNING: --fixtures, --api-url, and --api-key are Ignored. They do not re-score and are obsolete. "
    "verify only checks the JSON from POST /v1/attest/{ticker}. "
    "It does not re-score and it does not invent a payload."
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


def _parse_bool(text: str) -> bool | None:
    token = text.strip().split()[0].lower() if text.strip() else ""
    if token in {"true", "1"}:
        return True
    if token in {"false", "0"}:
        return False
    return None


def _parse_record(text: str) -> dict[str, Any] | None:
    """cast prints getAttestation as a tuple, or one field per line.

    Cast 1.8 adds a ``[1.7e9]`` annotation after each uint. Drop those.
    """
    fields: list[str] = []
    for token in text.replace("(", " ").replace(")", " ").replace(",", " ").split():
        if token.startswith("[") and token.endswith("]"):
            continue
        fields.append(token.strip().strip('"'))
    if len(fields) < 5:
        return None
    try:
        attested_at = int(fields[2])
        claimed_at = int(fields[4])
    except ValueError:
        return None
    return {
        "score_hash": fields[0],
        "ticker": fields[1],
        "attested_at": attested_at,
        "attester": fields[3],
        "claimed_at": claimed_at,
    }


def on_chain_verify(
    *,
    contract: str,
    rpc_url: str,
    digest: str,
    ticker: str,
    expected_attester: str = "",
    tx_hash: str = "",
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
        attested_raw = _cast_output(
            cast,
            [cast, "call", contract, ATTESTED_SIG, digest, "--rpc-url", rpc_url],
            rpc_url,
        )
        record_raw = _cast_output(
            cast,
            [cast, "call", contract, GET_ATTESTATION_SIG, digest, "--rpc-url", rpc_url],
            rpc_url,
        )
        out = _cast_output(
            cast,
            [cast, "call", contract, VERIFY_SIG, digest, ticker, "--rpc-url", rpc_url],
            rpc_url,
        )
    except _RpcError as exc:
        return {"ok": False, "chain_id": chain_id, "error": str(exc)}
    attested_flag = _parse_bool(attested_raw)
    record = _parse_record(record_raw)
    if attested_flag is None or record is None:
        return {
            "ok": False,
            "chain_id": chain_id,
            "error": "unparsed attestation",
            "raw": record_raw.strip(),
        }
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
    verify_ok = flag.lower() in {"true", "1"}
    record_ticker = str(record.get("ticker") or "")
    record_ok = (
        attested_flag is True
        and record_ticker == ticker
        and str(record.get("attester") or "").lower() == attester.lower()
    )
    event = None
    if tx_hash.strip():
        try:
            receipt_raw = _cast_output(
                cast,
                [cast, "receipt", tx_hash.strip(), "--json", "--rpc-url", rpc_url],
                rpc_url,
            )
        except _RpcError as exc:
            return {"ok": False, "chain_id": chain_id, "error": str(exc)}
        event = {"tx_hash": tx_hash.strip()}
        if _score_attested_in_receipt(receipt_raw, digest, ticker):
            event["match"] = True
        else:
            record_ok = False
            event["match"] = False
    return {
        "ok": verify_ok and record_ok,
        "attested": attested_flag,
        "chain_id": chain_id,
        "attested_at": attested_at,
        "attester": attester,
        "attester_ok": attester_ok,
        "expected_attester": expected or None,
        "record": record,
        "event": event,
        "raw": out.strip(),
    }


def _score_attested_in_receipt(receipt_raw: str, digest: str, ticker: str) -> bool:
    """True when a receipt log is ScoreAttested for this ticker and hash.

    ``cast receipt --json`` ABI-encodes the hash inside ``logs[].data``.
    A substring search misses it. Decode the event the contract actually emits.
    """
    try:
        receipt = json.loads(receipt_raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(receipt, dict):
        return False
    logs = receipt.get("logs")
    if not isinstance(logs, list) and isinstance(receipt.get("transactionReceipt"), dict):
        logs = receipt["transactionReceipt"].get("logs")
    if not isinstance(logs, list):
        return False
    want = digest.lower()
    if not want.startswith("0x"):
        want = "0x" + want
    for log in logs:
        if not isinstance(log, dict):
            continue
        topics = log.get("topics") or []
        if not topics or str(topics[0]).lower() != _SCORE_ATTESTED_TOPIC:
            continue
        decoded = _decode_score_attested(str(log.get("data") or ""))
        if decoded is None:
            continue
        log_ticker, score_hash = decoded[0], decoded[1]
        score_hex = score_hash.hex() if isinstance(score_hash, (bytes, bytearray)) else str(score_hash)
        if not score_hex.startswith("0x"):
            score_hex = "0x" + score_hex
        if score_hex.lower() == want and str(log_ticker) == ticker:
            return True
    return False


def _decode_score_attested(data: str) -> tuple[Any, ...] | None:
    raw = data[2:] if data.startswith("0x") else data
    if not raw:
        return None
    try:
        blob = bytes.fromhex(raw)
        return abi_decode(["string", "bytes32", "uint256", "uint256", "address"], blob)
    except (ValueError, TypeError):
        return None


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


def export_payload(
    *,
    ticker: str,
    score_hash: str,
    canonical: bytes,
    inputs: bytes | None,
) -> dict[str, Any]:
    """JSON bundle a caller can save and pass to ``verify --payload-file``."""
    return {
        "ticker": ticker,
        "score_hash": score_hash,
        "canonical_b64": base64.b64encode(bytes(canonical)).decode("ascii"),
        "inputs_b64": None if inputs is None else base64.b64encode(bytes(inputs)).decode("ascii"),
    }


def load_payload_file(path: Path) -> dict[str, Any]:
    """Read a ``canonical_payload`` bundle. Raises ``ValueError`` when it is not one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("payload file is not a JSON bundle") from exc
    if not isinstance(data, dict) or "canonical_b64" not in data:
        raise ValueError("payload file is not a JSON bundle")
    try:
        canonical = base64.b64decode(data["canonical_b64"], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("payload file canonical bytes are not base64") from exc
    inputs_field = data.get("inputs_b64", None)
    inputs: bytes | None
    if inputs_field is None:
        inputs = None
    else:
        try:
            inputs = base64.b64decode(inputs_field, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("payload file inputs are not base64") from exc
    claimed = str(data.get("score_hash") or "").strip()
    nested = data.get("payload")
    ticker = str(data.get("ticker") or "")
    if not ticker and isinstance(nested, dict):
        ticker = str(nested.get("ticker") or "")
    return {
        "score_hash": claimed or hash_canonical(canonical),
        "ticker": ticker,
        "canonical": canonical,
        "inputs": inputs,
        "stored_at": None,
        "tx_hash": data.get("tx_hash") or None,
        "attested_at": None,
        "parsed_payload": nested if isinstance(nested, dict) else None,
    }


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
        help="Score hash the payload file must carry (default: the hash in the file)",
    )
    parser.add_argument(
        "--payload-file",
        default="",
        help=(
            "JSON returned by POST /v1/attest/{ticker} "
            "(score, as_of, score_hash, canonical_b64, payload, tx_hash, status). "
            "Required. The API does not keep a copy."
        ),
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

    payload_path = args.payload_file.strip()
    row: dict[str, Any] | None = None
    if payload_path:
        path = Path(payload_path)
        if not path.is_file():
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
                "error": "payload_missing",
                "note": (
                    f"Payload file does not exist ({path}). "
                    "verify does not create one. "
                    "A missing file is not the same as nothing attested."
                    + ignored_note
                ),
            }
            _emit(result, as_json=args.json)
            return EXIT_DB
        try:
            row = load_payload_file(path)
        except ValueError:
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
                "error": "payload_error",
                "note": "Payload file is not a canonical_payload bundle." + ignored_note,
            }
            _emit(result, as_json=args.json)
            return EXIT_DB
        requested = args.score_hash.strip()
        if requested and row["score_hash"] != requested:
            result = {
                "ticker": ticker,
                "stored": True,
                "score_hash": row["score_hash"],
                "payload": None,
                "canonical": None,
                "contract": contract,
                "on_chain": None,
                "match": False,
                "hash_ok": False,
                "note": (
                    " --hash does not match the payload file. "
                    "verify does not look up a different hash."
                    + ignored_note
                ),
            }
            _emit(result, as_json=args.json)
            return EXIT_NO_MATCH
    else:
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
            "error": "payload_missing",
            "note": (
                PAYLOAD_FILE_SUPPLY
                + ". verify does not re-score and does not invent canonical bytes."
                + ignored_note
            ),
        }
        _emit(result, as_json=args.json)
        return EXIT_DB

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
            "Hash recomputed from the canonical JSON in --payload-file. "
            "No live re-score. The contract stores this SHA-256 digest, not a keccak of the JSON. "
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
            result["note"] += " Canonical bytes do not match the hash."
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
        result["note"] += " Inputs were not in the file; inputs_digest was not re-derived."

    parsed_payload = row.get("parsed_payload")
    if isinstance(parsed_payload, dict) and payload is not None and parsed_payload != payload:
        result["match"] = False
        result["note"] += " Parsed payload does not match canonical bytes."
        _emit(result, as_json=args.json)
        return EXIT_HASH_MISMATCH

    if args.rpc_url.strip():
        chain = on_chain_verify(
            contract=contract,
            rpc_url=args.rpc_url.strip(),
            digest=recomputed,
            ticker=ticker,
            expected_attester=args.attester,
            tx_hash=str(row.get("tx_hash") or ""),
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
    if not result.get("stored") or "score" not in result:
        print(result.get("note") or "")
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
