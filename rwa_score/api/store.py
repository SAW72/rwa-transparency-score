"""Keys, history, attested payloads, and the attest queue, in memory only.

A restart drops every row. The collections that grow (history, payloads,
jobs, usage, deliveries) are capped. ``RWA_STORE_MAX_ENTRIES`` sets the cap
and cannot raise it past ``HARD_STORE_CAP``. In-flight attest jobs are not
evicted.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Hold a claimed row past the receipt wait so a second thread cannot send it too.
CLAIM_LEASE_SECONDS = 90.0
DEFAULT_STORE_CAP = 1000
HARD_STORE_CAP = 10_000


def resolve_store_cap(requested: int | None = None) -> int:
    """Cap from the argument, else ``RWA_STORE_MAX_ENTRIES``, else the default.

    Values below 1 become 1. Values above ``HARD_STORE_CAP`` are clamped.
    """
    if requested is None:
        raw = os.getenv("RWA_STORE_MAX_ENTRIES", "").strip()
        if raw:
            try:
                requested = int(raw)
            except ValueError:
                requested = DEFAULT_STORE_CAP
        else:
            requested = DEFAULT_STORE_CAP
    if requested < 1:
        requested = 1
    return min(int(requested), HARD_STORE_CAP)


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


@dataclass(frozen=True)
class Webhook:
    id: int
    key_id: int
    url: str
    secret: str
    ticker: str | None
    trigger: str
    created_at: str
    active: bool


def _copy_job(job: dict[str, Any]) -> dict[str, Any]:
    out = dict(job)
    out["known_tx_hashes"] = list(job["known_tx_hashes"])
    return out


def _copy_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "score_hash": row["score_hash"],
        "ticker": row["ticker"],
        "canonical": bytes(row["canonical"]),
        "inputs": None if row["inputs"] is None else bytes(row["inputs"]),
        "stored_at": row["stored_at"],
        "tx_hash": row["tx_hash"],
        "attested_at": row["attested_at"],
    }


class Store:
    """Process-local store. ``close`` is a no-op. Nothing is written to disk."""

    backend = "memory"

    def __init__(self, capacity: int | None = None) -> None:
        self.capacity = resolve_store_cap(capacity)
        self._lock = threading.Lock()
        self._next_id = 1
        self._keys_by_hash: dict[str, ApiKey] = {}
        self._keys_by_id: dict[int, ApiKey] = {}
        self._usage: list[tuple[int, float, str]] = []
        self._history: list[dict[str, Any]] = []
        self._bands: dict[tuple[int, str], tuple[str, float]] = {}
        self._watch: dict[int, list[str]] = {}
        self._hooks: dict[int, Webhook] = {}
        self._deliveries: list[dict[str, Any]] = []
        self._payloads: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._jobs: dict[int, dict[str, Any]] = {}

    def close(self) -> None:
        return None

    def queue_depth(self) -> int:
        with self._lock:
            return sum(
                1
                for job in self._jobs.values()
                if job["status"] in {"pending", "broadcast_pending"}
            )

    def _alloc_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def _bulk_count(self) -> int:
        return (
            len(self._history)
            + len(self._payloads)
            + len(self._jobs)
            + len(self._usage)
            + len(self._deliveries)
        )

    def _referenced_hashes(self) -> set[str]:
        return {job["score_hash"] for job in self._jobs.values()}

    def _evict_one(self, protect: set[str] | None = None) -> bool:
        if self._history:
            self._history.pop(0)
            return True
        if self._deliveries:
            self._deliveries.pop(0)
            return True
        if self._usage:
            self._usage.pop(0)
            return True
        terminal = [
            job
            for job in self._jobs.values()
            if job["status"] in {"confirmed", "failed"}
        ]
        if terminal:
            oldest = min(terminal, key=lambda job: int(job["id"]))
            self._jobs.pop(int(oldest["id"]))
            return True
        keep = self._referenced_hashes() | (protect or set())
        for digest in list(self._payloads.keys()):
            if digest not in keep:
                self._payloads.pop(digest)
                return True
        return False

    def _make_room(self, incoming: int = 1, protect: set[str] | None = None) -> None:
        while self._bulk_count() + incoming > self.capacity:
            if not self._evict_one(protect):
                return

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
            self._make_room(1)
            self._usage.append((key_id, now, path))
            self._usage = [row for row in self._usage if row[1] >= cutoff]
            return True, used + 1

    def record_usage(self, key_id: int, path: str, *, ts: float | None = None) -> None:
        stamped = time.time() if ts is None else ts
        cutoff = stamped - (86_400.0 * 2)
        with self._lock:
            self._make_room(1)
            self._usage.append((key_id, stamped, path))
            self._usage = [row for row in self._usage if row[1] >= cutoff]

    def record_history(
        self,
        *,
        ticker: str,
        score: float,
        band: str,
        payload_json: str,
        payload_hash: str,
        scored_at: float | None = None,
        key_id: int | None = None,
    ) -> None:
        stamped = time.time() if scored_at is None else scored_at
        row = {
            "ticker": ticker.upper(),
            "scored_at": stamped,
            "score": score,
            "band": band,
            "payload_json": payload_json,
            "payload_hash": payload_hash,
            "key_id": key_id,
        }
        with self._lock:
            self._make_room(1)
            self._history.append(row)

    def get_history(
        self,
        ticker: str,
        *,
        limit: int = 30,
        key_id: int | None = None,
    ) -> list[dict[str, Any]]:
        symbol = ticker.upper()
        with self._lock:
            rows = [
                row
                for row in self._history
                if row["ticker"] == symbol and (key_id is None or row["key_id"] == key_id)
            ]
        rows.sort(key=lambda row: float(row["scored_at"]), reverse=True)
        return [
            {
                "ticker": row["ticker"],
                "scored_at": row["scored_at"],
                "score": row["score"],
                "band": row["band"],
                "payload_hash": row["payload_hash"],
                "key_id": row["key_id"],
            }
            for row in rows[:limit]
        ]

    def get_last_band(self, key_id: int, ticker: str) -> tuple[str, float] | None:
        return self._bands.get((key_id, ticker.upper()))

    def set_last_band(self, key_id: int, ticker: str, band: str, score: float) -> None:
        self._bands[(key_id, ticker.upper())] = (band, float(score))

    def add_watchlist(self, key_id: int, tickers: Iterable[str]) -> list[str]:
        symbols = [t.strip().upper() for t in tickers if t and t.strip()]
        with self._lock:
            current = list(self._watch.get(key_id, []))
            for symbol in symbols:
                if symbol not in current:
                    current.append(symbol)
            current.sort()
            self._watch[key_id] = current
            return list(current)

    def set_watchlist(self, key_id: int, tickers: Iterable[str]) -> list[str]:
        symbols = [t.strip().upper() for t in tickers if t and t.strip()]
        with self._lock:
            self._watch[key_id] = list(symbols)
            return list(symbols)

    def remove_watchlist(self, key_id: int, ticker: str) -> None:
        symbol = ticker.upper()
        with self._lock:
            self._watch[key_id] = [item for item in self._watch.get(key_id, []) if item != symbol]

    def get_watchlist(self, key_id: int) -> list[str]:
        return sorted(self._watch.get(key_id, []))

    def all_watchlist_tickers(self) -> list[str]:
        found: set[str] = set()
        for symbols in self._watch.values():
            found.update(symbols)
        return sorted(found)

    def watchlist_entries(self) -> list[tuple[int, str]]:
        rows: list[tuple[int, str]] = []
        for key_id in sorted(self._watch):
            for ticker in self._watch[key_id]:
                rows.append((key_id, ticker))
        return rows

    def add_webhook(
        self,
        key_id: int,
        *,
        url: str,
        secret: str,
        ticker: str | None = None,
        trigger: str = "band_cross",
    ) -> Webhook:
        if trigger not in {"band_cross", "below_orange"}:
            raise ValueError(f"unknown trigger: {trigger}")
        symbol = ticker.strip().upper() if ticker else None
        with self._lock:
            hook = Webhook(
                id=self._alloc_id(),
                key_id=key_id,
                url=url,
                secret=secret,
                ticker=symbol,
                trigger=trigger,
                created_at=_iso(),
                active=True,
            )
            self._hooks[hook.id] = hook
            return hook

    def list_webhooks(self, key_id: int) -> list[Webhook]:
        return [hook for _id, hook in sorted(self._hooks.items()) if hook.key_id == key_id]

    def get_webhook(self, webhook_id: int) -> Webhook | None:
        return self._hooks.get(webhook_id)

    def deactivate_webhook(self, key_id: int, webhook_id: int) -> bool:
        with self._lock:
            hook = self._hooks.get(webhook_id)
            if hook is None or hook.key_id != key_id:
                return False
            self._hooks[webhook_id] = Webhook(
                id=hook.id,
                key_id=hook.key_id,
                url=hook.url,
                secret=hook.secret,
                ticker=hook.ticker,
                trigger=hook.trigger,
                created_at=hook.created_at,
                active=False,
            )
            return True

    def active_webhooks(self, key_id: int) -> list[Webhook]:
        return [
            hook
            for _id, hook in sorted(self._hooks.items())
            if hook.key_id == key_id and hook.active
        ]

    def record_delivery(
        self,
        *,
        webhook_id: int,
        ticker: str,
        event: str,
        status_code: int,
        ok: bool,
    ) -> None:
        with self._lock:
            self._make_room(1)
            self._deliveries.append(
                {
                    "webhook_id": webhook_id,
                    "ticker": ticker.upper(),
                    "event": event,
                    "status_code": status_code,
                    "delivered_at": time.time(),
                    "ok": int(ok),
                }
            )

    def save_attested_payload(
        self,
        *,
        ticker: str,
        canonical: bytes,
        inputs: bytes | None = None,
    ) -> str:
        """Store the exact canonical JSON bytes, keyed by their SHA-256.

        Same hash is idempotent. A different byte string under that hash is
        refused. A later save may fill ``inputs`` when the existing row has
        none. The bytes are dropped when this process exits.
        """
        from .attest import hash_canonical

        if not isinstance(canonical, (bytes, bytearray)):
            raise TypeError("canonical payload must be bytes")
        raw = bytes(canonical)
        inputs_raw = None if inputs is None else bytes(inputs)
        digest = hash_canonical(raw)
        symbol = ticker.strip().upper()
        with self._lock:
            existing = self._payloads.get(digest)
            if existing is not None:
                self._keep_existing_payload(existing, raw, inputs_raw)
                self._payloads.move_to_end(digest)
                return digest
            self._make_room(1)
            self._payloads[digest] = {
                "score_hash": digest,
                "ticker": symbol,
                "canonical": raw,
                "inputs": inputs_raw,
                "stored_at": _iso(),
                "tx_hash": None,
                "attested_at": None,
            }
        return digest

    def _keep_existing_payload(
        self,
        existing: dict[str, Any],
        raw: bytes,
        inputs_raw: bytes | None,
    ) -> None:
        if bytes(existing["canonical"]) != raw:
            raise ValueError("refusing to replace attested payload bytes for an existing hash")
        if inputs_raw is None:
            return
        prior = existing["inputs"]
        if prior is None:
            existing["inputs"] = inputs_raw
            return
        if bytes(prior) != inputs_raw:
            raise ValueError("refusing to replace attested inputs for an existing hash")

    def get_attested_payload(self, score_hash: str) -> dict[str, Any] | None:
        digest = score_hash.strip()
        with self._lock:
            row = self._payloads.get(digest)
            if row is None:
                return None
            self._payloads.move_to_end(digest)
            return _copy_payload(row)

    def latest_attested_payload(self, ticker: str) -> dict[str, Any] | None:
        symbol = ticker.strip().upper()
        with self._lock:
            matches = [row for row in self._payloads.values() if row["ticker"] == symbol]
        if not matches:
            return None
        matches.sort(key=lambda row: (str(row["stored_at"]), str(row["score_hash"])))
        return _copy_payload(matches[-1])

    def enqueue_attest_job(
        self,
        *,
        score_hash: str,
        ticker: str,
        claimed_at: int,
        force: bool = False,
    ) -> dict[str, Any]:
        """Queue one send. A pending, broadcast, or confirmed job for this hash is reused.

        ``force`` inserts another pending row so a rerun can hit
        ``AlreadyAttested`` / the verify pre-check. The API does not set it.
        """
        digest = score_hash.strip()
        symbol = ticker.strip().upper()
        now = _iso()
        with self._lock:
            if not force:
                existing = [
                    job
                    for job in self._jobs.values()
                    if job["score_hash"] == digest
                    and job["status"] in {"pending", "broadcast_pending", "confirmed"}
                ]
                if existing:
                    newest = max(existing, key=lambda job: int(job["id"]))
                    return _copy_job(newest)
            self._make_room(1, protect={digest})
            job = {
                "id": self._alloc_id(),
                "score_hash": digest,
                "ticker": symbol,
                "claimed_at": int(claimed_at),
                "status": "pending",
                "tx_hash": None,
                "nonce": None,
                "known_tx_hashes": [],
                "broadcast_at": None,
                "attested_at": None,
                "attempts": 0,
                "next_attempt_at": 0.0,
                "last_error": None,
                "created_at": now,
                "updated_at": now,
            }
            self._jobs[job["id"]] = job
            return _copy_job(job)

    def latest_attest_job(self, score_hash: str) -> dict[str, Any] | None:
        digest = score_hash.strip()
        with self._lock:
            rows = [job for job in self._jobs.values() if job["score_hash"] == digest]
        if not rows:
            return None
        return _copy_job(max(rows, key=lambda job: int(job["id"])))

    def latest_attest_job_for_ticker(self, ticker: str) -> dict[str, Any] | None:
        symbol = ticker.strip().upper()
        with self._lock:
            rows = [job for job in self._jobs.values() if job["ticker"] == symbol]
        if not rows:
            return None
        return _copy_job(max(rows, key=lambda job: int(job["id"])))

    def count_attest_jobs_since(self, created_after: str) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if str(job["created_at"]) >= created_after)

    def note_submitted_tx(self, job_id: int, *, tx_hash: str, nonce: int) -> None:
        """Remember a broadcast hash and the nonce it used, before the receipt."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            known = list(job["known_tx_hashes"])
            if tx_hash not in known:
                known.append(tx_hash)
            job["tx_hash"] = tx_hash
            job["nonce"] = int(nonce)
            job["known_tx_hashes"] = known
            if job["broadcast_at"] is None:
                job["broadcast_at"] = time.time()
            job["updated_at"] = _iso()

    def claim_next_attest_job(self, *, now: float) -> dict[str, Any] | None:
        """Take the oldest due pending job that has not been broadcast."""
        stamped = _iso()
        lease_until = float(now) + CLAIM_LEASE_SECONDS
        with self._lock:
            due = [
                job
                for job in self._jobs.values()
                if job["status"] == "pending"
                and job["tx_hash"] is None
                and float(job["next_attempt_at"] or 0) <= float(now)
            ]
            if not due:
                return None
            job = min(due, key=lambda item: int(item["id"]))
            job["attempts"] = int(job["attempts"]) + 1
            job["next_attempt_at"] = lease_until
            job["updated_at"] = stamped
            return _copy_job(job)

    def claim_broadcast_job(self, *, now: float, lease_seconds: float = 15.0) -> dict[str, Any] | None:
        """Take one due broadcast so the reconciler can poll it. Does not send."""
        stamped = _iso()
        lease_until = float(now) + float(lease_seconds)
        with self._lock:
            due = [
                job
                for job in self._jobs.values()
                if (
                    job["status"] == "broadcast_pending"
                    or (job["status"] == "pending" and job["tx_hash"] is not None)
                )
                and float(job["next_attempt_at"] or 0) <= float(now)
            ]
            if not due:
                return None
            job = min(due, key=lambda item: int(item["id"]))
            job["next_attempt_at"] = lease_until
            job["updated_at"] = stamped
            return _copy_job(job)

    def list_inflight_broadcasts(self) -> list[dict[str, Any]]:
        """Jobs that already have a hash, including ones still inside a claim lease."""
        with self._lock:
            rows = [
                job
                for job in self._jobs.values()
                if job["status"] == "broadcast_pending"
                or (job["status"] == "pending" and job["tx_hash"] is not None)
            ]
        rows.sort(key=lambda job: int(job["id"]))
        return [_copy_job(job) for job in rows]

    def finish_attest_job(
        self,
        job_id: int,
        *,
        status: str,
        tx_hash: str | None = None,
        attested_at: int | None = None,
        error: str | None = None,
        next_attempt_at: float | None = None,
        nonce: int | None = None,
    ) -> None:
        if status not in {"pending", "broadcast_pending", "confirmed", "failed"}:
            raise ValueError(f"unknown job status: {status}")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if status == "confirmed":
                kept_tx = tx_hash
            else:
                kept_tx = job["tx_hash"] if tx_hash is None else tx_hash
            kept_nonce = job["nonce"] if nonce is None else int(nonce)
            job["status"] = status
            job["tx_hash"] = kept_tx
            job["nonce"] = kept_nonce
            job["attested_at"] = attested_at
            job["last_error"] = error
            job["next_attempt_at"] = 0.0 if next_attempt_at is None else float(next_attempt_at)
            job["updated_at"] = _iso()
            if status == "confirmed":
                payload = self._payloads.get(job["score_hash"])
                if payload is not None:
                    if kept_tx:
                        payload["tx_hash"] = kept_tx
                    if attested_at is not None:
                        payload["attested_at"] = int(attested_at)

    def recent_deliveries(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(reversed(self._deliveries))
        return [dict(row) for row in rows[:limit]]


def open_store() -> Store:
    """Open the in-memory store for this process. A restart starts empty."""
    store = Store()
    logger.info("store backend=memory cap=%s", store.capacity)
    return store
