"""In-memory store: same job contract, a hard cap, and no database."""

from __future__ import annotations

import time

import pytest

from rwa_score.api.attest import canonical_bytes, hash_canonical
from rwa_score.api.store import CLAIM_LEASE_SECONDS, HARD_STORE_CAP, Store, resolve_store_cap


def test_memory_store_contract() -> None:
    store = Store()
    assert store.backend == "memory"
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
    store._payloads[digest]["canonical"] = b"not-the-stored-bytes"
    with pytest.raises(ValueError):
        store.save_attested_payload(ticker="NVDA", canonical=first)
    store._payloads[digest]["canonical"] = first

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
    store.close()


def test_claim_lease_blocks_a_second_take() -> None:
    store = Store()
    store.enqueue_attest_job(score_hash="0x" + "ee" * 32, ticker="LOCK", claimed_at=1)
    now = 1_000.0
    first = store.claim_next_attest_job(now=now)
    assert first is not None
    assert store.claim_next_attest_job(now=now + 1) is None
    again = store.claim_next_attest_job(now=now + CLAIM_LEASE_SECONDS + 1)
    assert again is not None
    assert again["id"] == first["id"]
    assert again["attempts"] == 2


def test_history_is_tenant_scoped() -> None:
    store = Store()
    raw_a = store.create_key(name="a", tier="paid")
    raw_b = store.create_key(name="b", tier="paid")
    a = store.lookup_key(raw_a)
    b = store.lookup_key(raw_b)
    assert a is not None and b is not None
    store.record_history(
        ticker="NVDA",
        score=80.0,
        band="GREEN",
        payload_json="{}",
        payload_hash="aaa",
        key_id=a.id,
    )
    store.record_history(
        ticker="NVDA",
        score=20.0,
        band="RED",
        payload_json="{}",
        payload_hash="bbb",
        key_id=b.id,
    )
    only_a = store.get_history("NVDA", key_id=a.id)
    only_b = store.get_history("NVDA", key_id=b.id)
    assert [row["payload_hash"] for row in only_a] == ["aaa"]
    assert [row["payload_hash"] for row in only_b] == ["bbb"]


def test_cap_drops_oldest_history_and_keeps_inflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RWA_STORE_MAX_ENTRIES", "999999")
    assert resolve_store_cap(None) == HARD_STORE_CAP
    monkeypatch.setenv("RWA_STORE_MAX_ENTRIES", "nope")
    assert resolve_store_cap(None) == 1000

    store = Store(capacity=2)
    assert store.capacity == 2
    for n in range(4):
        store.record_history(
            ticker="NVDA",
            score=float(n),
            band="GREEN",
            payload_json="{}",
            payload_hash=f"h{n}",
        )
    hashes = [row["payload_hash"] for row in store.get_history("NVDA", limit=10)]
    assert hashes == ["h3", "h2"]

    pinned = Store(capacity=1)
    raw = canonical_bytes({"ticker": "NVDA", "n": 9})
    digest = pinned.save_attested_payload(ticker="NVDA", canonical=raw)
    job = pinned.enqueue_attest_job(score_hash=digest, ticker="NVDA", claimed_at=1)
    pinned.record_history(
        ticker="NVDA", score=1, band="GREEN", payload_json="{}", payload_hash="extra"
    )
    assert pinned.latest_attest_job(digest)["id"] == job["id"]
    assert pinned.get_attested_payload(digest) is not None
    assert pinned.queue_depth() == 1
