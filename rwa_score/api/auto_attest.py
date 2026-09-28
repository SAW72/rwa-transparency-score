"""In-process attester for the current Base Sepolia ScoreAttestation.

Enqueue on ``GET /v1/attest``. A single background thread sends
``attest(scoreHash, ticker, as_of)`` and is not on the HTTP response path.
The pending queue is the sqlite ``attest_jobs`` table, so a process restart
keeps the work **when the sqlite file is still on disk**.

Render's free web service disk is ephemeral. A spin-down deletes that file.
This module does not add a second Render service. A Render background worker
or a paid plan with a persistent disk is the way to keep the queue across
spin-down. Spencer sets the attester key on Render himself. This process
reads ``RWA_ATTESTER_PRIVATE_KEY`` from the environment and never logs it.

If the key, contract, or RPC is unset, the worker stays disabled and the
API says so. It does not crash and it does not send.

One API instance only. During a Render deploy the old process and the new
one overlap. Postgres claims with ``FOR UPDATE SKIP LOCKED``, and a
broadcast transaction is retried at the same nonce. Do not run a second
worker on this key.
"""

from __future__ import annotations

import calendar
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
# EIP-1559 ceiling in code. Env may set a lower cap, never a higher one.
HARD_MAX_FEE_GWEI = 100
DEFAULT_MAX_FEE_GWEI = 20
# Drain protection. Env may lower these. The daily cap cannot exceed the hard max.
DEFAULT_MIN_INTERVAL_SECONDS = 300
HARD_DAILY_TX_CAP = 48
DEFAULT_DAILY_TX_CAP = 8
DEFAULT_VALUE_CAP_WEI = 0
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_SECONDS = 2.0
_REDACTED = "[redacted]"
FAILED_REASON = "attest_failed"
FAILED_MESSAGE = "The attest job failed."
THROTTLE_MESSAGES = {
    "min_interval": (
        "Attest for this ticker is inside the minimum interval. No transaction was sent."
    ),
    "daily_cap": "Daily attest transaction cap reached. No transaction was sent.",
}
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
        "min_interval_seconds",
        "daily_tx_cap",
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
        min_interval_seconds: int = DEFAULT_MIN_INTERVAL_SECONDS,
        daily_tx_cap: int = DEFAULT_DAILY_TX_CAP,
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
        self.max_fee_gwei = min(fee, float(HARD_MAX_FEE_GWEI))
        self.min_interval_seconds = max(0, int(min_interval_seconds))
        cap = int(daily_tx_cap)
        if cap < 0:
            cap = DEFAULT_DAILY_TX_CAP
        self.daily_tx_cap = min(cap, HARD_DAILY_TX_CAP)
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
            min_interval_seconds=_env_int(
                "RWA_ATTEST_MIN_INTERVAL_SECONDS", DEFAULT_MIN_INTERVAL_SECONDS
            ),
            daily_tx_cap=_env_int("RWA_ATTEST_DAILY_CAP", DEFAULT_DAILY_TX_CAP),
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


def _require_stored(store: Any, job: dict[str, Any]) -> None:
    """Only hashes this process stored from the scorer may be sent."""
    row = store.get_attested_payload(job["score_hash"])
    if row is None:
        raise TerminalAttestError("refusing hash that was not stored by the scorer")
    raw = row["canonical"]
    if hash_canonical(raw) != row["score_hash"] or row["score_hash"] != job["score_hash"]:
        raise TerminalAttestError("stored payload does not match its hash")
    payload = json.loads(raw.decode("utf-8"))
    if str(payload.get("ticker")) != str(job["ticker"]):
        raise TerminalAttestError("ticker does not match stored payload")
    if int(payload.get("as_of")) != int(job["claimed_at"]):
        raise TerminalAttestError("as_of does not match stored payload")
    _validate_subject(job["score_hash"], str(job["ticker"]), int(job["claimed_at"]))


def _tx_hex(tx_hash: Any) -> str:
    text = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    return text if text.startswith("0x") else "0x" + text


def _is_already(exc: BaseException) -> bool:
    blob = f"{exc} {getattr(exc, 'data', '')} {getattr(exc, 'message', '')}".lower()
    return ALREADY_ATTESTED_SELECTOR in blob or "alreadyattested" in blob


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
    """``(maxFeePerGas, maxPriorityFeePerGas)``, both at or under the hard ceiling.

    ``bump`` raises the priority about 12.5% per step so a same-nonce replacement
    is accepted, and still cannot pass the ceiling.
    """
    cap = int(HARD_MAX_FEE_GWEI * 1_000_000_000)
    requested = float(max_fee_gwei)
    if requested < 0:
        requested = 0.0
    requested_wei = int(min(requested, float(HARD_MAX_FEE_GWEI)) * 1_000_000_000)
    if requested_wei > cap:
        requested_wei = cap
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


def _parse_iso(text: str) -> float:
    return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))


def admission_block(store: Any, settings: AttesterSettings, ticker: str) -> str | None:
    """``min_interval`` or ``daily_cap`` when a new enqueue must not be sent."""
    interval = int(settings.min_interval_seconds)
    if interval > 0:
        latest = store.latest_attest_job_for_ticker(ticker)
        if latest is not None:
            try:
                age = time.time() - _parse_iso(str(latest["created_at"]))
            except (TypeError, ValueError, OSError):
                age = 0.0
            if age < interval:
                return "min_interval"
    cap = int(settings.daily_tx_cap)
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
    count = int(store.count_attest_jobs_since(since))
    if count >= cap:
        return "daily_cap"
    return None


class Web3Chain:
    """One signer, one nonce stream. Do not run a second worker on this key.

    A Render deploy can overlap two processes. The database claim lock
    (``FOR UPDATE SKIP LOCKED``) gives the job to one of them. If a hash and
    nonce are already stored, this sender re-waits or replaces at that same
    nonce. It does not allocate a new nonce while the old one is unresolved.
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

    @property
    def address(self) -> str:
        return self._account.address

    def chain_id(self) -> int:
        return int(self._w3.eth.chain_id)

    def fee_wei(self) -> int:
        return int(self._contract.functions.attestationFee().call())

    def verify(self, score_hash: str, ticker: str) -> tuple[bool, int, str]:
        ok, ts, who = self._contract.functions.verify(_hash_bytes(score_hash), ticker).call()
        return bool(ok), int(ts), str(who)

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
        if known:
            landed = self._landed(known, score_hash, ticker)
            if landed:
                if pending_nonce is not None:
                    self._nonce = int(pending_nonce) + 1
                return landed
        unresolved = pending_nonce is not None and bool(pending_tx)
        if unresolved:
            try:
                receipt = self._w3.eth.wait_for_transaction_receipt(
                    pending_tx, timeout=RECEIPT_TIMEOUT_SECONDS
                )
            except Exception:
                receipt = None
            if receipt is not None and int(receipt["status"]) == 1:
                self._nonce = int(pending_nonce) + 1
                return _tx_hex(receipt["transactionHash"])
            if receipt is not None and int(receipt["status"]) == 0:
                landed = self._landed(known, score_hash, ticker)
                if landed:
                    self._nonce = int(pending_nonce) + 1
                    return landed
                # Revert consumed the nonce. A later send may take a new one.
                self._nonce = None
            else:
                # Still pending. Replace at the same nonce. Never suggested+1.
                nonce = choose_nonce(
                    stored_nonce=int(pending_nonce),
                    unresolved=True,
                    suggested=int(pending_nonce) + 1,
                )
                self._nonce = nonce
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
                    bump=1,
                )
        if self._nonce is None:
            suggested = int(self._w3.eth.get_transaction_count(self._account.address, "pending"))
            self._nonce = choose_nonce(stored_nonce=None, unresolved=False, suggested=suggested)
        nonce = int(self._nonce)
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
        sent = self._w3.eth.send_raw_transaction(raw)
        hex_hash = _tx_hex(sent)
        if hex_hash not in known:
            known.append(hex_hash)
        if on_submitted is not None:
            on_submitted(hex_hash, int(nonce))
        # Stay on this nonce until a receipt says it was consumed.
        self._nonce = int(nonce)
        try:
            receipt = self._w3.eth.wait_for_transaction_receipt(
                sent, timeout=RECEIPT_TIMEOUT_SECONDS
            )
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


def send_one(
    job: dict[str, Any],
    chain: Chain,
    settings: AttesterSettings,
    *,
    on_submitted: Callable[[str, int], None] | None = None,
) -> SendOutcome:
    """Pre-check ``verify``, then send. ``AlreadyAttested`` is success."""
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
    return SendOutcome(
        tx_hash=tx_hash,
        attested_at=int(ts3) if ok3 else None,
        already=False,
    )


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


def on_chain_view(
    store: Any,
    score_hash: str,
    settings: AttesterSettings,
    *,
    admission: str | None = None,
) -> dict[str, Any]:
    if not settings.enabled:
        return {
            "attested": False,
            "tx": None,
            "attestedAt": None,
            "worker": "disabled",
            "reason": settings.disabled_reason,
        }
    payload = store.get_attested_payload(score_hash)
    job = store.latest_attest_job(score_hash)
    tx = None if payload is None else payload.get("tx_hash")
    attested_at = None if payload is None else payload.get("attested_at")
    status = None if job is None else job["status"]
    if tx is None and job is not None:
        tx = job.get("tx_hash")
    if attested_at is None and job is not None and job.get("status") == "confirmed":
        attested_at = job.get("attested_at")
    attested = status == "confirmed" or attested_at is not None
    body: dict[str, Any] = {
        "attested": bool(attested),
        "tx": tx,
        "attestedAt": attested_at,
        "worker": "enabled",
        "status": status or "absent",
    }
    if admission in THROTTLE_MESSAGES:
        body["status"] = "throttled"
        body["reason"] = admission
        body["message"] = THROTTLE_MESSAGES[admission]
        return body
    if status == "failed":
        body["reason"] = FAILED_REASON
        body["message"] = FAILED_MESSAGE
    elif status == "pending" and job is not None and job.get("last_error") in THROTTLE_MESSAGES:
        code = str(job["last_error"])
        body["status"] = "throttled"
        body["reason"] = code
        body["message"] = THROTTLE_MESSAGES[code]
    return body


class AttestWorker:
    """Single thread. ``process_once`` is what tests call. ``kick`` is async."""

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
        self._stop = threading.Event()
        self._wake = threading.Event()

    def chain(self) -> Chain:
        if self._chain is None:
            self._chain = Web3Chain(self.settings)
        return self._chain

    def kick(self) -> None:
        """Start the thread if this worker is enabled. Returns immediately."""
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

    def _promote_if_landed(self, job: dict[str, Any]) -> bool:
        """If the subject is already on chain, confirm it with the mined hash."""
        if not job.get("tx_hash") and not (job.get("known_tx_hashes") or []):
            return False
        try:
            verified = self.chain().verify(job["score_hash"], job["ticker"])
        except Exception:
            return False
        if not (isinstance(verified, tuple) and verified and verified[0] is True):
            return False
        landed = _receipt_hash(self.chain(), job)
        attested_at = None
        if len(verified) >= 2 and isinstance(verified[1], int):
            attested_at = int(verified[1])
        logger.info(
            "attest job %s confirmed from chain hash=%s",
            job["id"],
            job["score_hash"],
        )
        self.store.finish_attest_job(
            job["id"],
            status="confirmed",
            tx_hash=landed,
            attested_at=attested_at,
            error=None,
        )
        return True

    def process_once(self, *, now: float | None = None) -> bool:
        """Handle one due job. False when the queue has nothing due."""
        if not self.settings.enabled:
            return False
        clock = time.time() if now is None else float(now)
        job = self.store.claim_next_attest_job(now=clock)
        if job is None:
            return False
        secret = self.settings.private_key
        rpc_url = self.settings.rpc_url

        def _on_submitted(tx_hash: str, nonce: int) -> None:
            self.store.note_submitted_tx(job["id"], tx_hash=tx_hash, nonce=nonce)
            job["tx_hash"] = tx_hash
            job["nonce"] = nonce
            known = list(job.get("known_tx_hashes") or [])
            if tx_hash not in known:
                known.append(tx_hash)
            job["known_tx_hashes"] = known

        try:
            _require_stored(self.store, job)
            outcome = send_one(job, self.chain(), self.settings, on_submitted=_on_submitted)
        except TerminalAttestError as exc:
            safe = redact(str(exc), secret, rpc_url)
            if self._promote_if_landed(job):
                return True
            logger.info("attest job %s failed: %s", job["id"], safe)
            self.store.finish_attest_job(job["id"], status="failed", error=safe)
            return True
        except Exception as exc:  # noqa: BLE001 — retry transient RPC / gas errors
            safe = redact(str(exc), secret, rpc_url)
            attempts = int(job["attempts"])
            if attempts >= self.settings.max_attempts:
                if self._promote_if_landed(job):
                    return True
                logger.info("attest job %s exhausted retries: %s", job["id"], safe)
                self.store.finish_attest_job(job["id"], status="failed", error=safe)
                return True
            delay = self.settings.backoff_seconds * (2 ** (attempts - 1))
            logger.info(
                "attest job %s retry %s in %ss: %s",
                job["id"],
                attempts,
                delay,
                safe,
            )
            self.store.finish_attest_job(
                job["id"],
                status="pending",
                error=safe,
                next_attempt_at=clock + delay,
            )
            return True
        logger.info(
            "attest job %s confirmed hash=%s already=%s",
            job["id"],
            job["score_hash"],
            outcome.already,
        )
        self.store.finish_attest_job(
            job["id"],
            status="confirmed",
            tx_hash=outcome.tx_hash,
            attested_at=outcome.attested_at,
            error=None,
        )
        return True


def main() -> None:
    """Drain due jobs once, then exit. For a future worker process. No send if disabled."""
    from .settings import ApiSettings
    from .store import open_store

    settings = AttesterSettings.from_env()
    if not settings.enabled:
        print(settings.disabled_reason)
        return
    cfg = ApiSettings.from_env()
    store = open_store(path=cfg.db_path, database_url=cfg.database_url)
    worker = AttestWorker(store=store, settings=settings, autostart=False)
    try:
        while worker.process_once():
            pass
    finally:
        store.close()


if __name__ == "__main__":
    main()
