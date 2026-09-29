"""Same store contract on SQLite always, and on Postgres when TEST_DATABASE_URL is set."""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from rwa_score.api.app import create_app
from rwa_score.api.attest import canonical_bytes, hash_canonical
from rwa_score.api.auto_attest import PINNED_ATTESTATION_CONTRACT, AttesterSettings
from rwa_score.api.settings import ApiSettings, normalize_postgres_url
from rwa_score.api.store import CLAIM_LEASE_SECONDS, CLAIM_SQL_POSTGRES, Store


def _exercise(store: Store) -> None:
    raw_key = store.create_key(name="contract", tier="paid")
    key = store.lookup_key(raw_key)
    assert key is not None
    store.set_last_band(key.id, "NVDA", "GREEN", 80.0)
    assert store.get_last_band(key.id, "NVDA") == ("GREEN", 80.0)
    assert store.add_watchlist(key.id, ["nvda", "NVDA"]) == ["NVDA"]
    hook = store.add_webhook(key.id, url="https://example.test/h", secret="s", ticker="nvda")
    assert hook.ticker == "NVDA"
    assert store.active_webhooks(key.id)[0].id == hook.id
    store.record_history(
        ticker="nvda",
        score=80.0,
        band="GREEN",
        payload_json='{"ticker":"NVDA"}',
        payload_hash="0x" + "ab" * 32,
        key_id=key.id,
    )
    history = store.get_history("NVDA", key_id=key.id)
    assert history[0]["payload_hash"] == "0x" + "ab" * 32
    assert history[0]["band"] == "GREEN"
    stored_history = store._execute(
        "SELECT payload_json FROM score_history WHERE payload_hash = ?",
        ("0x" + "ab" * 32,),
    ).fetchone()
    assert stored_history["payload_json"] == '{"ticker":"NVDA"}'

    first = canonical_bytes({"ticker": "NVDA", "n": 1})
    second = canonical_bytes({"ticker": "NVDA", "n": 2})
    inputs = canonical_bytes({"cmc": {"ticker": "NVDA"}})
    digest = store.save_attested_payload(ticker="nvda", canonical=first, inputs=inputs)
    assert digest == hash_canonical(first)
    loaded = store.get_attested_payload(digest)
    assert loaded is not None
    assert loaded["canonical"] == first
    assert loaded["inputs"] == inputs
    assert store.save_attested_payload(ticker="NVDA", canonical=first) == digest

    job = store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=10)
    again = store.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=10)
    assert again["id"] == job["id"]

    digest_b = store.save_attested_payload(ticker="NVDA", canonical=second)
    job_b = store.enqueue_attest_job(score_hash=digest_b, ticker="NVDA", claimed_at=11)
    claimed = store.claim_next_attest_job(now=time.time() + 1)
    assert claimed is not None
    assert claimed["id"] == job["id"]
    assert claimed["attempts"] == 1
    store.finish_attest_job(
        claimed["id"],
        status="confirmed",
        tx_hash="0x" + "cd" * 32,
        attested_at=1_700_000_000,
    )
    nxt = store.claim_next_attest_job(now=time.time() + 1)
    assert nxt is not None
    assert nxt["id"] == job_b["id"]
    saved = store.get_attested_payload(digest)
    assert saved is not None
    assert saved["tx_hash"] == "0x" + "cd" * 32
    assert saved["attested_at"] == 1_700_000_000


def test_file_store_contract(tmp_path: Path) -> None:
    store = Store(tmp_path / "contract.sqlite")
    try:
        assert store.backend == "sqlite"
        _exercise(store)
    finally:
        store.close()


def test_postgres_store_contract() -> None:
    url = (os.getenv("TEST_DATABASE_URL") or "").strip()
    if not url:
        pytest.skip("TEST_DATABASE_URL is unset; Postgres store contract skipped")
    schema = "t_" + uuid.uuid4().hex[:16]
    store = Store(database_url=url, schema=schema)
    try:
        assert store.backend == "postgres"
        _exercise(store)
        _assert_skip_locked(url, schema)
    finally:
        store.close()
        _drop_schema(url, schema)


def _assert_skip_locked(url: str, schema: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    seeder = Store(database_url=url, schema=schema)
    try:
        seeder.enqueue_attest_job(score_hash="0x" + "ee" * 32, ticker="LOCK", claimed_at=1)
    finally:
        seeder.close()
    held = psycopg.connect(normalize_postgres_url(url), row_factory=dict_row)
    waiter = Store(database_url=url, schema=schema)
    try:
        held.execute(f"SET search_path TO {schema}")
        locked = held.execute(CLAIM_SQL_POSTGRES.replace("?", "%s"), (time.time() + 5,)).fetchone()
        assert locked is not None
        skipped = waiter.claim_next_attest_job(now=time.time() + 5)
        assert skipped is None
    finally:
        held.rollback()
        held.close()
        waiter.close()


def test_second_connection_cannot_claim_during_lease(tmp_path: Path) -> None:
    path = tmp_path / "lease.sqlite"
    first = Store(path)
    second = Store(path)
    try:
        first.enqueue_attest_job(score_hash="0x" + "ab" * 32, ticker="NVDA", claimed_at=1)
        now = 1_700_000_000.0
        claimed = first.claim_next_attest_job(now=now)
        assert claimed is not None
        assert second.claim_next_attest_job(now=now + 1) is None
        again = second.claim_next_attest_job(now=now + CLAIM_LEASE_SECONDS + 1)
        assert again is not None
        assert again["id"] == claimed["id"]
        assert again["attempts"] == 2
    finally:
        first.close()
        second.close()


def _postgres_url() -> str:
    url = (os.getenv("TEST_DATABASE_URL") or "").strip()
    if not url:
        pytest.skip("TEST_DATABASE_URL is unset; Postgres store contract skipped")
    return url


def test_postgres_fk_race_idle_reconnect_and_health(fixture_scorer) -> None:
    url = _postgres_url()
    schema = "t_" + uuid.uuid4().hex[:16]
    store = Store(database_url=url, schema=schema)
    try:
        import psycopg
        from psycopg.rows import dict_row

        with pytest.raises(psycopg.Error):
            store.set_last_band(10**9, "NVDA", "GREEN", 1.0)
        raw_key = store.create_key(name="after-fk", tier="paid")
        key = store.lookup_key(raw_key)
        assert key is not None
        store.set_last_band(key.id, "NVDA", "GREEN", 80.0)
        assert store.get_last_band(key.id, "NVDA") == ("GREEN", 80.0)

        raw = canonical_bytes({"ticker": "NVDA", "n": 7})
        digest = hash_canonical(raw)
        results: list[str] = []
        errors: list[BaseException] = []

        def _save(database_url: str) -> None:
            peer = Store(database_url=database_url, schema=schema)
            try:
                results.append(peer.save_attested_payload(ticker="NVDA", canonical=raw))
            except BaseException as exc:  # noqa: BLE001 — the test records the race
                errors.append(exc)
            finally:
                peer.close()

        threads = [threading.Thread(target=_save, args=(url,)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert results == [digest, digest, digest, digest]
        count = store._execute(
            "SELECT COUNT(*) AS n FROM attested_payloads WHERE score_hash = ?",
            (digest,),
        ).fetchone()
        assert int(count["n"]) == 1
        assert store.save_attested_payload(ticker="NVDA", canonical=raw) == digest

        other = canonical_bytes({"ticker": "NVDA", "n": 8})
        store._execute(
            "UPDATE attested_payloads SET canonical_json = ? WHERE score_hash = ?",
            (other, digest),
        )
        with pytest.raises(ValueError, match="refusing to replace"):
            store.save_attested_payload(ticker="NVDA", canonical=raw)
        store._execute(
            "UPDATE attested_payloads SET canonical_json = ? WHERE score_hash = ?",
            (raw, digest),
        )

        pid = store.backend_pid()
        assert pid is not None
        store.get_attested_payload(digest)
        store.get_history("NVDA", key_id=key.id)
        watcher = psycopg.connect(normalize_postgres_url(url), autocommit=True, row_factory=dict_row)
        try:
            state = watcher.execute(
                "SELECT state FROM pg_stat_activity WHERE pid = %s",
                (pid,),
            ).fetchone()
            assert state is not None
            assert state["state"] == "idle"
            watcher.execute("SELECT pg_terminate_backend(%s)", (pid,))
        finally:
            watcher.close()
        assert store.ping() is True
        assert store.backend_pid() != pid
        assert store.get_attested_payload(digest) is not None

        app = create_app(
            settings=ApiSettings(database_url=url),
            store=store,
            scorer=fixture_scorer,
            attester=AttesterSettings(),
            start_worker=False,
        )
        client = TestClient(app)
        up = client.get("/health")
        assert up.status_code == 200
        assert up.json()["database"] == "ok"
        assert up.json()["store"] == "postgres"
        live = store.backend_pid()
        killer = psycopg.connect(normalize_postgres_url(url), autocommit=True)
        try:
            killer.execute("SELECT pg_terminate_backend(%s)", (live,))
        finally:
            killer.close()
        saved = store.database_url
        store.database_url = "postgresql://postgres:postgres@127.0.0.1:1/rat"
        down = client.get("/health")
        assert down.status_code == 503
        assert down.json()["database"] == "down"
        store.database_url = saved
        assert client.get("/health").status_code == 200
    finally:
        store.close()
        _drop_schema(url, schema)


def test_postgres_worker_smoke(fixture_scorer) -> None:
    url = _postgres_url()
    schema = "t_" + uuid.uuid4().hex[:16]
    store = Store(database_url=url, schema=schema)
    try:
        settings = AttesterSettings(
            private_key="0x" + "77" * 32,
            contract=PINNED_ATTESTATION_CONTRACT,
            rpc_url="http://127.0.0.1:9",
            min_interval_seconds=0,
            daily_tx_cap=48,
            hourly_tx_cap=24,
            min_balance_wei=0,
        )
        chain = Mock()
        chain.chain_id.return_value = 84532
        chain.verify.return_value = (False, 0, "0x" + "00" * 20)
        chain.fee_wei.return_value = 0
        chain.attest.return_value = "0x" + "cd" * 32
        app = create_app(
            settings=ApiSettings(database_url=url),
            store=store,
            scorer=fixture_scorer,
            attester=settings,
            chain=chain,
            start_worker=False,
        )
        client = TestClient(app)
        raw = store.create_key(name="paid", tier="paid")
        resp = client.get("/v1/attest/NVDA", headers={"X-API-Key": raw})
        assert resp.status_code == 200
        assert app.state.worker.process_once() is True
        job = store.latest_attest_job(resp.json()["score_hash"])
        assert job is not None
        assert job["status"] == "confirmed"
        assert job["tx_hash"] == "0x" + "cd" * 32
    finally:
        store.close()
        _drop_schema(url, schema)


def _drop_schema(url: str, schema: str) -> None:
    import psycopg

    conn = psycopg.connect(normalize_postgres_url(url))
    try:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.commit()
    finally:
        conn.close()
