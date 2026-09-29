"""Auto-attest worker. Mocks by default. Anvil dev accounts only when anvil exists.

Anvil account keys are derived at runtime from Foundry's published test mnemonic.
The hex keys are not stored in this file. They are not used on a real network.
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
from rwa_score.api.attest import attestation_payload, canonical_bytes, hash_canonical
from rwa_score.api.auto_attest import (
    HARD_GAS_CAP,
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

ROOT = Path(__file__).resolve().parents[1]


def _anvil_account(index: int):
    """Foundry's default test account. The mnemonic is public; the key is not a literal."""
    from eth_account import Account

    Account.enable_unaudited_hdwallet_features()
    phrase = " ".join(["test"] * 11 + ["junk"])
    return Account.from_mnemonic(phrase, account_path=f"m/44'/60'/0'/0/{index}")


def _anvil_key(index: int) -> str:
    raw = _anvil_account(index).key.hex()
    return raw if str(raw).startswith("0x") else "0x" + str(raw)


def _anvil_address(index: int) -> str:
    return _anvil_account(index).address
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


def _seed(scorer: TransparencyScorer) -> tuple[str, int, bytes]:
    report = scorer.score("NVDA")
    payload = attestation_payload(report)
    raw = canonical_bytes(payload)
    return hash_canonical(raw), int(payload["as_of"]), raw


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
    api = ApiSettings()
    store = Store()
    app = create_app(
        settings=api,
        store=store,
        scorer=fixture_scorer,
        attester=settings,
        start_worker=False,
    )
    raw = store.create_key(name="paid", tier="paid")
    resp = TestClient(app).post("/v1/attest/NVDA", headers={"X-API-Key": raw})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "disabled"
    assert body["tx_hash"] is None
    assert body["canonical_b64"]
    assert body["payload"]["ticker"] == "NVDA"
    assert store.inflight_for_hash(body["score_hash"]) is None


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
    api = ApiSettings()
    store = Store()
    digest, claimed, raw_bytes = _seed(fixture_scorer)
    worker = _worker(store, settings, chain)
    with caplog.at_level(logging.DEBUG, logger="rwa_score.api.auto_attest"):
        logging.getLogger("rwa_score.api.auto_attest").info("settings %s", settings)
        result = worker.submit(
            canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed, now=1_000.0
        )
    assert result.status == "failed"
    blob = caplog.text + repr(settings) + str(result.error)
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
    resp = TestClient(app).post("/v1/attest/NVDA", headers={"X-API-Key": raw})
    assert key not in resp.text
    assert "11" * 32 not in resp.text
    assert result.error not in resp.text


def test_wrong_chain_is_terminal_and_does_not_send(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    chain = Mock()
    chain.chain_id.return_value = 1
    store = Store()
    digest, claimed, raw_bytes = _seed(fixture_scorer)
    worker = _worker(store, _settings("0x" + "22" * 32), chain)
    result = worker.submit(
        canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed, now=1_000.0
    )
    assert result.status == "failed"
    assert "84532" in (result.error or "")
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
    store = Store()
    digest, claimed, raw_bytes = _seed(fixture_scorer)
    worker = _worker(store, _settings("0x" + "33" * 32), chain)
    result = worker.submit(
        canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed, now=1_000.0
    )
    assert result.status == "confirmed"
    assert result.attested_at == 1_700_000_111
    assert store.queue_depth() == 0


def test_verify_precheck_skips_send(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    chain = Mock()
    chain.chain_id.return_value = 84532
    chain.verify.return_value = (True, 42, "0x" + "cd" * 20)
    store = Store()
    digest, claimed, raw_bytes = _seed(fixture_scorer)
    worker = _worker(store, _settings("0x" + "44" * 32), chain)
    result = worker.submit(
        canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed, now=1_000.0
    )
    chain.attest.assert_not_called()
    assert result.status == "confirmed"
    assert result.attested_at == 42


def test_fee_above_cap_does_not_send(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    chain = Mock()
    chain.chain_id.return_value = 84532
    chain.verify.return_value = (False, 0, "0x" + "00" * 20)
    chain.fee_wei.return_value = 1
    store = Store()
    digest, claimed, raw_bytes = _seed(fixture_scorer)
    worker = _worker(store, _settings("0x" + "55" * 32, value_cap_wei=0), chain)
    result = worker.submit(
        canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed, now=1_000.0
    )
    chain.attest.assert_not_called()
    assert result.status == "failed"
    assert "RWA_ATTEST_VALUE_CAP_WEI" in (result.error or "")


def test_pre_broadcast_errors_can_be_retried(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    """A send that never broadcast may be tried again. A broadcast is never resent."""
    chain = Mock()
    chain.chain_id.side_effect = [ConnectionError("down"), ConnectionError("down"), 84532]
    chain.verify.return_value = (True, 7, "0x" + "11" * 20)
    store = Store()
    digest, claimed, raw_bytes = _seed(fixture_scorer)
    settings = _settings("0x" + "66" * 32, max_attempts=4, backoff_seconds=0.0)
    worker = _worker(store, settings, chain)
    first = worker.submit(canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed, now=10.0)
    assert first.status == "failed"
    second = worker.submit(canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed, now=10.0)
    assert second.status == "failed"
    third = worker.submit(canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed, now=10.0)
    assert third.status == "confirmed"
    assert third.attested_at == 7
    chain.attest.assert_not_called()
    assert store.queue_depth() == 0


def test_disabled_worker_process_once_is_a_no_op(tmp_path: Path) -> None:
    store = Store()
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
                _anvil_address(0),
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
        worker_addr = _anvil_address(1)
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
                _anvil_address(0),
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
                _anvil_address(0),
            ],
            stdout=subprocess.DEVNULL,
        )
        settings = AttesterSettings(
            private_key=_anvil_key(1),
            contract=address,
            rpc_url=rpc,
            value_cap_wei=0,
            gas_limit=300_000,
            max_attempts=3,
            backoff_seconds=0.0,
            enforce_contract_pin=False,
            wait_seconds=10,
        )
        chain = Web3Chain(settings)
        assert chain.chain_id() == 84532
        assert chain.fee_wei() == 0
        store = Store()
        digest, claimed, raw_bytes = _seed(fixture_scorer)
        assert digest != "0x" + "00" * 32
        worker = AttestWorker(store=store, settings=settings, chain=chain, autostart=False)
        first = worker.submit(
            canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed
        )
        assert first.status == "confirmed"
        assert first.tx_hash
        assert first.attested_at
        assert store.queue_depth() == 0
        nonce_after = chain._w3.eth.get_transaction_count(chain.address, "pending")
        second = worker.submit(
            canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed
        )
        assert second.status == "confirmed"
        assert second.tx_hash is None or second.tx_hash == first.tx_hash
        nonce_rerun = chain._w3.eth.get_transaction_count(chain.address, "pending")
        assert nonce_rerun == nonce_after
        ok, ts, who = chain.verify(digest, "NVDA")
        assert ok is True
        assert ts == first.attested_at
        assert who.lower() == chain.address.lower()
        import base64
        import json

        from rwa_score.api.verify import main as verify_main

        saved = tmp_path / "nvda-attest.json"
        saved.write_text(
            json.dumps(
                {
                    "ticker": "NVDA",
                    "score_hash": digest,
                    "canonical_b64": base64.b64encode(raw_bytes).decode("ascii"),
                    "payload": json.loads(raw_bytes.decode("utf-8")),
                    "tx_hash": first.tx_hash,
                    "as_of": claimed,
                }
            ),
            encoding="utf-8",
        )
        assert (
            verify_main(
                [
                    "NVDA",
                    "--json",
                    "--payload-file",
                    str(saved),
                    "--rpc-url",
                    rpc,
                    "--contract",
                    address,
                    "--attester",
                    chain.address,
                ]
            )
            == 0
        )
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
    store = Store()
    digest, claimed, raw_bytes = _seed(fixture_scorer)
    bad_contract = _settings("0x" + "77" * 32, contract="0x" + "ab" * 20)
    worker = _worker(store, bad_contract, chain)
    refused = worker.submit(
        canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed, now=1.0
    )
    assert refused.status == "failed"
    assert "pinned" in (refused.error or "")
    chain.chain_id.assert_not_called()
    chain.attest.assert_not_called()

    bad_chain = _settings("0x" + "77" * 32, chain_id=1)
    worker = _worker(store, bad_chain, chain)
    failed = worker.submit(
        canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed, now=2.0
    )
    assert failed.status == "failed"
    assert "configured chain id" in (failed.error or "")
    chain.attest.assert_not_called()


def test_sca_refuses_a_hash_not_from_this_request(tmp_path: Path) -> None:
    chain = Mock()
    chain.chain_id.return_value = 84532
    store = Store()
    digest = "0x" + "ab" * 32
    worker = _worker(store, _settings("0x" + "88" * 32), chain)
    result = worker.submit(
        canonical=b"{}", score_hash=digest, ticker="NVDA", claimed_at=0, now=1.0
    )
    assert result.status == "failed"
    assert "not computed from this request" in (result.error or "")
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
    store = Store()
    digest, claimed, raw_bytes = _seed(fixture_scorer)
    worker = _worker(store, _settings("0x" + "99" * 32), chain)
    result = worker.submit(
        canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed, now=1.0
    )
    assert result.status == "confirmed"
    assert result.attested_at == 1_700_000_222
    assert result.error is None


def test_sca_gas_cap_and_subject_bounds(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    chain = Mock()
    chain.chain_id.return_value = 84532
    store = Store()
    digest, claimed, raw_bytes = _seed(fixture_scorer)
    worker = _worker(store, _settings("0x" + "ab" * 32, gas_limit=HARD_GAS_CAP + 1), chain)
    result = worker.submit(
        canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed, now=1.0
    )
    assert result.status == "failed"
    assert "gas cap" in (result.error or "")
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
    api = ApiSettings()
    store = Store()
    app = create_app(settings=api, store=store, scorer=fixture_scorer, start_worker=False)
    resp = TestClient(app).post("/v1/attest/NVDA")
    assert resp.status_code == 401
    assert store.queue_depth() == 0


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
    settings = AttesterSettings.from_env()
    assert settings.enabled is True
    chain = Mock()
    chain.chain_id.return_value = 84532
    chain.verify.return_value = (False, 0, "0x" + "00" * 20)
    chain.fee_wei.return_value = 0
    chain.attest.side_effect = RuntimeError(f"rpc down {rpc}")
    api = ApiSettings()
    store = Store()
    digest, claimed, raw_bytes = _seed(fixture_scorer)
    worker = _worker(store, settings, chain)
    settings.max_attempts = 1
    with caplog.at_level(logging.INFO):
        logging.getLogger("web3").error("provider %s", rpc)
        logging.getLogger("rwa_score.api").error("status leaked %s", rpc)
        result = worker.submit(
            canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed, now=1_000.0
        )
    assert result.status == "failed"
    assert "SECRETRPCKEY123" not in (result.error or "")
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
    posted = TestClient(app).post("/v1/attest/NVDA", headers={"X-API-Key": raw})
    assert posted.status_code == 200
    assert posted.json()["reason"] == "attest_failed"
    assert posted.json()["message"] == "The attest job failed."
    assert (result.error or "missing-error") not in posted.text
    resp = TestClient(app).get("/v1/attest/NVDA/status", headers={"X-API-Key": raw})
    assert resp.status_code == 200
    assert "SECRETRPCKEY123" not in resp.text
    assert rpc not in resp.text
    assert (result.error or "missing-error") not in resp.text


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
    api = ApiSettings()
    store = Store()
    digest, claimed, raw_bytes = _seed(fixture_scorer)
    worker = _worker(store, settings, chain)
    with caplog.at_level(logging.INFO):
        logging.getLogger("web3.providers").info("dial %s", leaked)
        logging.getLogger("rwa_score.api.app").error("app blew up on %s", rpc)
        logging.getLogger().warning("root saw %s", leaked)
        result = worker.submit(
            canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed, now=1_000.0
        )
    assert result.status == "failed"
    blob = (result.error or "") + caplog.text
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
    posted = TestClient(app).post("/v1/attest/NVDA", headers={"X-API-Key": raw})
    assert posted.status_code == 200
    body = posted.json()
    assert body["reason"] == "attest_failed"
    assert body["message"] == "The attest job failed."
    resp = TestClient(app).get("/v1/attest/NVDA/status", headers={"X-API-Key": raw})
    assert resp.status_code == 200
    assert rpc.lower() not in resp.text.lower()
    assert "abcdefgh12345678" not in resp.text.lower()
    assert "zzyyxxww99887766" not in resp.text.lower()
    assert (result.error or "missing-error") not in resp.text


def test_fee_and_nonce_helpers() -> None:
    max_fee, priority = clamp_eip1559_fees(max_fee_gwei=20, base_fee_wei=50 * 10**9, bump=0)
    assert max_fee <= 20 * 1_000_000_000
    assert priority <= max_fee
    assert priority > 0
    low_fee, low_priority = clamp_eip1559_fees(max_fee_gwei=2, base_fee_wei=0, bump=0)
    assert low_fee <= 2 * 1_000_000_000
    assert low_priority <= low_fee
    assert choose_nonce(stored_nonce=4, unresolved=True, suggested=9) == 4
    assert choose_nonce(stored_nonce=None, unresolved=False, suggested=9) == 9


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
            _anvil_address(0),
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
    worker_addr = _anvil_address(1)
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
            _anvil_address(0),
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
            _anvil_address(0),
        ],
        stdout=subprocess.DEVNULL,
    )


@pytest.mark.skipif(shutil.which("anvil") is None or shutil.which("forge") is None, reason="anvil missing")
def test_anvil_receipt_timeout_then_it_lands(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    """Receipt wait times out, the tx later mines, and reconcile stores that hash.

    Mining is off (``anvil_setAutomine false``) until ``evm_mine``. Fails if
    ``_landed`` is disabled: confirm records only the hash that scan returns.
    A later ``process_once`` must not broadcast a second transaction.
    """
    port = _free_port()
    rpc = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            "anvil",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--chain-id",
            "84532",
            "--silent",
        ],
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
            private_key=_anvil_key(1),
            contract=address,
            rpc_url=rpc,
            value_cap_wei=0,
            gas_limit=300_000,
            max_attempts=5,
            backoff_seconds=0.0,
            enforce_contract_pin=False,
            max_fee_gwei=20,
            wait_seconds=30,
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
        store = Store()
        digest, claimed, raw_bytes = _seed(fixture_scorer)
        worker = AttestWorker(store=store, settings=settings, chain=chain, autostart=False)
        submitted = worker.submit(
            canonical=raw_bytes, score_hash=digest, ticker="NVDA", claimed_at=claimed
        )
        assert submitted.status == "pending"
        assert submitted.tx_hash
        pending = store.get_inflight(submitted.tx_hash)
        assert pending is not None
        assert pending["nonce"] == 0
        assert pending["broadcast_at"] is not None
        saved_hash = pending["tx_hash"]
        first_tx = chain._w3.eth.get_transaction(saved_hash)
        assert int(first_tx["nonce"]) == 0
        assert int(first_tx["maxPriorityFeePerGas"]) <= int(first_tx["maxFeePerGas"])
        assert int(first_tx["maxFeePerGas"]) > 0
        assert nonce_lookups == ["pending"]
        # Still unmined. Reconcile must not sign a replacement.
        assert worker.process_once(now=time.time() + 30) is True
        waiting = store.get_inflight(saved_hash)
        assert waiting is not None
        assert waiting["tx_hash"] == saved_hash
        assert waiting["nonce"] == 0
        assert waiting["known_tx_hashes"] == [saved_hash]
        assert nonce_lookups == ["pending"]
        chain._w3.provider.make_request("evm_mine", [])
        assert worker.process_once(now=time.time() + 60) is True
        assert store.get_inflight(saved_hash) is None
        receipt = chain._w3.eth.get_transaction_receipt(saved_hash)
        assert int(receipt["status"]) == 1
        mined = receipt["transactionHash"]
        mined_hex = mined.hex() if hasattr(mined, "hex") else str(mined)
        if not mined_hex.startswith("0x"):
            mined_hex = "0x" + mined_hex
        assert saved_hash.lower() == mined_hex.lower()
        assert nonce_lookups == ["pending"]
        assert chain._w3.eth.get_transaction_count(chain.address) == 1
        assert store.queue_depth() == 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_crash_recovery_polls_saved_hash_and_does_not_resend(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    chain = Mock()
    chain.chain_id.return_value = 84532
    chain.verify.return_value = (False, 0, "0x" + "00" * 20)
    chain.landed_hash.return_value = "0x" + "cd" * 32
    chain.attest.side_effect = AssertionError("must not send again")
    store = Store()
    digest, claimed, _raw = _seed(fixture_scorer)
    store.note_broadcast(
        tx_hash="0x" + "cd" * 32,
        nonce=3,
        score_hash=digest,
        ticker="NVDA",
        claimed_at=claimed,
    )
    worker = _worker(store, _settings("0x" + "61" * 32), chain)
    assert worker.recover_broadcasts(now=time.time()) == 1
    assert store.get_inflight("0x" + "cd" * 32) is None
    chain.attest.assert_not_called()


def test_broadcast_fails_only_after_deadline_when_nonce_is_taken(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    chain = Mock()
    chain.chain_id.return_value = 84532
    chain.verify.return_value = (False, 0, "0x" + "00" * 20)
    chain.landed_hash.return_value = None
    chain.get_receipt.return_value = None
    chain.get_tx.return_value = None
    chain.transaction_count.return_value = 0
    chain.attest.side_effect = AssertionError("must not send again")
    store = Store()
    digest, claimed, _raw = _seed(fixture_scorer)
    started = time.time() - 120
    store.note_broadcast(
        tx_hash="0x" + "ab" * 32,
        nonce=0,
        score_hash=digest,
        ticker="NVDA",
        claimed_at=claimed,
        now=started,
    )
    worker = _worker(
        store,
        _settings("0x" + "62" * 32, broadcast_deadline_seconds=30),
        chain,
    )
    assert worker.process_once(now=time.time()) is True
    waiting = store.get_inflight("0x" + "ab" * 32)
    assert waiting is not None
    assert waiting["nonce"] == 0
    chain.transaction_count.return_value = 1
    assert worker.process_once(now=time.time() + 30) is True
    assert store.get_inflight("0x" + "ab" * 32) is None
    chain.attest.assert_not_called()
