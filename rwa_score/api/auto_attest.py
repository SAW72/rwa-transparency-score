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
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

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
DEFAULT_VALUE_CAP_WEI = 0
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_SECONDS = 2.0

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
]


def redact(text: str, secret: str) -> str:
    """Strip a private key from text. Empty secret is a no-op."""
    if not text or not secret:
        return text
    cleaned = text.replace(secret, "[redacted]")
    bare = secret[2:] if secret.lower().startswith("0x") else secret
    if bare:
        cleaned = cleaned.replace(bare, "[redacted]")
        cleaned = cleaned.replace(bare.lower(), "[redacted]")
    return cleaned


class _RedactFilter(logging.Filter):
    def __init__(self, secret: str) -> None:
        super().__init__()
        self._secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secret:
            return True
        record.msg = redact(str(record.msg), self._secret)
        if record.args:
            record.args = tuple(
                redact(item, self._secret) if isinstance(item, str) else item
                for item in record.args
            )
        return True


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    return float(raw)


class AttesterSettings:
    """Env-only attester config. ``repr`` never includes the private key."""

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
    ) -> None:
        self._private_key = (private_key or "").strip()
        self.contract = (contract or "").strip()
        self.rpc_url = (rpc_url or "").strip()
        self.value_cap_wei = int(value_cap_wei)
        self.gas_limit = int(gas_limit)
        self.max_attempts = int(max_attempts)
        self.backoff_seconds = float(backoff_seconds)
        if self._private_key:
            logger.addFilter(_RedactFilter(self._private_key))

    @property
    def private_key(self) -> str:
        return self._private_key

    @property
    def enabled(self) -> bool:
        return bool(self._private_key and self.contract and self.rpc_url)

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
    return bytes.fromhex(raw)


def _is_already(exc: BaseException) -> bool:
    blob = f"{exc} {getattr(exc, 'data', '')} {getattr(exc, 'message', '')}".lower()
    return ALREADY_ATTESTED_SELECTOR in blob or "alreadyattested" in blob


class Web3Chain:
    """One signer, one nonce stream. Do not run a second worker on this key."""

    def __init__(self, settings: AttesterSettings) -> None:
        if not settings.enabled:
            raise TerminalAttestError(DISABLED_REASON)
        from eth_account import Account
        from web3 import Web3

        self._settings = settings
        self._w3 = Web3(Web3.HTTPProvider(settings.rpc_url, request_kwargs={"timeout": 20}))
        self._account = Account.from_key(settings.private_key)
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
    ) -> str:
        with self._lock:
            return self._send(
                score_hash,
                ticker,
                claimed_at,
                value_wei=value_wei,
                gas_limit=gas_limit,
            )

    def _send(
        self,
        score_hash: str,
        ticker: str,
        claimed_at: int,
        *,
        value_wei: int,
        gas_limit: int,
    ) -> str:
        chain_id = int(self._w3.eth.chain_id)
        if chain_id != BASE_SEPOLIA_CHAIN_ID:
            raise TerminalAttestError(
                f"refusing eth_chainId {chain_id}; only {BASE_SEPOLIA_CHAIN_ID}"
            )
        if self._nonce is None:
            self._nonce = int(
                self._w3.eth.get_transaction_count(self._account.address, "pending")
            )
        nonce = self._nonce
        fn = self._contract.functions.attest(_hash_bytes(score_hash), ticker, int(claimed_at))
        try:
            tx = fn.build_transaction(
                {
                    "from": self._account.address,
                    "value": int(value_wei),
                    "gas": int(gas_limit),
                    "nonce": nonce,
                    "chainId": chain_id,
                }
            )
            signed = self._account.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None)
            if raw is None:
                raw = signed.rawTransaction
            tx_hash = self._w3.eth.send_raw_transaction(raw)
        except Exception as exc:
            self._nonce = None
            if _is_already(exc):
                raise AlreadyAttestedError("AlreadyAttested") from exc
            raise
        self._nonce = nonce + 1
        try:
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        except Exception:
            self._nonce = None
            raise
        if int(receipt.status) != 1:
            self._nonce = None
            reason = _revert_blob(self._w3, tx)
            if _is_already(RuntimeError(reason)):
                raise AlreadyAttestedError("AlreadyAttested")
            raise RuntimeError(redact(f"attest receipt status 0: {reason}", self._settings.private_key))
        return tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)


def _revert_blob(w3: Any, tx: dict[str, Any]) -> str:
    try:
        w3.eth.call(tx)
    except Exception as exc:  # noqa: BLE001 — surface the revert selector only
        return redact(str(exc), "")
    return ""


def send_one(job: dict[str, Any], chain: Chain, settings: AttesterSettings) -> SendOutcome:
    """Pre-check ``verify``, then send. ``AlreadyAttested`` is success."""
    chain_id = int(chain.chain_id())
    if chain_id != BASE_SEPOLIA_CHAIN_ID:
        raise TerminalAttestError(
            f"refusing eth_chainId {chain_id}; only {BASE_SEPOLIA_CHAIN_ID}"
        )
    ok, attested_at, _who = chain.verify(job["score_hash"], job["ticker"])
    if ok:
        return SendOutcome(tx_hash=None, attested_at=int(attested_at), already=True)
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
        )
    except AlreadyAttestedError:
        ok2, ts2, _who2 = chain.verify(job["score_hash"], job["ticker"])
        return SendOutcome(
            tx_hash=None,
            attested_at=int(ts2) if ok2 else None,
            already=True,
        )
    ok3, ts3, _who3 = chain.verify(job["score_hash"], job["ticker"])
    return SendOutcome(
        tx_hash=tx_hash,
        attested_at=int(ts3) if ok3 else None,
        already=False,
    )


def on_chain_view(store: Any, score_hash: str, settings: AttesterSettings) -> dict[str, Any]:
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
    if status == "failed" and job is not None:
        body["reason"] = job.get("last_error")
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
                    redact(str(exc), self.settings.private_key),
                )
            if not worked:
                self._wake.wait(timeout=2.0)
                self._wake.clear()

    def process_once(self, *, now: float | None = None) -> bool:
        """Handle one due job. False when the queue has nothing due."""
        if not self.settings.enabled:
            return False
        clock = time.time() if now is None else float(now)
        job = self.store.claim_next_attest_job(now=clock)
        if job is None:
            return False
        secret = self.settings.private_key
        try:
            outcome = send_one(job, self.chain(), self.settings)
        except TerminalAttestError as exc:
            safe = redact(str(exc), secret)
            logger.info("attest job %s failed: %s", job["id"], safe)
            self.store.finish_attest_job(job["id"], status="failed", error=safe)
            return True
        except Exception as exc:  # noqa: BLE001 — retry transient RPC / gas errors
            safe = redact(str(exc), secret)
            attempts = int(job["attempts"])
            if attempts >= self.settings.max_attempts:
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
    from .store import Store

    settings = AttesterSettings.from_env()
    if not settings.enabled:
        print(settings.disabled_reason)
        return
    store = Store(ApiSettings.from_env().db_path)
    worker = AttestWorker(store=store, settings=settings, autostart=False)
    try:
        while worker.process_once():
            pass
    finally:
        store.close()


if __name__ == "__main__":
    main()
