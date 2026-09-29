"""Pending-tx map, rate counters, and key ring. No score cache."""

from __future__ import annotations

import time

from rwa_score.api.store import Store


def test_keys_rate_counters_and_inflight_are_the_only_state() -> None:
    store = Store()
    raw = store.create_key(name="paid", tier="paid")
    key = store.lookup_key(raw)
    assert key is not None
    assert key.is_paid
    ok, used = store.try_consume(key.id, path="/v1/score/NVDA", limit=2, window_seconds=10)
    assert ok is True
    assert used == 1
    assert store.count_usage(key.id, since=time.time() - 10) == 1
    store.note_broadcast(
        tx_hash="0x" + "ab" * 32,
        nonce=4,
        score_hash="0x" + "cd" * 32,
        ticker="nvda",
        claimed_at=10,
        now=1_000.0,
    )
    row = store.get_inflight("0x" + "ab" * 32)
    assert row is not None
    assert row["nonce"] == 4
    assert row["ticker"] == "NVDA"
    assert store.inflight_for_hash("0x" + "cd" * 32)["tx_hash"] == "0x" + "ab" * 32
    assert store.queue_depth() == 1
    store.drop_inflight("0x" + "ab" * 32)
    assert store.get_inflight("0x" + "ab" * 32) is None
    assert store.queue_depth() == 0
    assert not hasattr(store, "save_attested_payload")
    assert not hasattr(store, "record_history")
    store.close()


def test_free_window_stops_at_the_limit() -> None:
    store = Store()
    raw = store.create_key(name="free", tier="free")
    key = store.lookup_key(raw)
    assert key is not None
    assert store.try_consume(key.id, path="/v1/score/NVDA", limit=1, window_seconds=50)[0] is True
    ok, used = store.try_consume(key.id, path="/v1/score/NVDA", limit=1, window_seconds=50)
    assert ok is False
    assert used == 1
