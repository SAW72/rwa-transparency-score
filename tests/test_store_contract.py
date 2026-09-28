"""Same store contract on SQLite always, and on Postgres when TEST_DATABASE_URL is set."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

from rwa_score.api.attest import canonical_bytes, hash_canonical
from rwa_score.api.settings import normalize_postgres_url
from rwa_score.api.store import CLAIM_SQL_POSTGRES, Store


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
    digest = store.save_attested_payload(ticker="nvda", canonical=first)
    assert digest == hash_canonical(first)
    loaded = store.get_attested_payload(digest)
    assert loaded is not None
    assert loaded["canonical"] == first
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


def _drop_schema(url: str, schema: str) -> None:
    import psycopg

    conn = psycopg.connect(normalize_postgres_url(url))
    try:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.commit()
    finally:
        conn.close()
