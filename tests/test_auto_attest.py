"""Auto-attest worker. Mocks by default. Anvil dev accounts only when anvil exists.

The Anvil private keys below are the public Foundry development accounts
(account 0 and account 1). They are not secrets and are not used on a real network.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from rwa_score.api.app import create_app
from rwa_score.api.attest import attestation_payload, canonical_bytes
from rwa_score.api.auto_attest import (
    AlreadyAttestedError,
    AttestWorker,
    AttesterSettings,
    TerminalAttestError,
    Web3Chain,
    redact,
)
from rwa_score.api.settings import ApiSettings
from rwa_score.api.store import Store
from rwa_score.scorer import TransparencyScorer

# Public Anvil defaults. See https://book.getfoundry.sh/anvil/ (Development accounts).
ANVIL_ACCOUNT_0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_ACCOUNT_1 = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"


def _settings(key: str = "", **overrides: object) -> AttesterSettings:
    data = dict(
        private_key=key,
        contract="0x2F073a3628D498d92956e7eFE2b26633eDa75b00" if key else "",
        rpc_url="http://127.0.0.1:9" if key else "",
        value_cap_wei=0,
        gas_limit=300_000,
        max_attempts=5,
        backoff_seconds=0.0,
    )
    data.update(overrides)
    return AttesterSettings(**data)  # type: ignore[arg-type]


def _seed(store: Store, scorer: TransparencyScorer) -> tuple[str, int]:
    report = scorer.score("NVDA")
    payload = attestation_payload(report)
    raw = canonical_bytes(payload)
    digest = store.save_attested_payload(ticker="NVDA", canonical=raw)
    return digest, int(payload["as_of"])


def _worker(store: Store, settings: AttesterSettings, chain: Mock) -> AttestWorker:
    return AttestWorker(store=store, settings=settings, chain=chain, autostart=False)


def test_unset_env_disables_worker_and_api_still_responds(
    tmp_path: Path,
    fixture_scorer: TransparencyScorer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RWA_ATTESTER_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("RWA_ATTESTATION_CONTRACT", raising=False)
    monkeypatch.delenv("BASE_SEPOLIA_RPC_URL", raising=False)
    settings = AttesterSettings.from_env()
    assert settings.enabled is False
    assert "No transaction was sent" in settings.disabled_reason
    api = ApiSettings(db_path=tmp_path / "api.sqlite")
    store = Store(api.db_path)
    app = create_app(
        settings=api,
        store=store,
        scorer=fixture_scorer,
        attester=settings,
        start_worker=False,
    )
    raw = store.create_key(name="paid", tier="paid")
    resp = TestClient(app).get("/v1/attest/NVDA", headers={"X-API-Key": raw})
    assert resp.status_code == 200
    body = resp.json()
    assert body["on_chain"]["worker"] == "disabled"
    assert body["on_chain"]["attested"] is False
    assert body["on_chain"]["tx"] is None
    assert store.latest_attest_job(body["score_hash"]) is None


def test_private_key_never_in_logs_response_or_repr(
    tmp_path: Path,
    fixture_scorer: TransparencyScorer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = "0x" + "11" * 32
    settings = _settings(key)
    assert key not in repr(settings)
    assert key not in str(settings)
    assert key not in settings.__repr__()
    chain = Mock()
    chain.chain_id.return_value = 84532
    chain.verify.return_value = (False, 0, "0x" + "00" * 20)
    chain.fee_wei.return_value = 0
    chain.attest.side_effect = RuntimeError(f"rpc exploded while using {key}")
    api = ApiSettings(db_path=tmp_path / "api.sqlite")
    store = Store(api.db_path)
    digest, claimed = _seed(store, fixture_scorer)
    store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=claimed)
    worker = _worker(store, settings, chain)
    with caplog.at_level(logging.DEBUG, logger="rwa_score.api.auto_attest"):
        logging.getLogger("rwa_score.api.auto_attest").info("settings %s", settings)
        assert worker.process_once(now=1_000.0) is True
    job = store.latest_attest_job(digest)
    assert job is not None
    blob = caplog.text + repr(settings) + str(job.get("last_error"))
    assert key not in blob
    assert "11" * 32 not in blob
    assert redact(f"leak {key}", key) == "leak [redacted]"
    app = create_app(
        settings=api,
        store=store,
        scorer=fixture_scorer,
        attester=settings,
        chain=chain,
        start_worker=False,
    )
    raw = store.create_key(name="paid", tier="paid")
    resp = TestClient(app).get("/v1/attest/NVDA", headers={"X-API-Key": raw})
    assert key not in resp.text
    assert "11" * 32 not in resp.text


def test_wrong_chain_is_terminal_and_does_not_send(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    chain = Mock()
    chain.chain_id.return_value = 1
    store = Store(tmp_path / "jobs.sqlite")
    digest, claimed = _seed(store, fixture_scorer)
    store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=claimed)
    worker = _worker(store, _settings("0x" + "22" * 32), chain)
    assert worker.process_once(now=1_000.0) is True
    job = store.latest_attest_job(digest)
    assert job is not None
    assert job["status"] == "failed"
    assert "84532" in (job["last_error"] or "")
    chain.attest.assert_not_called()
    chain.verify.assert_not_called()
    assert worker.process_once(now=2_000.0) is False


def test_already_attested_revert_counts_as_success(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    chain = Mock()
    chain.chain_id.return_value = 84532
    chain.fee_wei.return_value = 0
    chain.verify.side_effect = [
        (False, 0, "0x" + "00" * 20),
        (True, 1_700_000_111, "0x" + "ab" * 20),
    ]
    chain.attest.side_effect = AlreadyAttestedError("AlreadyAttested")
    store = Store(tmp_path / "jobs.sqlite")
    digest, claimed = _seed(store, fixture_scorer)
    store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=claimed)
    worker = _worker(store, _settings("0x" + "33" * 32), chain)
    assert worker.process_once(now=1_000.0) is True
    job = store.latest_attest_job(digest)
    assert job is not None
    assert job["status"] == "confirmed"
    payload = store.get_attested_payload(digest)
    assert payload is not None
    assert payload["attested_at"] == 1_700_000_111


def test_verify_precheck_skips_send(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    chain = Mock()
    chain.chain_id.return_value = 84532
    chain.verify.return_value = (True, 42, "0x" + "cd" * 20)
    store = Store(tmp_path / "jobs.sqlite")
    digest, claimed = _seed(store, fixture_scorer)
    store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=claimed)
    worker = _worker(store, _settings("0x" + "44" * 32), chain)
    assert worker.process_once(now=1_000.0) is True
    chain.attest.assert_not_called()
    job = store.latest_attest_job(digest)
    assert job is not None
    assert job["status"] == "confirmed"
    assert job["attested_at"] == 42


def test_fee_above_cap_does_not_send(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    chain = Mock()
    chain.chain_id.return_value = 84532
    chain.verify.return_value = (False, 0, "0x" + "00" * 20)
    chain.fee_wei.return_value = 1
    store = Store(tmp_path / "jobs.sqlite")
    digest, claimed = _seed(store, fixture_scorer)
    store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=claimed)
    worker = _worker(store, _settings("0x" + "55" * 32, value_cap_wei=0), chain)
    assert worker.process_once(now=1_000.0) is True
    chain.attest.assert_not_called()
    job = store.latest_attest_job(digest)
    assert job is not None
    assert job["status"] == "failed"
    assert "RWA_ATTEST_VALUE_CAP_WEI" in (job["last_error"] or "")


def test_retries_then_succeeds(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    chain = Mock()
    chain.chain_id.side_effect = [ConnectionError("down"), ConnectionError("down"), 84532]
    chain.verify.return_value = (True, 7, "0x" + "11" * 20)
    store = Store(tmp_path / "jobs.sqlite")
    digest, claimed = _seed(store, fixture_scorer)
    store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=claimed)
    settings = _settings("0x" + "66" * 32, max_attempts=4, backoff_seconds=0.0)
    worker = _worker(store, settings, chain)
    assert worker.process_once(now=10.0) is True
    assert store.latest_attest_job(digest)["status"] == "pending"
    assert worker.process_once(now=10.0) is True
    assert store.latest_attest_job(digest)["status"] == "pending"
    assert worker.process_once(now=10.0) is True
    job = store.latest_attest_job(digest)
    assert job["status"] == "confirmed"
    assert job["attempts"] == 3
    chain.attest.assert_not_called()


def test_disabled_worker_process_once_is_a_no_op(tmp_path: Path) -> None:
    store = Store(tmp_path / "off.sqlite")
    worker = AttestWorker(store=store, settings=AttesterSettings(), autostart=False)
    assert worker.process_once() is False
    worker.kick()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _anvil_ready(rpc: str) -> bool:
    try:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 1}))
        return int(w3.eth.chain_id) == 84532
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(shutil.which("anvil") is None or shutil.which("forge") is None, reason="anvil missing")
def test_anvil_attest_then_rerun_is_success(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    port = _free_port()
    rpc = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        ["anvil", "--host", "127.0.0.1", "--port", str(port), "--chain-id", "84532", "--silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not _anvil_ready(rpc):
            if proc.poll() is not None:
                pytest.fail("anvil exited before it was ready")
            time.sleep(0.1)
        assert _anvil_ready(rpc)
        env = os.environ.copy()
        env.pop("RWA_ATTESTER_PRIVATE_KEY", None)
        # Flags before the contract. --constructor-args is variadic, so it stays last.
        deployed = subprocess.check_output(
            [
                "forge",
                "create",
                "--rpc-url",
                rpc,
                "--private-key",
                ANVIL_ACCOUNT_0,
                "--broadcast",
                "src/ScoreAttestation.sol:ScoreAttestation",
                "--constructor-args",
                "0",
            ],
            cwd=CONTRACTS,
            text=True,
            env=env,
        )
        address = ""
        for line in deployed.splitlines():
            if "Deployed to:" in line:
                address = line.split("Deployed to:", 1)[1].strip()
        assert address.startswith("0x")
        from eth_account import Account

        worker_addr = Account.from_key(ANVIL_ACCOUNT_1).address
        subprocess.check_call(
            [
                "cast",
                "send",
                address,
                "setAttester(address,bool)",
                worker_addr,
                "true",
                "--rpc-url",
                rpc,
                "--private-key",
                ANVIL_ACCOUNT_0,
            ],
            stdout=subprocess.DEVNULL,
        )
        subprocess.check_call(
            [
                "cast",
                "send",
                address,
                "setFee(uint256)",
                "0",
                "--rpc-url",
                rpc,
                "--private-key",
                ANVIL_ACCOUNT_0,
            ],
            stdout=subprocess.DEVNULL,
        )
        settings = AttesterSettings(
            private_key=ANVIL_ACCOUNT_1,
            contract=address,
            rpc_url=rpc,
            value_cap_wei=0,
            gas_limit=300_000,
            max_attempts=3,
            backoff_seconds=0.0,
        )
        chain = Web3Chain(settings)
        assert chain.chain_id() == 84532
        assert chain.fee_wei() == 0
        store = Store(tmp_path / "anvil.sqlite")
        digest, claimed = _seed(store, fixture_scorer)
        assert digest != "0x" + "00" * 32
        store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=claimed)
        worker = AttestWorker(store=store, settings=settings, chain=chain, autostart=False)
        assert worker.process_once() is True
        first = store.latest_attest_job(digest)
        assert first is not None
        assert first["status"] == "confirmed"
        assert first["tx_hash"]
        payload = store.get_attested_payload(digest)
        assert payload is not None
        assert payload["attested_at"]
        assert payload["tx_hash"] == first["tx_hash"]
        nonce_after = chain._w3.eth.get_transaction_count(chain.address)
        store.enqueue_attest_job(
            score_hash=digest, ticker="NVDA", claimed_at=claimed, force=True
        )
        assert worker.process_once() is True
        second = store.latest_attest_job(digest)
        assert second is not None
        assert second["id"] != first["id"]
        assert second["status"] == "confirmed"
        nonce_rerun = chain._w3.eth.get_transaction_count(chain.address)
        assert nonce_rerun == nonce_after
        ok, ts, who = chain.verify(digest, "NVDA")
        assert ok is True
        assert ts == payload["attested_at"]
        assert who.lower() == chain.address.lower()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_web3_chain_refuses_to_build_when_disabled() -> None:
    with pytest.raises(TerminalAttestError):
        Web3Chain(AttesterSettings())
