"""Fail-closed attester limits. No network. No private-key literals.

Anvil account keys are derived at runtime from Foundry's published test mnemonic.
"""

from __future__ import annotations

import math
import time
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from rwa_score.api.app import create_app
from rwa_score.api.attest import canonical_bytes, hash_canonical
from rwa_score.api.auto_attest import (
    BALANCE_HEALTH_CACHE_SECONDS,
    DEFAULT_MAX_FEE_GWEI,
    DEFAULT_MAX_PER_DAY,
    DEFAULT_MAX_PER_HOUR,
    DEFAULT_MAX_PER_KEY_PER_DAY,
    DEFAULT_MIN_BALANCE_WEI,
    DEFAULT_MIN_INTERVAL_SECONDS,
    DEFAULT_VALUE_CAP_WEI,
    HARD_MAX_FEE_GWEI,
    AttestWorker,
    AttesterSettings,
    SafetyRefusal,
    eip1559_fees_or_refuse,
    safety_status,
)
from rwa_score.api.settings import ApiSettings
from rwa_score.api.store import Store
from rwa_score.scorer import TransparencyScorer
from tests.test_auto_attest import _anvil_key, _settings, _worker


def _subject(ticker: str, as_of: int) -> tuple[str, int, bytes]:
    raw = canonical_bytes({"ticker": ticker, "as_of": as_of})
    return hash_canonical(raw), as_of, raw


def _ready_chain() -> Mock:
    chain = Mock()
    chain.chain_id.return_value = 84532
    chain.verify.return_value = (False, 0, "0x" + "00" * 20)
    chain.fee_wei.return_value = 0
    chain.get_receipt.return_value = {"status": 1}
    seq = {"n": 0}

    def _attest(*_args: object, **kwargs: object) -> str:
        seq["n"] += 1
        tx_hash = "0x" + f"{seq['n']:064x}"
        on_submitted = kwargs.get("on_submitted")
        if callable(on_submitted):
            on_submitted(tx_hash, seq["n"])
        return tx_hash

    chain.attest.side_effect = _attest
    return chain


def _submit(worker: AttestWorker, ticker: str, as_of: int, **kwargs: object):
    digest, claimed, raw = _subject(ticker, as_of)
    return worker.submit(
        canonical=raw,
        score_hash=digest,
        ticker=ticker,
        claimed_at=claimed,
        **kwargs,
    )


def _patch_por(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "rwa_score.health.probe_chainlink_por",
        lambda *args, **kwargs: False,
    )


def _client(settings: AttesterSettings, chain: Mock, scorer: TransparencyScorer):
    store = Store()
    app = create_app(
        settings=ApiSettings(),
        store=store,
        scorer=scorer,
        attester=settings,
        chain=chain,
        start_worker=False,
    )
    return TestClient(app), store


def test_from_env_kill_switch_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    key = _anvil_key(0)
    monkeypatch.setenv("RWA_ATTESTER_PRIVATE_KEY", key)
    monkeypatch.setenv("RWA_ATTESTATION_CONTRACT", "0x2F073a3628D498d92956e7eFE2b26633eDa75b00")
    monkeypatch.setenv("BASE_SEPOLIA_RPC_URL", "http://127.0.0.1:9")
    for name in (
        "RWA_ATTEST_ENABLED",
        "RWA_USE_FIXTURES",
        "RWA_ATTEST_MIN_BALANCE_WEI",
        "RWA_ATTEST_MAX_PER_HOUR",
        "RWA_ATTEST_MAX_PER_DAY",
        "RWA_ATTEST_MIN_INTERVAL_SECONDS",
        "RWA_ATTEST_MAX_PER_KEY_PER_DAY",
        "RWA_ATTEST_VALUE_CAP_WEI",
        "RWA_ATTEST_MAX_FEE_GWEI",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = AttesterSettings.from_env()
    assert settings.attest_enabled is False
    assert settings.enabled is False
    assert settings.use_fixtures is False
    assert settings.min_balance_wei == DEFAULT_MIN_BALANCE_WEI
    assert settings.max_per_hour == DEFAULT_MAX_PER_HOUR
    assert settings.max_per_day == DEFAULT_MAX_PER_DAY
    assert settings.min_interval_seconds == DEFAULT_MIN_INTERVAL_SECONDS
    assert settings.max_per_key_per_day == DEFAULT_MAX_PER_KEY_PER_DAY
    assert settings.value_cap_wei == DEFAULT_VALUE_CAP_WEI
    assert settings.max_fee_gwei == DEFAULT_MAX_FEE_GWEI
    assert key not in repr(settings)


def test_kill_switch_off_refuses_before_rpc(
    fixture_scorer: TransparencyScorer,
) -> None:
    chain = _ready_chain()
    settings = _settings(_anvil_key(1), attest_enabled=False)
    assert settings.configured is True
    assert settings.enabled is False
    worker = _worker(Store(), settings, chain)
    result = _submit(worker, "NVDA", 1_700_000_000, now=1_000.0)
    assert result.status == "disabled"
    assert result.reason == "attester_disabled"
    assert result.tx_hash is None
    chain.attest.assert_not_called()
    chain.chain_id.assert_not_called()
    chain.fee_wei.assert_not_called()
    assert worker.process_once() is False
    worker.autostart = True
    worker.kick()
    assert worker._thread is None

    client, store = _client(settings, chain, fixture_scorer)
    raw = store.create_key(name="paid", tier="paid")
    resp = client.post("/v1/attest/NVDA", headers={"X-API-Key": raw})
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "attester_disabled"
    assert "No transaction was sent" in body["message"]
    assert "score" not in body
    chain.attest.assert_not_called()


def test_kill_switch_on_can_send() -> None:
    chain = _ready_chain()
    settings = _settings(_anvil_key(2), attest_enabled=True, min_balance_wei=0)
    assert settings.enabled is True
    result = _submit(_worker(Store(), settings, chain), "NVDA", 1_700_000_001, now=2_000.0)
    assert result.status == "confirmed"
    assert result.tx_hash
    chain.attest.assert_called_once()


def test_balance_below_floor_sends_nothing_and_health_is_machine_readable(
    fixture_scorer: TransparencyScorer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_por(monkeypatch)
    floor = 10**18
    chain = _ready_chain()
    chain.balance_wei.return_value = 100
    settings = _settings(_anvil_key(3), min_balance_wei=floor)
    worker = _worker(Store(), settings, chain)
    result = _submit(worker, "NVDA", 1_700_000_002, now=3_000.0)
    assert result.status == "failed"
    assert result.reason == "attester_low_balance"
    assert result.tx_hash is None
    chain.attest.assert_not_called()
    chain.verify.assert_not_called()
    chain.fee_wei.assert_not_called()
    assert worker.last_balance_wei == 100

    client, _store = _client(settings, chain, fixture_scorer)
    # The app builds its own worker around the same chain.
    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["attester_low_balance"] is True
    assert body["attester_balance_wei"] == 100
    assert body["attester_min_balance_wei"] == floor
    assert body["attester_balance"] == "low"
    assert isinstance(body["attester_low_balance"], bool)
    chain.attest.assert_not_called()

    status = client.get(
        "/v1/attest/NVDA/status",
        headers={"X-API-Key": _store.create_key(name="paid", tier="paid")},
    )
    assert status.status_code == 200
    echoed = status.json()
    assert echoed["attester_low_balance"] is True
    assert echoed["attester_balance_wei"] == 100
    assert echoed["attester_min_balance_wei"] == floor


def test_balance_at_floor_is_ok_and_unknown_when_unread(
    fixture_scorer: TransparencyScorer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_por(monkeypatch)
    floor = 10**15
    chain = _ready_chain()
    chain.balance_wei.return_value = floor
    settings = _settings(_anvil_key(4), min_balance_wei=floor)
    result = _submit(_worker(Store(), settings, chain), "NVDA", 1_700_000_003, now=4_000.0)
    assert result.status == "confirmed"
    chain.attest.assert_called_once()

    client, store = _client(settings, chain, fixture_scorer)
    body = client.get("/health").json()
    assert body["attester_low_balance"] is False
    assert body["attester_balance_wei"] == floor
    assert body["attester_min_balance_wei"] == floor
    assert body["attester_balance"] == "ok"

    bare = AttestWorker(store=store, settings=settings, chain=None, autostart=False)
    unknown = safety_status(bare, settings)
    assert unknown["attester_low_balance"] is False
    assert unknown["attester_balance_wei"] is None
    assert unknown["attester_min_balance_wei"] == floor
    assert unknown["attester_balance"] == "unknown"


def test_hourly_and_daily_caps_refuse_with_no_tx() -> None:
    chain = _ready_chain()
    settings = _settings(
        _anvil_key(5),
        max_per_hour=1,
        max_per_day=1,
        min_interval_seconds=0,
        max_per_key_per_day=100,
        min_balance_wei=0,
    )
    worker = _worker(Store(), settings, chain)
    first = _submit(worker, "NVDA", 1_700_000_010, now=10_000.0)
    assert first.status == "confirmed"
    assert chain.attest.call_count == 1
    second = _submit(worker, "NVDA", 1_700_000_011, now=10_001.0)
    assert second.status == "failed"
    assert second.reason == "attest_hourly_cap"
    assert second.tx_hash is None
    assert chain.attest.call_count == 1
    chain.chain_id.assert_called_once()

    daily_chain = _ready_chain()
    daily = _worker(
        Store(),
        _settings(
            _anvil_key(5),
            max_per_hour=100,
            max_per_day=1,
            min_interval_seconds=0,
            max_per_key_per_day=100,
            min_balance_wei=0,
        ),
        daily_chain,
    )
    assert _submit(daily, "AAPL", 1_700_000_012, now=20_000.0).status == "confirmed"
    blocked = _submit(daily, "AAPL", 1_700_000_013, now=20_000.0 + 4000)
    assert blocked.reason == "attest_daily_cap"
    assert blocked.tx_hash is None
    assert daily_chain.attest.call_count == 1


def test_per_ticker_interval_blocks_same_ticker_only() -> None:
    chain = _ready_chain()
    settings = _settings(
        _anvil_key(6),
        max_per_hour=100,
        max_per_day=100,
        min_interval_seconds=1000,
        max_per_key_per_day=100,
        min_balance_wei=0,
    )
    worker = _worker(Store(), settings, chain)
    assert _submit(worker, "NVDA", 1_700_000_020, now=1_000.0).status == "confirmed"
    blocked = _submit(worker, "NVDA", 1_700_000_021, now=1_001.0)
    assert blocked.reason == "attest_ticker_interval"
    assert blocked.tx_hash is None
    other = _submit(worker, "AAPL", 1_700_000_022, now=1_001.0)
    assert other.status == "confirmed"
    assert chain.attest.call_count == 2


def test_per_api_key_daily_cap_and_same_hash_dedup() -> None:
    chain = _ready_chain()
    settings = _settings(
        _anvil_key(7),
        max_per_hour=100,
        max_per_day=100,
        min_interval_seconds=0,
        max_per_key_per_day=1,
        min_balance_wei=0,
    )
    worker = _worker(Store(), settings, chain)
    first = _submit(worker, "NVDA", 1_700_000_030, now=30_000.0, api_key_id=7)
    assert first.status == "confirmed"
    second = _submit(worker, "NVDA", 1_700_000_031, now=30_001.0, api_key_id=7)
    assert second.reason == "attest_key_daily_cap"
    assert second.tx_hash is None
    other_key = _submit(worker, "AAPL", 1_700_000_032, now=30_002.0, api_key_id=8)
    assert other_key.status == "confirmed"
    assert chain.attest.call_count == 2

    pending_chain = Mock()
    pending_chain.chain_id.return_value = 84532
    pending_chain.verify.return_value = (False, 0, "0x" + "00" * 20)
    pending_chain.fee_wei.return_value = 0
    pending_chain.get_receipt.return_value = None

    def _attest(*_args: object, **kwargs: object) -> str:
        tx_hash = "0x" + "cd" * 32
        on_submitted = kwargs.get("on_submitted")
        if callable(on_submitted):
            on_submitted(tx_hash, 1)
        return tx_hash

    pending_chain.attest.side_effect = _attest
    pending = _worker(
        Store(),
        _settings(
            _anvil_key(7),
            max_per_hour=100,
            max_per_day=100,
            min_interval_seconds=0,
            max_per_key_per_day=1,
            min_balance_wei=0,
        ),
        pending_chain,
    )
    digest, claimed, raw = _subject("TSLA", 1_700_000_040)
    opened = pending.submit(
        canonical=raw,
        score_hash=digest,
        ticker="TSLA",
        claimed_at=claimed,
        now=40_000.0,
        api_key_id=9,
    )
    assert opened.status == "pending"
    again = pending.submit(
        canonical=raw,
        score_hash=digest,
        ticker="TSLA",
        claimed_at=claimed,
        now=40_001.0,
        api_key_id=9,
    )
    assert again.reason == "broadcast_pending"
    assert again.tx_hash == opened.tx_hash
    assert pending_chain.attest.call_count == 1
    fresh = _submit(pending, "META", 1_700_000_041, now=40_002.0, api_key_id=9)
    assert fresh.reason == "attest_key_daily_cap"
    assert pending_chain.attest.call_count == 1


def test_per_api_key_daily_cap_on_post(
    fixture_scorer: TransparencyScorer,
) -> None:
    chain = _ready_chain()
    settings = _settings(
        _anvil_key(8),
        max_per_hour=100,
        max_per_day=100,
        min_interval_seconds=0,
        max_per_key_per_day=1,
        min_balance_wei=0,
    )
    client, store = _client(settings, chain, fixture_scorer)
    first_key = store.create_key(name="one", tier="paid")
    second_key = store.create_key(name="two", tier="paid")
    first = client.post("/v1/attest/NVDA", headers={"X-API-Key": first_key})
    assert first.status_code == 200
    assert first.json()["tx_hash"]
    second = client.post("/v1/attest/AAPL", headers={"X-API-Key": first_key})
    assert second.status_code == 429
    body = second.json()
    assert body["error"] == "attest_key_daily_cap"
    assert body["reason"] == "attest_key_daily_cap"
    assert body["tx_hash"] is None
    assert chain.attest.call_count == 1
    other = client.post("/v1/attest/AAPL", headers={"X-API-Key": second_key})
    assert other.status_code == 200
    assert other.json()["tx_hash"]
    assert chain.attest.call_count == 2


def test_fee_above_cap_code_sends_nothing() -> None:
    chain = _ready_chain()
    chain.fee_wei.return_value = 10**15
    settings = _settings(_anvil_key(9), value_cap_wei=0, min_balance_wei=0)
    result = _submit(_worker(Store(), settings, chain), "NVDA", 1_700_000_050, now=50_000.0)
    assert result.reason == "attest_fee_cap"
    assert result.tx_hash is None
    assert "RWA_ATTEST_VALUE_CAP_WEI" in (result.error or "")
    chain.attest.assert_not_called()


def test_gas_ceiling_and_hard_max_refuse_before_sign() -> None:
    chain = _ready_chain()
    over = _settings(_anvil_key(0), max_fee_gwei=101, min_balance_wei=0)
    refused = _submit(_worker(Store(), over, chain), "NVDA", 1_700_000_060, now=60_000.0)
    assert refused.reason == "attest_gas_fee_cap"
    assert refused.tx_hash is None
    chain.chain_id.assert_not_called()
    chain.attest.assert_not_called()

    priced = _ready_chain()
    priced.base_fee_wei.return_value = 50 * 10**9
    settings = _settings(_anvil_key(1), max_fee_gwei=20, min_balance_wei=0)
    blocked = _submit(_worker(Store(), settings, priced), "NVDA", 1_700_000_061, now=60_001.0)
    assert blocked.reason == "attest_gas_fee_cap"
    assert blocked.tx_hash is None
    priced.attest.assert_not_called()

    with pytest.raises(SafetyRefusal) as exc:
        eip1559_fees_or_refuse(max_fee_gwei=20, base_fee_wei=50 * 10**9)
    assert exc.value.code == "attest_gas_fee_cap"
    max_fee, priority = eip1559_fees_or_refuse(max_fee_gwei=20, base_fee_wei=1_000_000_000)
    ceiling = 20 * 1_000_000_000
    assert max_fee <= ceiling
    assert priority <= max_fee
    assert priority <= 1_000_000_000
    assert max_fee >= 1_000_000_000 + priority
    with pytest.raises(SafetyRefusal):
        eip1559_fees_or_refuse(max_fee_gwei=HARD_MAX_FEE_GWEI + 1, base_fee_wei=1)


def test_fixtures_plus_enabled_refuses(
    fixture_scorer: TransparencyScorer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _anvil_key(2)
    monkeypatch.setenv("RWA_ATTESTER_PRIVATE_KEY", key)
    monkeypatch.setenv("RWA_ATTESTATION_CONTRACT", "0x2F073a3628D498d92956e7eFE2b26633eDa75b00")
    monkeypatch.setenv("BASE_SEPOLIA_RPC_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("RWA_ATTEST_ENABLED", "1")
    monkeypatch.setenv("RWA_USE_FIXTURES", "1")
    settings = AttesterSettings.from_env()
    assert settings.fixtures_blocked is True
    assert settings.enabled is False
    chain = _ready_chain()
    worker = AttestWorker(store=Store(), settings=settings, chain=chain, autostart=True)
    worker.kick()
    assert worker._thread is None
    result = _submit(worker, "NVDA", 1_700_000_070, now=70_000.0)
    assert result.reason == "fixtures_with_attester"
    assert result.tx_hash is None
    chain.attest.assert_not_called()
    chain.chain_id.assert_not_called()

    client, store = _client(settings, chain, fixture_scorer)
    raw = store.create_key(name="paid", tier="paid")
    resp = client.post("/v1/attest/NVDA", headers={"X-API-Key": raw})
    assert resp.status_code == 503
    assert resp.json()["error"] == "fixtures_with_attester"
    chain.attest.assert_not_called()


def test_derived_key_never_leaks(
    fixture_scorer: TransparencyScorer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_por(monkeypatch)
    key = _anvil_key(3)
    assert key.startswith("0x")
    assert len(key) == 66
    settings = _settings(key, rpc_url="http://127.0.0.1:9")
    with pytest.raises(TypeError):
        vars(settings)
    assert not hasattr(settings, "_private_key")
    assert settings.private_key == key
    assert key not in repr(settings)
    assert key not in str(settings)
    closed = settings._get_key.__closure__
    assert closed is not None
    for cell in closed:
        assert key not in repr(cell.cell_contents)
        assert key not in str(cell.cell_contents)
    chain = _ready_chain()
    chain.attest.side_effect = RuntimeError(f"rpc exploded while using {key}")
    worker = _worker(Store(), settings, chain)
    result = _submit(worker, "NVDA", 1_700_000_080, now=80_000.0)
    assert result.status == "failed"
    assert key not in (result.error or "")
    assert key not in (result.message or "")
    client, _store = _client(settings, chain, fixture_scorer)
    blob = client.get("/health").text + repr(settings) + str(result.error)
    assert key not in blob
    assert key[2:] not in blob


def test_constructor_defaults_attest_enabled_off() -> None:
    key = _anvil_key(4)
    settings = AttesterSettings(
        private_key=key,
        contract="0x2F073a3628D498d92956e7eFE2b26633eDa75b00",
        rpc_url="http://127.0.0.1:9",
    )
    assert settings.attest_enabled is False
    assert settings.configured is True
    assert settings.enabled is False


def test_bad_gas_ceiling_refuses_without_defaulting() -> None:
    key = _anvil_key(5)
    for bad in (float("nan"), float("inf"), float("-inf"), 0, -1):
        chain = _ready_chain()
        settings = _settings(key, max_fee_gwei=bad, min_balance_wei=0)
        assert settings.max_fee_gwei != DEFAULT_MAX_FEE_GWEI
        if bad == 0 or bad == -1:
            assert settings.max_fee_gwei == float(bad)
        result = _submit(_worker(Store(), settings, chain), "NVDA", 1_700_000_090, now=90_000.0)
        assert result.reason == "attest_gas_fee_cap"
        assert result.tx_hash is None
        chain.chain_id.assert_not_called()
        chain.attest.assert_not_called()
    with pytest.raises(SafetyRefusal) as exc:
        eip1559_fees_or_refuse(max_fee_gwei=float("nan"), base_fee_wei=1)
    assert exc.value.code == "attest_gas_fee_cap"


def test_from_env_keeps_non_finite_gas_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RWA_ATTESTER_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("RWA_ATTEST_MAX_FEE_GWEI", "nan")
    assert math.isnan(AttesterSettings.from_env().max_fee_gwei)
    monkeypatch.setenv("RWA_ATTEST_MAX_FEE_GWEI", "-3")
    assert AttesterSettings.from_env().max_fee_gwei == -3.0


def test_balance_read_failure_is_not_low_balance(
    fixture_scorer: TransparencyScorer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_por(monkeypatch)
    chain = _ready_chain()
    chain.balance_wei.side_effect = RuntimeError("rpc down")
    settings = _settings(_anvil_key(6), min_balance_wei=10**15)
    worker = _worker(Store(), settings, chain)
    result = _submit(worker, "NVDA", 1_700_000_091, now=91_000.0)
    assert result.reason == "attester_balance_unavailable"
    assert result.reason != "attester_low_balance"
    assert result.tx_hash is None
    chain.attest.assert_not_called()
    chain.verify.assert_not_called()
    body = safety_status(worker, settings)
    assert body["attester_balance"] == "unavailable"
    assert body["attester_balance_unavailable"] is True
    assert body["attester_low_balance"] is False
    assert body["attester_balance_wei"] is None

    client, _store = _client(settings, chain, fixture_scorer)
    health = client.get("/health").json()
    assert health["attester_balance"] == "unavailable"
    assert health["attester_balance_unavailable"] is True
    assert health["attester_low_balance"] is False
    assert health["attester_balance_wei"] is None


def test_startup_balance_read_is_cached_and_send_is_fresh() -> None:
    chain = _ready_chain()
    chain.balance_wei.return_value = 10**18
    settings = _settings(_anvil_key(7), min_balance_wei=10**15)
    worker = AttestWorker(store=Store(), settings=settings, chain=chain, autostart=True)
    worker.kick()
    try:
        assert worker._thread is not None
        first = safety_status(worker, settings)
        assert first["attester_balance"] == "ok"
        assert first["attester_balance_wei"] == 10**18
        assert first["attester_low_balance"] is False
        cached_calls = chain.balance_wei.call_count
        assert cached_calls >= 1
        again = safety_status(worker, settings)
        assert again["attester_balance_wei"] == 10**18
        assert chain.balance_wei.call_count == cached_calls
        worker.balance_read_at = time.time() - BALANCE_HEALTH_CACHE_SECONDS - 1
        safety_status(worker, settings)
        assert chain.balance_wei.call_count == cached_calls + 1
        before_send = chain.balance_wei.call_count
        result = _submit(worker, "NVDA", 1_700_000_092, now=92_000.0)
        assert result.status == "confirmed"
        assert chain.balance_wei.call_count > before_send
    finally:
        worker.stop()
        if worker._thread is not None:
            worker._thread.join(timeout=2)


def test_startup_balance_failure_does_not_crash() -> None:
    chain = _ready_chain()
    chain.balance_wei.side_effect = RuntimeError("boot rpc down")
    settings = _settings(_anvil_key(8), min_balance_wei=10**15)
    worker = AttestWorker(store=Store(), settings=settings, chain=chain, autostart=True)
    worker.kick()
    try:
        body = safety_status(worker, settings)
        assert body["attester_balance"] == "unavailable"
        assert body["attester_balance_unavailable"] is True
        assert body["attester_low_balance"] is False
    finally:
        worker.stop()
        if worker._thread is not None:
            worker._thread.join(timeout=2)


def test_zero_per_key_cap_still_refuses() -> None:
    chain = _ready_chain()
    settings = _settings(
        _anvil_key(9),
        max_per_hour=100,
        max_per_day=100,
        min_interval_seconds=0,
        max_per_key_per_day=0,
        min_balance_wei=0,
    )
    worker = _worker(Store(), settings, chain)
    blocked = _submit(worker, "NVDA", 1_700_000_093, now=93_000.0, api_key_id=3)
    assert blocked.reason == "attest_key_daily_cap"
    assert blocked.tx_hash is None
    chain.attest.assert_not_called()
    chain.chain_id.assert_not_called()
