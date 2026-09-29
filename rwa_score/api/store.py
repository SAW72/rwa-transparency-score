"""API keys, rate counters, and in-flight attestations. Nothing else.

There is no score store, payload cache, history, watchlist, or webhook
table, and nothing is written to disk. A restart drops the key ring
(recreate the paid key from ``RWA_API_BOOTSTRAP_KEY``), the rate-limit
counters, and the in-flight map.

The in-flight map is pending-transaction state: ``tx_hash`` and ``nonce``,
plus the subject needed to poll ``attested`` for that broadcast. It is not
a copy of the score. It is lost on restart. The startup hold plus the
``attested`` pre-check reduce the chance of a duplicate. A restart
mid-broadcast can still cost one duplicate transaction that reverts or
no-ops.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_api_key() -> str:
    return "rat_" + secrets.token_urlsafe(24)


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or time.time()))


@dataclass(frozen=True)
class ApiKey:
    id: int
    key_hash: str
    key_prefix: str
    name: str
    tier: str
    created_at: str
    revoked_at: str | None

    @property
    def is_paid(self) -> bool:
        return self.tier == "paid"

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


class Store:
    """Process-local auth, rate counters, and pending transactions.

    ``backend`` is ``"memory"`` only so logs can say where this state lives.
    ``close`` is a no-op. Scores are not stored.
    """

    backend = "memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_id = 1
        self._keys_by_hash: dict[str, ApiKey] = {}
        self._keys_by_id: dict[int, ApiKey] = {}
        self._usage: list[tuple[int, float, str]] = []
        # tx_hash -> pending broadcast. Dropped when the receipt lands or the
        # deadline says the broadcast is gone. Not a score cache.
        self._inflight: dict[str, dict[str, Any]] = {}
        self._dropped: dict[str, str] = {}

    def close(self) -> None:
        return None

    def queue_depth(self) -> int:
        with self._lock:
            return len(self._inflight)

    def _alloc_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def create_key(self, *, name: str, tier: str) -> str:
        if tier not in {"free", "paid"}:
            raise ValueError(f"unknown tier: {tier}")
        raw = new_api_key()
        self._insert_key(raw, name=name, tier=tier)
        return raw

    def ensure_key(self, raw: str, *, name: str, tier: str) -> ApiKey:
        """Create or revive a key with a caller-supplied secret (env bootstrap)."""
        if tier not in {"free", "paid"}:
            raise ValueError(f"unknown tier: {tier}")
        digest = hash_key(raw)
        now = _iso()
        with self._lock:
            existing = self._keys_by_hash.get(digest)
            if existing is not None:
                revived = ApiKey(
                    id=existing.id,
                    key_hash=existing.key_hash,
                    key_prefix=existing.key_prefix,
                    name=name,
                    tier=tier,
                    created_at=existing.created_at,
                    revoked_at=None,
                )
                self._keys_by_hash[digest] = revived
                self._keys_by_id[revived.id] = revived
                return revived
            rec = ApiKey(
                id=self._alloc_id(),
                key_hash=digest,
                key_prefix=raw[:12],
                name=name,
                tier=tier,
                created_at=now,
                revoked_at=None,
            )
            self._keys_by_hash[digest] = rec
            self._keys_by_id[rec.id] = rec
            return rec

    def _insert_key(self, raw: str, *, name: str, tier: str) -> None:
        digest = hash_key(raw)
        with self._lock:
            if digest in self._keys_by_hash:
                raise ValueError("api key already exists")
            rec = ApiKey(
                id=self._alloc_id(),
                key_hash=digest,
                key_prefix=raw[:12],
                name=name,
                tier=tier,
                created_at=_iso(),
                revoked_at=None,
            )
            self._keys_by_hash[digest] = rec
            self._keys_by_id[rec.id] = rec

    def lookup_key(self, raw: str) -> ApiKey | None:
        return self._keys_by_hash.get(hash_key(raw))

    def list_keys(self) -> list[ApiKey]:
        return [self._keys_by_id[i] for i in sorted(self._keys_by_id)]

    def revoke_key(self, *, prefix: str) -> int:
        changed = 0
        now = _iso()
        with self._lock:
            for rec in list(self._keys_by_id.values()):
                if rec.key_prefix == prefix and rec.revoked_at is None:
                    updated = ApiKey(
                        id=rec.id,
                        key_hash=rec.key_hash,
                        key_prefix=rec.key_prefix,
                        name=rec.name,
                        tier=rec.tier,
                        created_at=rec.created_at,
                        revoked_at=now,
                    )
                    self._keys_by_id[rec.id] = updated
                    self._keys_by_hash[rec.key_hash] = updated
                    changed += 1
        return changed

    def count_usage(self, key_id: int, *, since: float) -> int:
        with self._lock:
            return sum(1 for kid, ts, _path in self._usage if kid == key_id and ts > since)

    def try_consume(
        self,
        key_id: int,
        *,
        path: str,
        limit: int | None,
        window_seconds: float,
    ) -> tuple[bool, int]:
        """Atomically count-then-insert. ``limit=None`` means paid / unlimited."""
        now = time.time()
        since = now - window_seconds
        cutoff = now - (86_400.0 * 2)
        with self._lock:
            used = sum(1 for kid, ts, _path in self._usage if kid == key_id and ts > since)
            if limit is not None and used >= limit:
                return False, used
            self._usage.append((key_id, now, path))
            self._usage = [row for row in self._usage if row[1] >= cutoff]
            return True, used + 1

    def note_broadcast(
        self,
        *,
        tx_hash: str,
        nonce: int,
        score_hash: str,
        ticker: str,
        claimed_at: int,
        now: float | None = None,
    ) -> None:
        """Record a broadcast the moment it is submitted, before the receipt wait."""
        clock = time.time() if now is None else float(now)
        symbol = ticker.upper()
        row = {
            "tx_hash": tx_hash,
            "nonce": int(nonce),
            "score_hash": score_hash,
            "ticker": symbol,
            "claimed_at": int(claimed_at),
            "broadcast_at": clock,
            "known_tx_hashes": [tx_hash],
        }
        with self._lock:
            self._inflight[tx_hash] = row

    def get_inflight(self, tx_hash: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._inflight.get(tx_hash)
            return None if row is None else dict(row)

    def inflight_for_hash(self, score_hash: str) -> dict[str, Any] | None:
        with self._lock:
            for row in self._inflight.values():
                if row["score_hash"] == score_hash:
                    return dict(row)
        return None

    def list_inflight(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._inflight.values()]

    def next_inflight(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._inflight:
                return None
            first = next(iter(self._inflight.values()))
            return dict(first)

    def drop_inflight(self, tx_hash: str, *, reason: str | None = None) -> None:
        with self._lock:
            self._inflight.pop(tx_hash, None)
            if reason:
                self._dropped[tx_hash] = reason

    def dropped_reason(self, tx_hash: str) -> str | None:
        with self._lock:
            return self._dropped.get(tx_hash)


def open_store() -> Store:
    logger.info("store backend=memory pending-tx only; no score cache")
    return Store()
