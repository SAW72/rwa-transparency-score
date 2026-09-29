"""In-process attester for the current Base Sepolia ScoreAttestation.

``POST /v1/attest/{ticker}`` scores live, hashes the canonical bytes from
that request, checks ``attested`` on chain, and broadcasts. The HTTP
handler returns immediately, or after ``RWA_ATTEST_WAIT_SECONDS``.

The only pending-transaction state is an in-flight map of ``tx_hash`` and
``nonce`` (plus the subject needed to poll ``attested``). A restart drops
it. A startup hold waits while the pending nonce is ahead of the latest
nonce, and every send checks ``attested`` first. That reduces the chance
of a duplicate. A restart in the middle of a broadcast can still cost one
duplicate transaction that reverts or no-ops. The background thread only
reconciles receipts. It does not keep scores.

Spencer sets the attester key on Render himself. This process reads
``RWA_ATTESTER_PRIVATE_KEY`` from the environment and never logs it.

If the key, contract, or RPC is unset, the worker stays disabled and the
API says so. It does not crash and it does not send.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import unquote, urlsplit

from rwa_score.api.attest import hash_canonical
from rwa_score.api.settings import BASE_SEPOLIA_CHAIN_ID

logger = logging.getLogger(__name__)

# bytes4(keccak256("AlreadyAttested()"))
ALREADY_ATTESTED_SELECTOR = "0x35d90805"
DISABLED_REASON = (
    "Attester worker disabled: set RWA_ATTESTER_PRIVATE_KEY, "
    "RWA_ATTESTATION_CONTRACT, and BASE_SEPOLIA_RPC_URL. "
    "No transaction was sent."
)
DEFAULT_GAS_LIMIT = 300_000
# Hard ceiling. Env may set a lower cap. Measured attest max is about 210495.
HARD_GAS_CAP = 500_000
DEFAULT_MAX_FEE_GWEI = 20
# How long a broadcast may stay unresolved before the drop check is allowed.
DEFAULT_BROADCAST_DEADLINE_SECONDS = 30 * 60
# POST returns as soon as the tx is broadcast unless this is raised.
DEFAULT_WAIT_SECONDS = 0.0
BROADCAST_POLL_SECONDS = 15.0
# On startup, wait this long for a leftover mempool tx to mine before sending.
STARTUP_HOLD_SECONDS = 15.0
STARTUP_HOLD_POLL_SECONDS = 0.25
DEFAULT_VALUE_CAP_WEI = 0
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_SECONDS = 2.0
_REDACTED = "[redacted]"
FAILED_REASON = "attest_failed"
FAILED_MESSAGE = "The attest job failed."
RPC_TIMEOUT_SECONDS = 20
RECEIPT_TIMEOUT_SECONDS = 60
# Fixture scores use as_of 0. Anything past year 2100 is not a unix second.
AS_OF_MAX = 4_102_444_800
PINNED_ATTESTATION_CONTRACT = "0x2F073a3628D498d92956e7eFE2b26633eDa75b00"
_TICKER_RE = re.compile(r"[A-Za-z0-9]{1,16}")

_ATTEST_ABI = [
    {
        "name": "attest",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "scoreHash", "type": "bytes32"},
            {"name": "ticker", "type": "string"},
            {"name": "timestamp", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "verify",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "scoreHash", "type": "bytes32"},
            {"name": "ticker", "type": "string"},
        ],
        "outputs": [
            {"name": "ok", "type": "bool"},
            {"name": "timestamp", "type": "uint256"},
            {"name": "attester", "type": "address"},
        ],
    },
    {
        "name": "attestationFee",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "owner",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "isAttester",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "who", "type": "address"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "attested",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "getAttestation",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "scoreHash", "type": "bytes32"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "scoreHash", "type": "bytes32"},
                    {"name": "ticker", "type": "string"},
                    {"name": "attestedAt", "type": "uint256"},
                    {"name": "attester", "type": "address"},
                    {"name": "claimedAt", "type": "uint256"},
                ],
            }
        ],
    },
    {
        "name": "hashesForTicker",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "ticker", "type": "string"}],
        "outputs": [{"name": "", "type": "bytes32[]"}],
    },
    {
        "name": "ScoreAttested",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "ticker", "type": "string", "indexed": False},
            {"name": "scoreHash", "type": "bytes32", "indexed": False},
            {"name": "attestedAt", "type": "uint256", "indexed": False},
            {"name": "claimedAt", "type": "uint256", "indexed": False},
            {"name": "attester", "type": "address", "indexed": False},
        ],
    },
]


def _rpc_needles(rpc_url: str) -> list[str]:
    """Full RPC URL plus path, query, userinfo, and any embedded key."""
    raw = (rpc_url or "").strip()
    if not raw:
        return []
    found = [raw]
    parts = urlsplit(raw)
    if parts.username:
        found.append(unquote(parts.username))
    if parts.password:
        found.append(unquote(parts.password))
    if "@" in parts.netloc:
        found.append(parts.netloc.split("@", 1)[0])
    path = parts.path or ""
    if path and path != "/":
        found.append(path)
        for seg in path.split("/"):
            if seg:
                found.append(unquote(seg))
    if parts.query:
        found.append(parts.query)
        for item in parts.query.split("&"):
            if "=" in item:
                _key, val = item.split("=", 1)
                if val:
                    found.append(unquote(val))
            elif item:
                found.append(unquote(item))
    return found


def _needles_for(secret: str, rpc_url: str) -> list[str]:
    found: list[str] = []
    if secret:
        found.append(secret)
        bare = secret[2:] if secret.lower().startswith("0x") else secret
        if bare:
            found.append(bare)
    found.extend(_rpc_needles(rpc_url))
    out: list[str] = []
    for item in found:
        if item and len(item) >= 8 and item not in out:
            out.append(item)
    return out


class _RedactionVault:
    def __init__(self) -> None:
        self._needles: list[str] = []
        self._lock = threading.Lock()

    def add(self, secret: str = "", rpc_url: str = "") -> None:
        fresh = _needles_for(secret, rpc_url)
        if not fresh:
            return
        with self._lock:
            for needle in fresh:
                if needle not in self._needles:
                    self._needles.append(needle)

    def needles(self) -> list[str]:
        with self._lock:
            return list(self._needles)


_VAULT = _RedactionVault()
_HANDLE_WRAPPED = False


def redact(text: str, secret: str = "", rpc_url: str = "") -> str:
    """Strip a private key and RPC URL from text. Match is case-insensitive."""
    if not text:
        return text
    needles = _needles_for(secret, rpc_url)
    if not needles:
        return text
    cleaned = text
    for needle in sorted(needles, key=len, reverse=True):
        cleaned = re.sub(re.escape(needle), _REDACTED, cleaned, flags=re.IGNORECASE)
    return cleaned


def _redact_known(text: str) -> str:
    needles = _VAULT.needles()
    if not text or not needles:
        return text
    cleaned = text
    for needle in sorted(needles, key=len, reverse=True):
        cleaned = re.sub(re.escape(needle), _REDACTED, cleaned, flags=re.IGNORECASE)
    return cleaned


class _RedactFilter(logging.Filter):
    """Redact secrets on every record. Installed on the root and app loggers."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_known(str(record.msg))
        if isinstance(record.args, dict):
            record.args = {
                key: _redact_known(val) if isinstance(val, str) else val
                for key, val in record.args.items()
            }
        elif record.args:
            record.args = tuple(
                _redact_known(item) if isinstance(item, str) else item for item in record.args
            )
        if record.exc_info and record.exc_info[0] is not None:
            import traceback

            text = "".join(traceback.format_exception(*record.exc_info))
            redacted = _redact_known(text)
            if redacted != text:
                record.exc_text = redacted
        return True


def _install_redact_filter() -> None:
    """Attach one filter to the root logger and the app logger tree.

    Logger filters do not run for records that propagate from a child.
    ``Logger.handle`` is wrapped once so a record from any logger is redacted
    before a handler (including pytest's caplog) formats it.
    """
    global _HANDLE_WRAPPED
    filt = _RedactFilter()
    for name in ("", "rwa_score", "rwa_score.api", "rwa_score.api.auto_attest"):
        target = logging.getLogger(name)
        if not any(isinstance(item, _RedactFilter) for item in target.filters):
            target.addFilter(filt)
    if _HANDLE_WRAPPED or getattr(logging.Logger.handle, "_rwa_redact", False):
        _HANDLE_WRAPPED = True
        return
    original = logging.Logger.handle

    def handle(self: logging.Logger, record: logging.LogRecord) -> None:
        filt.filter(record)
        return original(self, record)

    handle._rwa_redact = True  # type: ignore[attr-defined]
    logging.Logger.handle = handle  # type: ignore[method-assign]
    _HANDLE_WRAPPED = True


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return 0


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    return float(raw)


class AttesterSettings:
    """Env-only attester config. The key lives in a closure, not on ``__dict__``."""

    __slots__ = (
        "_get_key",
        "contract",
        "rpc_url",
        "value_cap_wei",
        "gas_limit",
        "max_attempts",
        "backoff_seconds",
        "chain_id",
        "enforce_contract_pin",
        "max_fee_gwei",
        "broadcast_deadline_seconds",
        "wait_seconds",
    )

    def __init__(
        self,
        *,
        private_key: str = "",
        contract: str = "",
        rpc_url: str = "",
        value_cap_wei: int = DEFAULT_VALUE_CAP_WEI,
        gas_limit: int = DEFAULT_GAS_LIMIT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        chain_id: int = BASE_SEPOLIA_CHAIN_ID,
        enforce_contract_pin: bool = True,
        max_fee_gwei: float = DEFAULT_MAX_FEE_GWEI,
        broadcast_deadline_seconds: int = DEFAULT_BROADCAST_DEADLINE_SECONDS,
        wait_seconds: float = DEFAULT_WAIT_SECONDS,
    ) -> None:
        key = (private_key or "").strip()
        self._get_key = (lambda captured: (lambda: captured))(key)
        self.contract = (contract or "").strip()
        self.rpc_url = (rpc_url or "").strip()
        self.value_cap_wei = int(value_cap_wei)
        self.gas_limit = int(gas_limit)
        self.max_attempts = int(max_attempts)
        self.backoff_seconds = float(backoff_seconds)
        self.chain_id = int(chain_id)
        self.enforce_contract_pin = bool(enforce_contract_pin)
        fee = float(max_fee_gwei)
        if fee < 0:
            fee = float(DEFAULT_MAX_FEE_GWEI)
        self.max_fee_gwei = fee
        deadline = int(broadcast_deadline_seconds)
        if deadline < 0:
            deadline = DEFAULT_BROADCAST_DEADLINE_SECONDS
        self.broadcast_deadline_seconds = deadline
        wait = float(wait_seconds)
        if wait < 0:
            wait = 0.0
        self.wait_seconds = wait
        if key or self.rpc_url:
            _VAULT.add(key, self.rpc_url)
            _install_redact_filter()

    @property
    def private_key(self) -> str:
        return self._get_key()

    @property
    def enabled(self) -> bool:
        return bool(self.private_key and self.contract and self.rpc_url)

    @property
    def disabled_reason(self) -> str:
        if self.enabled:
            return ""
        return DISABLED_REASON

    def __repr__(self) -> str:
        return (
            "AttesterSettings("
            f"enabled={self.enabled}, "
            f"contract={self.contract!r}, "
            f"rpc_set={bool(self.rpc_url)}, "
            f"value_cap_wei={self.value_cap_wei}, "
            f"gas_limit={self.gas_limit}, "
            "private_key='[redacted]')"
        )

    def __str__(self) -> str:
        return repr(self)

    @classmethod
    def from_env(cls) -> AttesterSettings:
        return cls(
            private_key=os.getenv("RWA_ATTESTER_PRIVATE_KEY", ""),
            contract=os.getenv("RWA_ATTESTATION_CONTRACT", ""),
            rpc_url=os.getenv("BASE_SEPOLIA_RPC_URL", ""),
            value_cap_wei=_env_int("RWA_ATTEST_VALUE_CAP_WEI", DEFAULT_VALUE_CAP_WEI),
            gas_limit=_env_int("RWA_ATTEST_GAS_LIMIT", DEFAULT_GAS_LIMIT),
            max_attempts=_env_int("RWA_ATTEST_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS),
            backoff_seconds=_env_float("RWA_ATTEST_BACKOFF_SECONDS", DEFAULT_BACKOFF_SECONDS),
            chain_id=_env_int("RWA_ATTESTATION_CHAIN_ID", BASE_SEPOLIA_CHAIN_ID),
            max_fee_gwei=_env_float("RWA_ATTEST_MAX_FEE_GWEI", DEFAULT_MAX_FEE_GWEI),
            broadcast_deadline_seconds=_env_int(
                "RWA_ATTEST_BROADCAST_DEADLINE_SECONDS",
                DEFAULT_BROADCAST_DEADLINE_SECONDS,
            ),
            wait_seconds=_env_float("RWA_ATTEST_WAIT_SECONDS", DEFAULT_WAIT_SECONDS),
        )


class TerminalAttestError(Exception):
    """Do not retry. Wrong chain and a fee above the cap land here."""


class AlreadyAttestedError(Exception):
    """The contract reverted ``AlreadyAttested``. Treat as success."""


class Chain(Protocol):
    def chain_id(self) -> int: ...

    def fee_wei(self) -> int: ...

    def verify(self, score_hash: str, ticker: str) -> tuple[bool, int, str]: ...

    def attest(
        self,
        score_hash: str,
        ticker: str,
        claimed_at: int,
        *,
        value_wei: int,
        gas_limit: int,
    ) -> str: ...


@dataclass(frozen=True)
class SendOutcome:
    tx_hash: str | None
    attested_at: int | None
    already: bool


@dataclass(frozen=True)
class SubmitResult:
    """Outcome of one live attest. ``error`` is redacted and must not be returned on HTTP."""

    status: str
    tx_hash: str | None = None
    attested_at: int | None = None
    reason: str | None = None
    message: str | None = None
    error: str | None = None


def _hash_bytes(score_hash: str) -> bytes:
    raw = score_hash.strip().lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) != 64:
        raise TerminalAttestError("score hash must be 32 bytes")
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise TerminalAttestError("score hash must be 32 bytes") from None


def _validate_subject(score_hash: str, ticker: str, claimed_at: int) -> None:
    """Ticker, 32-byte hash, and a unix as_of. Raises before any send."""
    _hash_bytes(score_hash)
    symbol = str(ticker or "")
    if _TICKER_RE.fullmatch(symbol) is None:
        raise TerminalAttestError("ticker must be 1-16 letters or digits")
    when = int(claimed_at)
    if when < 0 or when > AS_OF_MAX:
        raise TerminalAttestError("as_of is not a unix second")


def _assert_dedicated_attester(*, signer: str, owner: str, is_attester: bool) -> None:
    """The signer is an allowlisted attester, not the contract owner."""
    if signer.lower() == owner.lower():
        raise TerminalAttestError("refusing owner key; use a dedicated attester")
    if not is_attester:
        raise TerminalAttestError("signer is not an attester on this contract")


def _require_request_bytes(
    canonical: bytes,
    score_hash: str,
    ticker: str,
    claimed_at: int,
) -> None:
    """Only the hash of bytes from this request may be sent. No caller-supplied hash."""
    raw = bytes(canonical)
    digest = hash_canonical(raw)
    if digest != score_hash:
        raise TerminalAttestError("refusing hash that was not computed from this request")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TerminalAttestError("canonical payload is not JSON") from exc
    if not isinstance(payload, dict):
        raise TerminalAttestError("canonical payload is not an object")
    if str(payload.get("ticker")) != str(ticker):
        raise TerminalAttestError("ticker does not match canonical payload")
    if int(payload.get("as_of")) != int(claimed_at):
        raise TerminalAttestError("as_of does not match canonical payload")
    _validate_subject(score_hash, str(ticker), int(claimed_at))


def _tx_hex(tx_hash: Any) -> str:
    text = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    return text if text.startswith("0x") else "0x" + text


def _is_already(exc: BaseException) -> bool:
    blob = f"{exc} {getattr(exc, 'data', '')} {getattr(exc, 'message', '')}".lower()
    return ALREADY_ATTESTED_SELECTOR in blob or "alreadyattested" in blob


def _nonce_rejected(exc: BaseException) -> bool:
    """True when the node refused the nonce and the cache must be dropped."""
    blob = str(exc).lower()
    return "nonce too low" in blob or "replacement underpriced" in blob


def _unambiguous_rejection(exc: BaseException) -> bool:
    """True when the node refused the raw transaction and did not accept it.

    A timeout or a dropped connection stays ambiguous: the hash recorded
    before ``send_raw_transaction`` might still be in the mempool.
    """
    blob = str(exc).lower()
    needles = (
        "insufficient funds",
        "insufficient balance",
        "nonce too low",
        "replacement transaction underpriced",
        "replacement underpriced",
        "intrinsic gas too low",
        "exceeds block gas limit",
        "gas required exceeds allowance",
        "max fee per gas less than block base fee",
    )
    return any(needle in blob for needle in needles)


def _status_of(receipt: Any) -> int | None:
    """Receipt status, or None when this object is not a mined receipt."""
    if receipt is None or isinstance(receipt, bool):
        return None
    raw: Any
    if isinstance(receipt, dict):
        raw = receipt.get("status")
    else:
        raw = getattr(receipt, "status", None)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return int(raw)


def _raw_receipt(chain: Any, tx_hash: str | None) -> Any:
    if not tx_hash:
        return None
    getter = getattr(chain, "get_receipt", None)
    if not callable(getter):
        return None
    try:
        return getter(tx_hash)
    except Exception:
        return None


def _receipt_succeeded(chain: Any, tx_hash: str | None) -> bool:
    """True when a receipt for ``tx_hash`` is already mined with status 1."""
    return _status_of(_raw_receipt(chain, tx_hash)) == 1


def choose_nonce(*, stored_nonce: int | None, unresolved: bool, suggested: int) -> int:
    """Keep the in-flight nonce. A new nonce is only legal once that one is resolved."""
    if unresolved and stored_nonce is not None:
        return int(stored_nonce)
    return int(suggested)


def clamp_eip1559_fees(
    *,
    max_fee_gwei: float,
    base_fee_wei: int = 0,
    bump: int = 0,
) -> tuple[int, int]:
    """``(maxFeePerGas, maxPriorityFeePerGas)`` at or under ``max_fee_gwei``.

    ``bump`` raises the priority about 12.5% per step and still cannot pass
    the requested cap. The broadcaster calls this with ``bump=0``.
    """
    requested = float(max_fee_gwei)
    if requested < 0:
        requested = float(DEFAULT_MAX_FEE_GWEI)
    requested_wei = int(requested * 1_000_000_000)
    if requested_wei < 1:
        requested_wei = 1
    priority = min(1_000_000_000, requested_wei)
    for _ in range(max(0, int(bump))):
        bumped = priority * 1125 // 1000 + 1
        if bumped > requested_wei:
            priority = requested_wei
            break
        priority = bumped
    base = max(0, int(base_fee_wei))
    max_fee = base * 2 + priority
    if max_fee > requested_wei:
        max_fee = requested_wei
    if priority > max_fee:
        priority = max_fee
    if max_fee < 1:
        max_fee = 1
        priority = 1
    return int(max_fee), int(priority)


class Web3Chain:
    """One signer, one nonce stream.

    Once a hash is broadcast, this sender does not broadcast another
    transaction for that job. The reconciler polls the saved hash.
    The startup hold plus the ``attested`` pre-check reduce the risk of a
    duplicate after a restart. A restart mid-broadcast can still cost one
    duplicate transaction that reverts or no-ops.
    """

    def __init__(self, settings: AttesterSettings) -> None:
        if not settings.enabled:
            raise TerminalAttestError(DISABLED_REASON)
        from eth_account import Account
        from web3 import Web3

        self._settings = settings
        self._w3 = Web3(
            Web3.HTTPProvider(settings.rpc_url, request_kwargs={"timeout": RPC_TIMEOUT_SECONDS})
        )
        try:
            self._account = Account.from_key(settings.private_key)
        except Exception:
            raise TerminalAttestError("invalid attester key") from None
        self._contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(settings.contract),
            abi=_ATTEST_ABI,
        )
        self._lock = threading.Lock()
        self._nonce: int | None = None
        self._startup_hold()

    def _startup_hold(self) -> None:
        """No sends while a restart still has a transaction in the mempool.

        Poll until the pending nonce count equals the latest nonce count,
        or until the deadline, then proceed.
        """
        deadline = time.monotonic() + STARTUP_HOLD_SECONDS
        while True:
            try:
                pending = int(self.transaction_count("pending"))
                latest = int(self.transaction_count("latest"))
            except Exception:
                return
            if pending <= latest:
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(STARTUP_HOLD_POLL_SECONDS)

    @property
    def address(self) -> str:
        return self._account.address

    def chain_id(self) -> int:
        return int(self._w3.eth.chain_id)

    def fee_wei(self) -> int:
        return int(self._contract.functions.attestationFee().call())

    def balance_wei(self) -> int:
        return int(self._w3.eth.get_balance(self._account.address))

    def transaction_count(self, block: str = "latest") -> int:
        return int(self._w3.eth.get_transaction_count(self._account.address, block))

    def get_tx(self, tx_hash: str) -> Any | None:
        try:
            return self._w3.eth.get_transaction(tx_hash)
        except Exception:
            return None

    def get_receipt(self, tx_hash: str) -> Any | None:
        try:
            return self._w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            return None

    def verify(self, score_hash: str, ticker: str) -> tuple[bool, int, str]:
        ok, ts, who = self._contract.functions.verify(_hash_bytes(score_hash), ticker).call()
        return bool(ok), int(ts), str(who)

    def attested(self, score_hash: str) -> bool:
        return bool(self._contract.functions.attested(_hash_bytes(score_hash)).call())

    def get_attestation(self, score_hash: str) -> dict[str, Any]:
        rec = self._contract.functions.getAttestation(_hash_bytes(score_hash)).call()
        score = rec[0]
        score_hex = score.hex() if hasattr(score, "hex") else str(score)
        if not str(score_hex).startswith("0x"):
            score_hex = "0x" + str(score_hex)
        return {
            "score_hash": score_hex,
            "ticker": str(rec[1]),
            "attested_at": int(rec[2]),
            "attester": str(rec[3]),
            "claimed_at": int(rec[4]),
        }

    def hashes_for_ticker(self, ticker: str) -> list[str]:
        rows = self._contract.functions.hashesForTicker(ticker).call()
        out: list[str] = []
        for item in rows:
            text = item.hex() if hasattr(item, "hex") else str(item)
            if not text.startswith("0x"):
                text = "0x" + text
            out.append(text)
        return out

    def receipt_event(self, tx_hash: str) -> dict[str, Any] | None:
        """Decode ``ScoreAttested`` from a receipt. None when the receipt is absent."""
        receipt = self.get_receipt(tx_hash)
        if receipt is None:
            return None
        try:
            logs = self._contract.events.ScoreAttested().process_receipt(receipt)
        except Exception:
            return None
        if not logs:
            return None
        args = logs[0]["args"]
        score = args["scoreHash"]
        score_hex = score.hex() if hasattr(score, "hex") else str(score)
        if not str(score_hex).startswith("0x"):
            score_hex = "0x" + str(score_hex)
        return {
            "ticker": str(args["ticker"]),
            "score_hash": score_hex,
            "attested_at": int(args["attestedAt"]),
            "claimed_at": int(args["claimedAt"]),
            "attester": str(args["attester"]),
            "tx_hash": tx_hash,
        }

    def attest(
        self,
        score_hash: str,
        ticker: str,
        claimed_at: int,
        *,
        value_wei: int,
        gas_limit: int,
        pending_nonce: int | None = None,
        pending_tx: str | None = None,
        known_hashes: list[str] | None = None,
        on_submitted: Callable[[str, int], None] | None = None,
        receipt_timeout: float | None = None,
    ) -> str:
        with self._lock:
            return self._send(
                score_hash,
                ticker,
                claimed_at,
                value_wei=value_wei,
                gas_limit=gas_limit,
                pending_nonce=pending_nonce,
                pending_tx=pending_tx,
                known_hashes=known_hashes,
                on_submitted=on_submitted,
                receipt_timeout=receipt_timeout,
            )

    def landed_hash(
        self,
        score_hash: str,
        ticker: str,
        known: list[str] | None = None,
    ) -> str | None:
        hashes = [item for item in (known or []) if item]
        return self._landed(hashes, score_hash, ticker)

    def _base_fee_wei(self) -> int:
        try:
            block = self._w3.eth.get_block("latest")
        except Exception:
            return 0
        raw = block.get("baseFeePerGas") if hasattr(block, "get") else None
        if raw is None:
            raw = getattr(block, "baseFeePerGas", 0)
        return int(raw or 0)

    def _send(
        self,
        score_hash: str,
        ticker: str,
        claimed_at: int,
        *,
        value_wei: int,
        gas_limit: int,
        pending_nonce: int | None = None,
        pending_tx: str | None = None,
        known_hashes: list[str] | None = None,
        on_submitted: Callable[[str, int], None] | None = None,
        receipt_timeout: float | None = None,
    ) -> str:
        chain_id = int(self._w3.eth.chain_id)
        if chain_id != BASE_SEPOLIA_CHAIN_ID:
            raise TerminalAttestError(
                f"refusing eth_chainId {chain_id}; only {BASE_SEPOLIA_CHAIN_ID}"
            )
        owner = str(self._contract.functions.owner().call())
        allowed = bool(self._contract.functions.isAttester(self._account.address).call())
        _assert_dedicated_attester(signer=self._account.address, owner=owner, is_attester=allowed)
        known: list[str] = []
        for item in list(known_hashes or []):
            if item and item not in known:
                known.append(item)
        if pending_tx and pending_tx not in known:
            known.append(pending_tx)
        if pending_tx:
            # Already broadcast. Never sign another transaction for this job.
            landed = self._landed(known, score_hash, ticker)
            if landed:
                if pending_nonce is not None:
                    self._nonce = int(pending_nonce) + 1
                return landed
            raise RuntimeError("broadcast still pending; not sending another transaction")
        if known:
            landed = self._landed(known, score_hash, ticker)
            if landed:
                if pending_nonce is not None:
                    self._nonce = int(pending_nonce) + 1
                return landed
        # The cache stays on the nonce we last used. The pending count is the
        # next legal nonce once that transaction is in the mempool, so a later
        # job must not reuse it.
        pending_count = int(self._w3.eth.get_transaction_count(self._account.address, "pending"))
        if self._nonce is None:
            nonce = pending_count
        else:
            nonce = max(int(self._nonce), pending_count)
        self._nonce = int(nonce)
        return self._broadcast(
            score_hash,
            ticker,
            claimed_at,
            value_wei=value_wei,
            gas_limit=gas_limit,
            chain_id=chain_id,
            nonce=nonce,
            known=known,
            on_submitted=on_submitted,
            bump=0,
            receipt_timeout=receipt_timeout,
        )

    def _broadcast(
        self,
        score_hash: str,
        ticker: str,
        claimed_at: int,
        *,
        value_wei: int,
        gas_limit: int,
        chain_id: int,
        nonce: int,
        known: list[str],
        on_submitted: Callable[[str, int], None] | None,
        bump: int,
        receipt_timeout: float | None = None,
    ) -> str:
        max_fee, priority = clamp_eip1559_fees(
            max_fee_gwei=self._settings.max_fee_gwei,
            base_fee_wei=self._base_fee_wei(),
            bump=bump,
        )
        fn = self._contract.functions.attest(_hash_bytes(score_hash), ticker, int(claimed_at))
        tx = fn.build_transaction(
            {
                "from": self._account.address,
                "value": int(value_wei),
                "gas": int(gas_limit),
                "nonce": int(nonce),
                "chainId": chain_id,
                "maxFeePerGas": int(max_fee),
                "maxPriorityFeePerGas": int(priority),
            }
        )
        signed = self._account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None)
        if raw is None:
            raw = signed.rawTransaction
        # The hash is known before the node accepts the bytes. Record it
        # first so a crash between sign and send is still in flight.
        hex_hash = _tx_hex(signed.hash)
        if hex_hash not in known:
            known.append(hex_hash)
        if on_submitted is not None:
            on_submitted(hex_hash, int(nonce))
        # Stay on this nonce until a receipt says it was consumed.
        self._nonce = int(nonce)
        try:
            sent = self._w3.eth.send_raw_transaction(raw)
        except Exception as exc:
            if _nonce_rejected(exc):
                self._nonce = None
            raise
        sent_hash = _tx_hex(sent)
        if sent_hash not in known:
            known.append(sent_hash)
        timeout = RECEIPT_TIMEOUT_SECONDS if receipt_timeout is None else float(receipt_timeout)
        if timeout <= 0:
            # Return immediately, but if the node already mined this hash
            # (anvil automine), report that receipt instead of a stale pending.
            landed = self._landed(known, score_hash, ticker)
            if landed:
                self._nonce = int(nonce) + 1
                return landed
            return hex_hash
        try:
            receipt = self._w3.eth.wait_for_transaction_receipt(sent, timeout=timeout)
        except Exception as exc:
            if _is_already(exc):
                raise AlreadyAttestedError("AlreadyAttested") from None
            landed = self._landed(known, score_hash, ticker)
            if landed:
                self._nonce = int(nonce) + 1
                return landed
            raise RuntimeError(
                redact(str(exc), self._settings.private_key, self._settings.rpc_url)
            ) from None
        if int(receipt["status"]) != 1:
            landed = self._landed(known, score_hash, ticker)
            if landed:
                self._nonce = int(nonce) + 1
                return landed
            self._nonce = int(nonce) + 1
            reason = _revert_blob(self._w3, tx)
            if _is_already(RuntimeError(reason)):
                raise AlreadyAttestedError("AlreadyAttested") from None
            raise RuntimeError(
                redact(
                    f"attest receipt status 0: {reason}",
                    self._settings.private_key,
                    self._settings.rpc_url,
                )
            ) from None
        self._nonce = int(nonce) + 1
        return _tx_hex(receipt["transactionHash"])

    def _landed(self, known: list[str], score_hash: str, ticker: str) -> str | None:
        """Return the known hash whose receipt status is 1.

        ``score_hash`` and ``ticker`` identify the subject the caller already
        checked with         ``verify``. This scan does not invent a hash from that
        check. Returning None is what the anvil timeout test treats as failure.
        """
        _ = (score_hash, ticker)
        for candidate in known:
            if not candidate:
                continue
            try:
                receipt = self._w3.eth.get_transaction_receipt(candidate)
            except Exception:
                receipt = None
            if receipt is None:
                continue
            try:
                status = int(receipt["status"])
            except (KeyError, TypeError, ValueError):
                status = int(getattr(receipt, "status", 0) or 0)
            if status != 1:
                continue
            try:
                raw = receipt["transactionHash"]
            except (KeyError, TypeError):
                raw = getattr(receipt, "transactionHash", candidate)
            return _tx_hex(raw)
        return None


def _revert_blob(w3: Any, tx: dict[str, Any]) -> str:
    try:
        w3.eth.call(tx)
    except Exception as exc:  # noqa: BLE001 — surface the revert selector only
        return redact(str(exc), "")
    return ""


def _local_refusal(settings: AttesterSettings, score_hash: str, ticker: str, claimed_at: int) -> str:
    """Config and subject checks that do not touch the network. Empty when they pass."""
    if int(settings.chain_id) != BASE_SEPOLIA_CHAIN_ID:
        return f"refusing configured chain id {settings.chain_id}; only {BASE_SEPOLIA_CHAIN_ID}"
    if settings.enforce_contract_pin and settings.contract.lower() != PINNED_ATTESTATION_CONTRACT.lower():
        return f"refusing contract; pinned to {PINNED_ATTESTATION_CONTRACT}"
    if settings.gas_limit <= 0 or settings.gas_limit > HARD_GAS_CAP:
        return f"gas cap must be 1..{HARD_GAS_CAP}"
    try:
        _validate_subject(score_hash, ticker, claimed_at)
    except TerminalAttestError as exc:
        return str(exc)
    return ""


def send_one(
    job: dict[str, Any],
    chain: Chain,
    settings: AttesterSettings,
    *,
    on_submitted: Callable[[str, int], None] | None = None,
    receipt_timeout: float | None = None,
    skip_initial_verify: bool = False,
) -> SendOutcome:
    """Pre-check ``verify``, then send. ``AlreadyAttested`` is success.

    ``skip_initial_verify`` is set when the caller already read ``eth_chainId``
    and ``verify`` for this attempt. The post-send ``verify`` still runs.
    """
    if int(settings.chain_id) != BASE_SEPOLIA_CHAIN_ID:
        raise TerminalAttestError(
            f"refusing configured chain id {settings.chain_id}; only {BASE_SEPOLIA_CHAIN_ID}"
        )
    if settings.enforce_contract_pin and settings.contract.lower() != PINNED_ATTESTATION_CONTRACT.lower():
        raise TerminalAttestError(
            f"refusing contract; pinned to {PINNED_ATTESTATION_CONTRACT}"
        )
    if settings.gas_limit <= 0 or settings.gas_limit > HARD_GAS_CAP:
        raise TerminalAttestError(f"gas cap must be 1..{HARD_GAS_CAP}")
    _validate_subject(str(job["score_hash"]), str(job["ticker"]), int(job["claimed_at"]))
    if not skip_initial_verify:
        chain_id = int(chain.chain_id())
        if chain_id != BASE_SEPOLIA_CHAIN_ID:
            raise TerminalAttestError(
                f"refusing eth_chainId {chain_id}; only {BASE_SEPOLIA_CHAIN_ID}"
            )
        ok, attested_at, _who = chain.verify(job["score_hash"], job["ticker"])
        if ok:
            return SendOutcome(
                tx_hash=_receipt_hash(chain, job),
                attested_at=int(attested_at),
                already=True,
            )
    fee = int(chain.fee_wei())
    if fee > settings.value_cap_wei:
        raise TerminalAttestError(
            f"attestationFee {fee} exceeds RWA_ATTEST_VALUE_CAP_WEI {settings.value_cap_wei}"
        )
    try:
        tx_hash = chain.attest(
            job["score_hash"],
            job["ticker"],
            int(job["claimed_at"]),
            value_wei=fee,
            gas_limit=settings.gas_limit,
            pending_nonce=job.get("nonce"),
            pending_tx=job.get("tx_hash"),
            known_hashes=list(job.get("known_tx_hashes") or []),
            on_submitted=on_submitted,
            receipt_timeout=receipt_timeout,
        )
    except AlreadyAttestedError:
        ok2, ts2, _who2 = chain.verify(job["score_hash"], job["ticker"])
        return SendOutcome(
            tx_hash=_receipt_hash(chain, job),
            attested_at=int(ts2) if ok2 else None,
            already=True,
        )
    except TerminalAttestError:
        raise
    except Exception:
        ok_late, ts_late, _who_late = chain.verify(job["score_hash"], job["ticker"])
        if ok_late:
            return SendOutcome(
                tx_hash=_receipt_hash(chain, job),
                attested_at=int(ts_late) if isinstance(ts_late, int) else None,
                already=True,
            )
        raise
    ok3, ts3, _who3 = chain.verify(job["score_hash"], job["ticker"])
    if ok3 is True:
        attested_at = int(ts3) if isinstance(ts3, int) and not isinstance(ts3, bool) else 0
        return SendOutcome(tx_hash=tx_hash, attested_at=attested_at, already=False)
    if _receipt_succeeded(chain, tx_hash):
        return SendOutcome(tx_hash=tx_hash, attested_at=0, already=False)
    return SendOutcome(tx_hash=tx_hash, attested_at=None, already=False)


def _receipt_hash(chain: Chain, job: dict[str, Any]) -> str | None:
    """Hash ``_landed`` found. None when that check is disabled or no receipt mined."""
    finder = getattr(chain, "landed_hash", None)
    if not callable(finder):
        return None
    try:
        found = finder(
            job["score_hash"],
            job["ticker"],
            list(job.get("known_tx_hashes") or []),
        )
    except Exception:
        return None
    if not isinstance(found, str) or not found.startswith("0x"):
        return None
    return found


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def chain_status(
    chain: Any,
    *,
    ticker: str,
    score_hash: str | None = None,
    tx_hash: str | None = None,
) -> dict[str, Any]:
    """Read ScoreAttestation. Never consults process memory.

    By ``tx_hash``: receipt plus a decoded ``ScoreAttested`` log.
    By ``score_hash``: ``attested``, ``getAttestation``, and ``verify``.
    By ticker alone: ``hashesForTicker``, then the same getters.
    A probe that throws becomes a generic error. The exception text is dropped.
    """
    symbol = ticker.strip().upper()
    body: dict[str, Any] = {
        "ticker": symbol,
        "attested": False,
        "tx": None,
        "attestedAt": None,
        "claimedAt": None,
        "attester": None,
        "score_hash": score_hash,
        "status": "absent",
        "source": "chain",
    }
    event = None
    if tx_hash:
        reader = getattr(chain, "receipt_event", None)
        if not callable(reader):
            body["status"] = "unavailable"
            body["reason"] = FAILED_REASON
            body["message"] = FAILED_MESSAGE
            return body
        try:
            event = reader(tx_hash)
        except Exception:
            event = None
        if not isinstance(event, dict):
            body["tx"] = tx_hash
            body["status"] = "pending"
            body["message"] = "Receipt is not on chain yet."
            return body
        body["tx"] = tx_hash
        body["event"] = {
            "ticker": event.get("ticker"),
            "score_hash": event.get("score_hash"),
            "attested_at": _as_int(event.get("attested_at")),
            "claimed_at": _as_int(event.get("claimed_at")),
            "attester": event.get("attester"),
        }
        score_hash = str(event.get("score_hash") or score_hash or "")
        body["score_hash"] = score_hash or None
        if str(event.get("ticker") or "").upper() != symbol:
            body["status"] = "absent"
            body["attested"] = False
            body["message"] = "Receipt event ticker does not match."
            return body
    digest = (score_hash or "").strip()
    if not digest:
        listing = getattr(chain, "hashes_for_ticker", None)
        if callable(listing):
            try:
                found = listing(symbol)
            except Exception:
                found = None
            if isinstance(found, list) and found and isinstance(found[-1], str):
                digest = found[-1]
                body["score_hash"] = digest
    if not digest:
        if event is not None:
            body["status"] = "confirmed"
            body["attested"] = True
            body["attestedAt"] = _as_int(event.get("attested_at"))
        return body
    attested_fn = getattr(chain, "attested", None)
    attested_flag: bool | None = None
    if callable(attested_fn):
        try:
            attested_flag = _as_bool(attested_fn(digest))
        except Exception:
            attested_flag = None
    if attested_flag is None and event is None:
        body["status"] = "unavailable"
        body["reason"] = FAILED_REASON
        body["message"] = FAILED_MESSAGE
        return body
    record = None
    getter = getattr(chain, "get_attestation", None)
    if callable(getter):
        try:
            record = getter(digest)
        except Exception:
            record = None
    verified = None
    verifier = getattr(chain, "verify", None)
    if callable(verifier):
        try:
            verified = verifier(digest, symbol)
        except Exception:
            verified = None
    ok = attested_flag is True
    if isinstance(verified, tuple) and verified:
        ok = ok and verified[0] is True
    elif attested_flag is not True:
        ok = False
    if isinstance(record, dict):
        if str(record.get("ticker") or "").upper() not in {"", symbol}:
            ok = False
        body["attestedAt"] = _as_int(record.get("attested_at"))
        body["claimedAt"] = _as_int(record.get("claimed_at"))
        body["attester"] = record.get("attester")
    elif isinstance(verified, tuple) and len(verified) >= 2:
        body["attestedAt"] = _as_int(verified[1])
        if len(verified) >= 3:
            body["attester"] = verified[2]
    body["attested"] = bool(ok)
    body["status"] = "confirmed" if ok else "absent"
    if event is not None and ok:
        body["tx"] = tx_hash
        body["status"] = "confirmed"
    return body


class AttestWorker:
    """Single thread. ``submit`` broadcasts. ``process_once`` only reconciles."""

    def __init__(
        self,
        *,
        store: Any,
        settings: AttesterSettings,
        chain: Chain | None = None,
        autostart: bool = True,
    ) -> None:
        self.store = store
        self.settings = settings
        self._chain = chain
        self.autostart = autostart
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._submit_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()

    def chain(self) -> Chain:
        if self._chain is None:
            self._chain = Web3Chain(self.settings)
        return self._chain

    def kick(self) -> None:
        """Start the reconciler if this worker is enabled. Returns immediately."""
        if not self.settings.enabled or not self.autostart:
            return
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                self._wake.set()
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="rwa-attest-worker",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        try:
            self.recover_broadcasts()
        except Exception as exc:  # noqa: BLE001 — keep the thread up
            logger.info(
                "attest recover error: %s",
                redact(str(exc), self.settings.private_key, self.settings.rpc_url),
            )
        while not self._stop.is_set():
            worked = False
            try:
                worked = self.process_once()
            except Exception as exc:  # noqa: BLE001 — keep the thread up
                logger.info(
                    "attest worker loop error: %s",
                    redact(str(exc), self.settings.private_key, self.settings.rpc_url),
                )
            if not worked:
                self._wake.wait(timeout=2.0)
                self._wake.clear()

    def submit(
        self,
        *,
        canonical: bytes,
        score_hash: str,
        ticker: str,
        claimed_at: int,
        now: float | None = None,
    ) -> SubmitResult:
        """Hash must be of ``canonical`` from this request. Check attested, then broadcast once."""
        clock = time.time() if now is None else float(now)
        secret = self.settings.private_key
        rpc_url = self.settings.rpc_url
        if not self.settings.enabled:
            return SubmitResult(
                status="disabled",
                reason="disabled",
                message=self.settings.disabled_reason,
            )
        try:
            _require_request_bytes(canonical, score_hash, ticker, claimed_at)
        except TerminalAttestError as exc:
            safe = redact(str(exc), secret, rpc_url)
            logger.info("attest refused: %s", safe)
            return SubmitResult(
                status="failed",
                reason=FAILED_REASON,
                message=FAILED_MESSAGE,
                error=safe,
            )
        local = _local_refusal(
            self.settings, score_hash, ticker, int(claimed_at)
        )
        if local:
            logger.info("attest failed: %s", local)
            return SubmitResult(
                status="failed",
                reason=FAILED_REASON,
                message=FAILED_MESSAGE,
                error=local,
            )
        job: dict[str, Any] = {
            "score_hash": score_hash,
            "ticker": ticker,
            "claimed_at": int(claimed_at),
            "tx_hash": None,
            "nonce": None,
            "known_tx_hashes": [],
        }
        claimed = False

        def _on_submitted(tx_hash: str, nonce: int) -> None:
            self.store.note_broadcast(
                tx_hash=tx_hash,
                nonce=nonce,
                score_hash=score_hash,
                ticker=ticker,
                claimed_at=int(claimed_at),
                now=clock,
            )
            job["tx_hash"] = tx_hash
            job["nonce"] = nonce
            job["known_tx_hashes"] = [tx_hash]

        def _rejected(exc: BaseException) -> SubmitResult | None:
            """Drop the hash when the node refused the raw transaction outright."""
            if not (job.get("tx_hash") and _unambiguous_rejection(exc)):
                return None
            self.store.drop_inflight(str(job["tx_hash"]))
            safe = redact(str(exc), secret, rpc_url)
            logger.info("attest rejected: %s", safe)
            return SubmitResult(
                status="failed",
                reason=FAILED_REASON,
                message=FAILED_MESSAGE,
                error=safe,
            )

        # Check attested and claim the payload hash under one lock, before send.
        # A second identical POST sees the claim and does not broadcast.
        with self._submit_lock:
            existing = self.store.inflight_for_hash(score_hash)
            if existing is not None:
                return SubmitResult(
                    status="pending",
                    tx_hash=existing.get("tx_hash"),
                    reason="broadcast_pending",
                    message="Broadcast already in flight. No second transaction was sent.",
                )
            gate, attested_at, gate_error = self._preflight(score_hash, ticker)
            if gate == "failed":
                logger.info("attest failed: %s", gate_error)
                return SubmitResult(
                    status="failed",
                    reason=FAILED_REASON,
                    message=FAILED_MESSAGE,
                    error=gate_error,
                )
            if gate == "confirmed":
                return SubmitResult(status="confirmed", attested_at=attested_at)
            self.store.reserve_inflight(
                score_hash=score_hash,
                ticker=ticker,
                claimed_at=int(claimed_at),
                now=clock,
            )
            claimed = True
        try:
            outcome = send_one(
                job,
                self.chain(),
                self.settings,
                on_submitted=_on_submitted,
                receipt_timeout=self.settings.wait_seconds,
                skip_initial_verify=True,
            )
            if outcome.tx_hash and not job.get("tx_hash") and not outcome.already:
                self.store.note_broadcast(
                    tx_hash=outcome.tx_hash,
                    nonce=int(job["nonce"]) if job.get("nonce") is not None else 0,
                    score_hash=score_hash,
                    ticker=ticker,
                    claimed_at=int(claimed_at),
                    now=clock,
                )
                job["tx_hash"] = outcome.tx_hash
                job["known_tx_hashes"] = [outcome.tx_hash]
        except TerminalAttestError as exc:
            rejected = _rejected(exc)
            if rejected is not None:
                return rejected
            safe = redact(str(exc), secret, rpc_url)
            logger.info("attest failed: %s", safe)
            if job.get("tx_hash"):
                return SubmitResult(
                    status="pending",
                    tx_hash=job.get("tx_hash"),
                    reason="broadcast_pending",
                    message="Broadcast is in flight. No second transaction was sent.",
                )
            return SubmitResult(
                status="failed",
                reason=FAILED_REASON,
                message=FAILED_MESSAGE,
                error=safe,
            )
        except Exception as exc:  # noqa: BLE001 — redacted; an accepted broadcast stays pending
            rejected = _rejected(exc)
            if rejected is not None:
                return rejected
            safe = redact(str(exc), secret, rpc_url)
            logger.info("attest error: %s", safe)
            if job.get("tx_hash"):
                return SubmitResult(
                    status="pending",
                    tx_hash=job.get("tx_hash"),
                    reason="broadcast_pending",
                    message="Broadcast is in flight. No second transaction was sent.",
                    error=safe,
                )
            return SubmitResult(
                status="failed",
                reason=FAILED_REASON,
                message=FAILED_MESSAGE,
                error=safe,
            )
        finally:
            if claimed and not job.get("tx_hash"):
                self.store.release_reserve(score_hash)
        if outcome.already or (outcome.attested_at is not None and outcome.tx_hash):
            if job.get("tx_hash"):
                self.store.drop_inflight(str(job["tx_hash"]))
            return SubmitResult(
                status="confirmed",
                tx_hash=outcome.tx_hash,
                attested_at=outcome.attested_at,
            )
        if job.get("tx_hash") and _status_of(_raw_receipt(self.chain(), str(job["tx_hash"]))) == 0:
            self.store.drop_inflight(str(job["tx_hash"]))
            return SubmitResult(
                status="failed",
                reason=FAILED_REASON,
                message=FAILED_MESSAGE,
            )
        if job.get("tx_hash") or outcome.tx_hash:
            return SubmitResult(
                status="pending",
                tx_hash=job.get("tx_hash") or outcome.tx_hash,
                reason="broadcast_pending",
                message="Broadcast is in flight.",
            )
        return SubmitResult(
            status="failed",
            reason=FAILED_REASON,
            message=FAILED_MESSAGE,
        )

    def _preflight(self, score_hash: str, ticker: str) -> tuple[str, int | None, str]:
        """Read the chain once before a send.

        Returns ``failed`` (with a redacted message), ``confirmed`` (hash
        already attested), or ``ready`` (safe to consider a send).
        """
        secret = self.settings.private_key
        rpc_url = self.settings.rpc_url
        chain = self.chain()
        try:
            chain_id = int(chain.chain_id())
        except Exception as exc:
            return "failed", None, redact(str(exc), secret, rpc_url)
        if chain_id != BASE_SEPOLIA_CHAIN_ID:
            return (
                "failed",
                None,
                f"refusing eth_chainId {chain_id}; only {BASE_SEPOLIA_CHAIN_ID}",
            )
        try:
            ok, attested_at, _who = chain.verify(score_hash, ticker)
        except Exception as exc:
            return "failed", None, redact(str(exc), secret, rpc_url)
        if ok is True:
            if isinstance(attested_at, int) and not isinstance(attested_at, bool):
                return "confirmed", int(attested_at), ""
            return "confirmed", 0, ""
        return "ready", None, ""

    def recover_broadcasts(self, *, now: float | None = None) -> int:
        """Poll every in-flight broadcast once. Used on startup. Does not send."""
        if not self.settings.enabled:
            return 0
        clock = time.time() if now is None else float(now)
        rows = self.store.list_inflight()
        for job in rows:
            self._reconcile(job, clock)
        return len(rows)

    def process_once(self, *, now: float | None = None) -> bool:
        """Reconcile every in-flight broadcast once.

        True only when a row was removed. A pass that changes nothing returns
        False so the loop sleeps instead of spinning on a stuck head.
        """
        if not self.settings.enabled:
            return False
        clock = time.time() if now is None else float(now)
        jobs = [row for row in self.store.list_inflight() if row.get("tx_hash")]
        if not jobs:
            return False
        progressed = False
        for job in jobs:
            tx_hash = str(job["tx_hash"])
            self._reconcile(job, clock)
            if self.store.get_inflight(tx_hash) is None:
                progressed = True
        return progressed

    def _reconcile(self, job: dict[str, Any], clock: float) -> None:
        """Poll one saved hash. Remove it once any receipt exists or verify is true.

        A success receipt and a revert both leave the queue. A true ``verify``
        removes the row even when this hash has no receipt, so a duplicate that
        already landed cannot pin the loop. The saved broadcast hash is still
        not copied into a confirmed result. ``broadcast_dropped`` is only for
        a missing receipt past the deadline, with the nonce already consumed.
        """
        tx_hash = job.get("tx_hash")
        if not tx_hash:
            return
        chain = self.chain()
        landed = _receipt_hash(chain, job)
        status = _status_of(_raw_receipt(chain, str(tx_hash)))
        attested_at = None
        verified = False
        try:
            result = chain.verify(job["score_hash"], job["ticker"])
        except Exception:
            result = None
        if isinstance(result, tuple) and result and result[0] is True:
            verified = True
            if len(result) >= 2 and isinstance(result[1], int) and not isinstance(result[1], bool):
                attested_at = int(result[1])
        if landed or verified or status is not None:
            if landed or status == 1:
                logger.info("attest broadcast confirmed from receipt hash=%s", landed or tx_hash)
                if attested_at is not None:
                    job["attested_at"] = attested_at
                self._bump_nonce(chain, job)
            elif status is not None:
                logger.info("attest broadcast receipt status=%s hash=%s", status, tx_hash)
                self._bump_nonce(chain, job)
            else:
                logger.info("attest broadcast already verified hash=%s", tx_hash)
            self.store.drop_inflight(str(tx_hash))
            return
        if self._broadcast_dropped(job, chain, clock):
            logger.info("attest broadcast dropped hash=%s reason=broadcast_dropped", tx_hash)
            self.store.drop_inflight(str(tx_hash), reason="broadcast_dropped")
            return

    def _bump_nonce(self, chain: Chain, job: dict[str, Any]) -> None:
        try:
            if hasattr(chain, "_nonce") and job.get("nonce") is not None:
                chain._nonce = int(job["nonce"]) + 1
        except Exception:
            return

    def _broadcast_dropped(self, job: dict[str, Any], chain: Chain, clock: float) -> bool:
        started = job.get("broadcast_at")
        if started is None:
            return False
        deadline = float(started) + float(self.settings.broadcast_deadline_seconds)
        if clock < deadline:
            return False
        receipt_of = getattr(chain, "get_receipt", None)
        if not callable(receipt_of):
            return False
        try:
            receipt = receipt_of(job["tx_hash"])
        except Exception:
            receipt = None
        if receipt is not None:
            return False
        counter = getattr(chain, "transaction_count", None)
        lookup = getattr(chain, "get_tx", None)
        if not callable(counter) or not callable(lookup):
            return False
        saved = job.get("nonce")
        if saved is None:
            return False
        try:
            latest = int(counter("latest"))
        except Exception:
            return False
        if latest <= int(saved):
            return False
        try:
            other = lookup(job["tx_hash"])
        except Exception:
            other = None
        return other is None


def main() -> None:
    """Reconcile in-flight broadcasts once, then exit. No send if disabled."""
    from .store import open_store

    settings = AttesterSettings.from_env()
    if not settings.enabled:
        print(settings.disabled_reason)
        return
    store = open_store()
    worker = AttestWorker(store=store, settings=settings, autostart=False)
    try:
        worker.recover_broadcasts()
        while worker.process_once():
            pass
    finally:
        store.close()


if __name__ == "__main__":
    main()
