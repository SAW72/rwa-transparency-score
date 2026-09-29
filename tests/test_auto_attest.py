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
    HARD_DAILY_TX_CAP,
    HARD_GAS_CAP,
    HARD_MAX_FEE_GWEI,
    PINNED_ATTESTATION_CONTRACT,
    RECEIPT_TIMEOUT_SECONDS,
    RPC_TIMEOUT_SECONDS,
    AlreadyAttestedError,
    AttestWorker,
    AttesterSettings,
    TerminalAttestError,
    Web3Chain,
    _assert_dedicated_attester,
    choose_nonce,
    clamp_eip1559_fees,
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
        min_interval_seconds=0,
        daily_tx_cap=48,
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
                "--unlocked",
                "--from",
                "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
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
                "--unlocked",
                "--from",
                "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
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
                "--unlocked",
                "--from",
                "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
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
            enforce_contract_pin=False,
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


def test_sca_refuses_unpinned_contract_and_wrong_configured_chain(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    chain = Mock()
    chain.chain_id.return_value = 84532
    store = Store(tmp_path / "pin.sqlite")
    digest, claimed = _seed(store, fixture_scorer)
    store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=claimed)
    bad_contract = _settings("0x" + "77" * 32, contract="0x" + "ab" * 20)
    worker = _worker(store, bad_contract, chain)
    assert worker.process_once(now=1.0) is True
    assert "pinned" in (store.latest_attest_job(digest)["last_error"] or "")
    chain.chain_id.assert_not_called()
    chain.attest.assert_not_called()

    store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=claimed, force=True)
    bad_chain = _settings("0x" + "77" * 32, chain_id=1)
    worker = _worker(store, bad_chain, chain)
    assert worker.process_once(now=2.0) is True
    failed = store.latest_attest_job(digest)
    assert failed["status"] == "failed"
    assert "configured chain id" in (failed["last_error"] or "")
    chain.attest.assert_not_called()


def test_sca_refuses_hash_the_scorer_did_not_store(tmp_path: Path) -> None:
    chain = Mock()
    chain.chain_id.return_value = 84532
    store = Store(tmp_path / "unstored.sqlite")
    digest = "0x" + "ab" * 32
    store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=0)
    worker = _worker(store, _settings("0x" + "88" * 32), chain)
    assert worker.process_once(now=1.0) is True
    job = store.latest_attest_job(digest)
    assert job["status"] == "failed"
    assert "not stored" in (job["last_error"] or "")
    chain.attest.assert_not_called()
    chain.chain_id.assert_not_called()


def test_sca_timeout_after_send_is_success_when_verify_is_true(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    chain = Mock()
    chain.chain_id.return_value = 84532
    chain.fee_wei.return_value = 0
    chain.verify.side_effect = [
        (False, 0, "0x" + "00" * 20),
        (True, 1_700_000_222, "0x" + "cd" * 20),
    ]
    chain.attest.side_effect = TimeoutError("receipt timeout")
    store = Store(tmp_path / "timeout.sqlite")
    digest, claimed = _seed(store, fixture_scorer)
    store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=claimed)
    worker = _worker(store, _settings("0x" + "99" * 32), chain)
    assert worker.process_once(now=1.0) is True
    job = store.latest_attest_job(digest)
    assert job["status"] == "confirmed"
    assert job["attested_at"] == 1_700_000_222
    assert "failed" not in (job["last_error"] or "")


def test_sca_gas_cap_and_subject_bounds(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    chain = Mock()
    chain.chain_id.return_value = 84532
    store = Store(tmp_path / "bounds.sqlite")
    digest, claimed = _seed(store, fixture_scorer)
    store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=claimed)
    worker = _worker(store, _settings("0x" + "ab" * 32, gas_limit=HARD_GAS_CAP + 1), chain)
    assert worker.process_once(now=1.0) is True
    assert "gas cap" in (store.latest_attest_job(digest)["last_error"] or "")
    chain.attest.assert_not_called()
    assert RPC_TIMEOUT_SECONDS <= 30
    assert RECEIPT_TIMEOUT_SECONDS <= 120
    assert HARD_GAS_CAP == 500_000
    with pytest.raises(TerminalAttestError):
        _assert_dedicated_attester(
            signer="0x" + "11" * 20,
            owner="0x" + "11" * 20,
            is_attester=True,
        )
    with pytest.raises(TerminalAttestError):
        _assert_dedicated_attester(
            signer="0x" + "22" * 20,
            owner="0x" + "11" * 20,
            is_attester=False,
        )


def test_sca_unauthenticated_attest_does_not_enqueue(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    api = ApiSettings(db_path=tmp_path / "api.sqlite")
    store = Store(api.db_path)
    app = create_app(settings=api, store=store, scorer=fixture_scorer, start_worker=False)
    resp = TestClient(app).get("/v1/attest/NVDA")
    assert resp.status_code == 401
    assert store.latest_attested_payload("NVDA") is None


def test_sca_no_private_key_flag_in_signer_or_runbook() -> None:
    root = Path(__file__).resolve().parents[1]
    for rel in (
        "rwa_score/api/auto_attest.py",
        "contracts/ATTESTER_RUNBOOK.md",
        "rwa_score/api/app.py",
    ):
        assert "--private-key" not in (root / rel).read_text(encoding="utf-8")
    assert PINNED_ATTESTATION_CONTRACT == "0x2F073a3628D498d92956e7eFE2b26633eDa75b00"


def test_web3_chain_refuses_to_build_when_disabled() -> None:
    with pytest.raises(TerminalAttestError):
        Web3Chain(AttesterSettings())


def test_runbook_single_worker_key_import_and_rotation_order() -> None:
    text = (ROOT / "contracts" / "ATTESTER_RUNBOOK.md").read_text(encoding="utf-8")
    assert "One worker only" in text
    assert "FOR UPDATE SKIP LOCKED" in text
    assert "same nonce" in text
    new_at = text.index('"setAttester(address,bool)" "$NEW_ATTESTER" true')
    old_at = text.index('"setAttester(address,bool)" "$OLD_ATTESTER" false')
    assert new_at < old_at
    assert "cast wallet import <name> --interactive" in text
    assert "cast wallet new <dir> <name>" in text
    assert "dashboard secret" in text
    assert "Do not revoke the old key first" in text


def test_settings_mapping_does_not_expose_private_key() -> None:
    key = "0x" + "ab" * 32
    settings = _settings(key, rpc_url="https://rpc.example/v2/SecretKey12345678")
    with pytest.raises(TypeError):
        vars(settings)
    assert "_private_key" not in getattr(settings, "__dict__", {})
    assert not hasattr(settings, "_private_key")
    assert settings.private_key == key
    assert key not in repr(settings)
    assert "SecretKey12345678" not in repr(settings)


def test_b3_secret_rpc_key_absent_from_db_logs_and_status(
    tmp_path: Path,
    fixture_scorer: TransparencyScorer,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BASE_SEPOLIA_RPC_URL's embedded key must not reach the DB, logs, or status."""
    rpc = "https://base-sepolia.example/v2/SECRETRPCKEY123"
    monkeypatch.setenv("BASE_SEPOLIA_RPC_URL", rpc)
    monkeypatch.setenv("RWA_ATTESTER_PRIVATE_KEY", "0x" + "44" * 32)
    monkeypatch.setenv("RWA_ATTESTATION_CONTRACT", PINNED_ATTESTATION_CONTRACT)
    monkeypatch.setenv("RWA_ATTEST_MIN_INTERVAL_SECONDS", "0")
    settings = AttesterSettings.from_env()
    assert settings.enabled is True
    chain = Mock()
    chain.chain_id.return_value = 84532
    chain.verify.return_value = (False, 0, "0x" + "00" * 20)
    chain.fee_wei.return_value = 0
    chain.attest.side_effect = RuntimeError(f"rpc down {rpc}")
    api = ApiSettings(db_path=tmp_path / "b3.sqlite")
    store = Store(api.db_path)
    digest, claimed = _seed(store, fixture_scorer)
    store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=claimed)
    worker = _worker(store, settings, chain)
    settings.max_attempts = 1
    with caplog.at_level(logging.INFO):
        logging.getLogger("web3").error("provider %s", rpc)
        logging.getLogger("rwa_score.api").error("status leaked %s", rpc)
        assert worker.process_once(now=1_000.0) is True
    job = store.latest_attest_job(digest)
    assert job is not None
    assert "SECRETRPCKEY123" not in (job["last_error"] or "")
    assert "SECRETRPCKEY123" not in caplog.text
    assert rpc not in caplog.text
    app = create_app(
        settings=api,
        store=store,
        scorer=fixture_scorer,
        attester=settings,
        chain=chain,
        start_worker=False,
    )
    raw = store.create_key(name="paid", tier="paid")
    resp = TestClient(app).get("/v1/attest/NVDA/status", headers={"X-API-Key": raw})
    assert resp.status_code == 200
    assert "SECRETRPCKEY123" not in resp.text
    assert rpc not in resp.text
    assert resp.json()["on_chain"]["reason"] == "attest_failed"


def test_rpc_url_never_in_last_error_status_or_any_logger(
    tmp_path: Path,
    fixture_scorer: TransparencyScorer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rpc = "https://base-sepolia.example/v2/AbCdEfGh12345678?token=ZzYyXxWw99887766"
    leaked = rpc.upper()
    key = "0x" + "44" * 32
    settings = _settings(key, rpc_url=rpc, max_attempts=1)
    chain = Mock()
    chain.chain_id.return_value = 84532
    chain.verify.return_value = (False, 0, "0x" + "00" * 20)
    chain.fee_wei.return_value = 0
    chain.attest.side_effect = RuntimeError(f"provider rejected {leaked}")
    api = ApiSettings(db_path=tmp_path / "api.sqlite")
    store = Store(api.db_path)
    digest, claimed = _seed(store, fixture_scorer)
    store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=claimed)
    worker = _worker(store, settings, chain)
    with caplog.at_level(logging.INFO):
        logging.getLogger("web3.providers").info("dial %s", leaked)
        logging.getLogger("rwa_score.api.app").error("app blew up on %s", rpc)
        logging.getLogger().warning("root saw %s", leaked)
        assert worker.process_once(now=1_000.0) is True
    job = store.latest_attest_job(digest)
    assert job is not None
    assert job["status"] == "failed"
    blob = (job["last_error"] or "") + caplog.text
    assert rpc.lower() not in blob.lower()
    assert "abcdefgh12345678" not in blob.lower()
    assert "zzyyxxww99887766" not in blob.lower()
    assert redact(f"see {leaked}", "", rpc) == "see [redacted]"
    app = create_app(
        settings=api,
        store=store,
        scorer=fixture_scorer,
        attester=settings,
        chain=chain,
        start_worker=False,
    )
    raw = store.create_key(name="paid", tier="paid")
    resp = TestClient(app).get("/v1/attest/NVDA/status", headers={"X-API-Key": raw})
    assert resp.status_code == 200
    body = resp.json()["on_chain"]
    assert body["reason"] == "attest_failed"
    assert body["message"] == "The attest job failed."
    assert rpc.lower() not in resp.text.lower()
    assert "abcdefgh12345678" not in resp.text.lower()
    assert "zzyyxxww99887766" not in resp.text.lower()
    assert job["last_error"] not in resp.text


def test_fee_clamp_and_env_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    max_fee, priority = clamp_eip1559_fees(max_fee_gwei=99999, base_fee_wei=50 * 10**9, bump=6)
    assert max_fee <= HARD_MAX_FEE_GWEI * 1_000_000_000
    assert priority <= max_fee
    assert priority > 0
    low_fee, low_priority = clamp_eip1559_fees(max_fee_gwei=2, base_fee_wei=0, bump=0)
    assert low_fee <= 2 * 1_000_000_000
    assert low_priority <= low_fee
    monkeypatch.setenv("RWA_ATTEST_MAX_FEE_GWEI", "99999")
    monkeypatch.setenv("RWA_ATTEST_DAILY_CAP", "10000")
    monkeypatch.setenv("RWA_ATTESTER_PRIVATE_KEY", "0x" + "66" * 32)
    monkeypatch.setenv("RWA_ATTESTATION_CONTRACT", PINNED_ATTESTATION_CONTRACT)
    monkeypatch.setenv("BASE_SEPOLIA_RPC_URL", "http://127.0.0.1:9")
    settings = AttesterSettings.from_env()
    assert settings.max_fee_gwei == float(HARD_MAX_FEE_GWEI)
    assert settings.daily_tx_cap == HARD_DAILY_TX_CAP
    assert choose_nonce(stored_nonce=4, unresolved=True, suggested=9) == 4
    assert choose_nonce(stored_nonce=None, unresolved=False, suggested=9) == 9


def test_min_interval_does_not_enqueue_or_send(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    settings = _settings("0x" + "55" * 32, min_interval_seconds=3600, daily_tx_cap=10)
    chain = Mock()
    chain.chain_id.return_value = 84532
    chain.verify.return_value = (False, 0, "0x" + "00" * 20)
    chain.fee_wei.return_value = 0
    chain.attest.return_value = "0x" + "ab" * 32
    api = ApiSettings(db_path=tmp_path / "gap.sqlite")
    store = Store(api.db_path)
    app = create_app(
        settings=api,
        store=store,
        scorer=fixture_scorer,
        attester=settings,
        chain=chain,
        start_worker=False,
    )
    client = TestClient(app)
    raw = store.create_key(name="paid", tier="paid")
    headers = {"X-API-Key": raw}
    first = client.get("/v1/attest/NVDA", headers=headers)
    assert first.status_code == 200
    assert first.json()["on_chain"].get("reason") != "min_interval"
    second = client.get("/v1/attest/NVDA", headers=headers)
    assert second.status_code == 200
    throttled = second.json()["on_chain"]
    assert throttled["status"] == "throttled"
    assert throttled["reason"] == "min_interval"
    assert "minimum interval" in throttled["message"]
    assert "No transaction was sent" in throttled["message"]
    assert store.count_attest_jobs_since("1970-01-01T00:00:00Z") == 1
    worker = _worker(store, settings, chain)
    assert worker.process_once(now=time.time()) is True
    chain.attest.assert_called_once()
    assert worker.process_once(now=time.time() + 10) is False


def test_daily_cap_does_not_enqueue(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    settings = _settings("0x" + "56" * 32, min_interval_seconds=0, daily_tx_cap=1)
    api = ApiSettings(db_path=tmp_path / "cap.sqlite")
    store = Store(api.db_path)
    app = create_app(
        settings=api,
        store=store,
        scorer=fixture_scorer,
        attester=settings,
        start_worker=False,
    )
    client = TestClient(app)
    raw = store.create_key(name="paid", tier="paid")
    headers = {"X-API-Key": raw}
    first = client.get("/v1/attest/NVDA", headers=headers)
    assert first.status_code == 200
    assert first.json()["on_chain"].get("reason") != "daily_cap"
    second = client.get("/v1/attest/NVDA", headers=headers)
    assert second.status_code == 200
    throttled = second.json()["on_chain"]
    assert throttled["status"] == "throttled"
    assert throttled["reason"] == "daily_cap"
    assert "Daily attest" in throttled["message"]
    assert "No transaction was sent" in throttled["message"]
    assert store.count_attest_jobs_since("1970-01-01T00:00:00Z") == 1


def _forge_deploy(rpc: str) -> str:
    env = os.environ.copy()
    env.pop("RWA_ATTESTER_PRIVATE_KEY", None)
    deployed = subprocess.check_output(
        [
            "forge",
            "create",
            "--rpc-url",
            rpc,
            "--unlocked",
            "--from",
            "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
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
    if not address.startswith("0x"):
        raise AssertionError(deployed)
    return address


def _allow_worker(rpc: str, contract: str) -> None:
    from eth_account import Account

    worker_addr = Account.from_key(ANVIL_ACCOUNT_1).address
    subprocess.check_call(
        [
            "cast",
            "send",
            contract,
            "setAttester(address,bool)",
            worker_addr,
            "true",
            "--rpc-url",
            rpc,
            "--unlocked",
            "--from",
            "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        ],
        stdout=subprocess.DEVNULL,
    )
    subprocess.check_call(
        [
            "cast",
            "send",
            contract,
            "setFee(uint256)",
            "0",
            "--rpc-url",
            rpc,
            "--unlocked",
            "--from",
            "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        ],
        stdout=subprocess.DEVNULL,
    )


@pytest.mark.skipif(shutil.which("anvil") is None or shutil.which("forge") is None, reason="anvil missing")
def test_anvil_receipt_timeout_then_it_lands(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    """Receipt wait times out, the tx later mines, and the job stores that hash.

    Fails if ``_landed`` is disabled: the confirmed tx hash is the receipt hash
    ``_landed`` returns, and a success that only noticed ``verify`` has no hash.
    A second send while the first is pending must reuse nonce 0.
    """
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
        address = _forge_deploy(rpc)
        _allow_worker(rpc, address)
        settings = AttesterSettings(
            private_key=ANVIL_ACCOUNT_1,
            contract=address,
            rpc_url=rpc,
            value_cap_wei=0,
            gas_limit=300_000,
            max_attempts=5,
            backoff_seconds=0.0,
            enforce_contract_pin=False,
            min_interval_seconds=0,
            max_fee_gwei=20,
        )
        chain = Web3Chain(settings)
        chain._w3.provider.make_request("anvil_setAutomine", [False])
        nonce_lookups: list[str] = []
        real_count = chain._w3.eth.get_transaction_count

        def _count(address: str, block: str = "latest") -> int:
            nonce_lookups.append(str(block))
            return int(real_count(address, block))

        chain._w3.eth.get_transaction_count = _count  # type: ignore[method-assign]

        def _timeout(*_args: object, **_kwargs: object) -> None:
            raise TimeoutError("receipt timeout")

        chain._w3.eth.wait_for_transaction_receipt = _timeout  # type: ignore[method-assign]
        store = Store(tmp_path / "timeout-lands.sqlite")
        digest, claimed = _seed(store, fixture_scorer)
        store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=claimed)
        worker = AttestWorker(store=store, settings=settings, chain=chain, autostart=False)
        assert worker.process_once() is True
        pending = store.latest_attest_job(digest)
        assert pending is not None
        assert pending["status"] == "pending"
        assert pending["nonce"] == 0
        assert pending["tx_hash"]
        first_tx = chain._w3.eth.get_transaction(pending["tx_hash"])
        assert int(first_tx["nonce"]) == 0
        assert int(first_tx["maxFeePerGas"]) <= HARD_MAX_FEE_GWEI * 1_000_000_000
        assert int(first_tx["maxPriorityFeePerGas"]) <= int(first_tx["maxFeePerGas"])
        assert int(first_tx["maxFeePerGas"]) > 0
        assert nonce_lookups == ["pending"]
        assert worker.process_once() is True
        replaced = store.latest_attest_job(digest)
        assert replaced is not None
        assert replaced["status"] == "pending"
        assert replaced["nonce"] == 0
        assert len(replaced["known_tx_hashes"]) == 2
        assert replaced["tx_hash"] != pending["tx_hash"]
        # Same-nonce replacement. The node may drop the first hash.
        # A new nonce would have called get_transaction_count again.
        assert nonce_lookups == ["pending"]
        second_tx = chain._w3.eth.get_transaction(replaced["tx_hash"])
        assert int(second_tx["nonce"]) == 0
        assert int(second_tx["maxFeePerGas"]) >= int(first_tx["maxFeePerGas"])
        assert int(second_tx["maxFeePerGas"]) <= HARD_MAX_FEE_GWEI * 1_000_000_000
        assert int(second_tx["maxPriorityFeePerGas"]) <= int(second_tx["maxFeePerGas"])
        chain._w3.provider.make_request("evm_mine", [])
        assert worker.process_once() is True
        job = store.latest_attest_job(digest)
        assert job is not None
        assert job["status"] == "confirmed"
        assert job["tx_hash"]
        receipt = chain._w3.eth.get_transaction_receipt(job["tx_hash"])
        assert int(receipt["status"]) == 1
        mined = receipt["transactionHash"]
        mined_hex = mined.hex() if hasattr(mined, "hex") else str(mined)
        if not mined_hex.startswith("0x"):
            mined_hex = "0x" + mined_hex
        assert job["tx_hash"].lower() == mined_hex.lower()
        assert chain._w3.eth.get_transaction_count(chain.address) == 1
        payload = store.get_attested_payload(digest)
        assert payload is not None
        assert payload["tx_hash"].lower() == mined_hex.lower()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
