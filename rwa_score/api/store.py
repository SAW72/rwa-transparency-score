"""SQLite persistence for keys, usage, history, watchlists, and webhooks.

Schema lives in ``rwa_score.api.migrations`` (SQLite now, Postgres SQL dump
for a later self-hosted apply). This class does not open a hosted database.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .migrations import SQLITE_001 as SCHEMA, apply_sqlite_migrations


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


def _key_from_row(row: sqlite3.Row) -> ApiKey:
    return ApiKey(
        id=int(row["id"]),
        key_hash=row["key_hash"],
        key_prefix=row["key_prefix"],
        name=row["name"],
        tier=row["tier"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
    )


def _blob(value: Any) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.encode("utf-8")
    return bytes(value)


def _payload_from_row(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "score_hash": row["score_hash"],
        "ticker": row["ticker"],
        "canonical": _blob(row["canonical_json"]),
        "inputs": _blob(row["inputs_json"]) if "inputs_json" in keys else None,
        "stored_at": row["stored_at"],
        "tx_hash": row["tx_hash"] if "tx_hash" in keys else None,
        "attested_at": row["attested_at"] if "attested_at" in keys else None,
    }


def _job_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "score_hash": row["score_hash"],
        "ticker": row["ticker"],
        "claimed_at": int(row["claimed_at"]),
        "status": row["status"],
        "tx_hash": row["tx_hash"],
        "attested_at": row["attested_at"],
        "attempts": int(row["attempts"]),
        "next_attempt_at": float(row["next_attempt_at"] or 0),
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _hook_from_row(row: sqlite3.Row) -> Webhook:
    ticker = row["ticker"]
    return Webhook(
        id=int(row["id"]),
        key_id=int(row["key_id"]),
        url=row["url"],
        secret=row["secret"],
        ticker=(ticker.upper() if ticker else None),
        trigger=row["trigger"],
        created_at=row["created_at"],
        active=bool(row["active"]),
    )


class Store:
    """Process-local SQLite store. Swap the path for Postgres later."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent != Path("."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init()

    def _init(self) -> None:
        with self._lock:
            apply_sqlite_migrations(self._conn)
            self._migrate_last_bands()
            self._conn.commit()

    def _migrate_last_bands(self) -> None:
        """Scope band-crossing state per API key. Drop unattributable legacy rows."""
        info = self._conn.execute("PRAGMA table_info(last_bands)").fetchall()
        names = {row["name"] for row in info}
        if "key_id" in names:
            return
        self._conn.execute("ALTER TABLE last_bands RENAME TO last_bands_pre_tenant")
        self._conn.execute(
            """
            CREATE TABLE last_bands (
                key_id INTEGER NOT NULL REFERENCES api_keys(id),
                ticker TEXT NOT NULL,
                band TEXT NOT NULL,
                score REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (key_id, ticker)
            )
            """
        )
        self._conn.execute("DROP TABLE last_bands_pre_tenant")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

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
        prefix = raw[:12]
        now = _iso()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ?", (digest,)
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE api_keys SET name = ?, tier = ?, revoked_at = NULL WHERE id = ?",
                    (name, tier, row["id"]),
                )
                self._conn.commit()
                return _key_from_row(
                    self._conn.execute(
                        "SELECT * FROM api_keys WHERE id = ?", (row["id"],)
                    ).fetchone()
                )
            self._conn.execute(
                "INSERT INTO api_keys (key_hash, key_prefix, name, tier, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (digest, prefix, name, tier, now),
            )
            self._conn.commit()
            return _key_from_row(
                self._conn.execute(
                    "SELECT * FROM api_keys WHERE key_hash = ?", (digest,)
                ).fetchone()
            )

    def _insert_key(self, raw: str, *, name: str, tier: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO api_keys (key_hash, key_prefix, name, tier, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (hash_key(raw), raw[:12], name, tier, _iso()),
            )
            self._conn.commit()

    def lookup_key(self, raw: str) -> ApiKey | None:
        digest = hash_key(raw)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ?", (digest,)
            ).fetchone()
        if row is None:
            return None
        return _key_from_row(row)

    def list_keys(self) -> list[ApiKey]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM api_keys ORDER BY id"
            ).fetchall()
        return [_key_from_row(r) for r in rows]

    def revoke_key(self, *, prefix: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE api_keys SET revoked_at = ? "
                "WHERE key_prefix = ? AND revoked_at IS NULL",
                (_iso(), prefix),
            )
            self._conn.commit()
            return int(cur.rowcount)

    def count_usage(self, key_id: int, *, since: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM usage_events WHERE key_id = ? AND ts > ?",
                (key_id, since),
            ).fetchone()
        return int(row["n"]) if row else 0

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
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM usage_events WHERE key_id = ? AND ts > ?",
                (key_id, since),
            ).fetchone()
            used = int(row["n"]) if row else 0
            if limit is not None and used >= limit:
                return False, used
            self._conn.execute(
                "INSERT INTO usage_events (key_id, ts, path) VALUES (?, ?, ?)",
                (key_id, now, path),
            )
            self._conn.execute("DELETE FROM usage_events WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return True, used + 1

    def record_usage(self, key_id: int, path: str, *, ts: float | None = None) -> None:
        stamped = time.time() if ts is None else ts
        cutoff = stamped - (86_400.0 * 2)
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage_events (key_id, ts, path) VALUES (?, ?, ?)",
                (key_id, stamped, path),
            )
            self._conn.execute("DELETE FROM usage_events WHERE ts < ?", (cutoff,))
            self._conn.commit()

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
        with self._lock:
            self._conn.execute(
                "INSERT INTO score_history "
                "(ticker, scored_at, score, band, payload_json, payload_hash, key_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ticker.upper(), stamped, score, band, payload_json, payload_hash, key_id),
            )
            self._conn.commit()

    def get_history(
        self,
        ticker: str,
        *,
        limit: int = 30,
        key_id: int | None = None,
    ) -> list[dict[str, Any]]:
        symbol = ticker.upper()
        with self._lock:
            if key_id is None:
                rows = self._conn.execute(
                    "SELECT ticker, scored_at, score, band, payload_hash, key_id "
                    "FROM score_history WHERE ticker = ? "
                    "ORDER BY scored_at DESC LIMIT ?",
                    (symbol, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT ticker, scored_at, score, band, payload_hash, key_id "
                    "FROM score_history WHERE ticker = ? AND key_id = ? "
                    "ORDER BY scored_at DESC LIMIT ?",
                    (symbol, key_id, limit),
                ).fetchall()
        return [
            {
                "ticker": r["ticker"],
                "scored_at": r["scored_at"],
                "score": r["score"],
                "band": r["band"],
                "payload_hash": r["payload_hash"],
                "key_id": r["key_id"],
            }
            for r in rows
        ]

    def get_last_band(self, key_id: int, ticker: str) -> tuple[str, float] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT band, score FROM last_bands WHERE key_id = ? AND ticker = ?",
                (key_id, ticker.upper()),
            ).fetchone()
        if row is None:
            return None
        return str(row["band"]), float(row["score"])

    def set_last_band(self, key_id: int, ticker: str, band: str, score: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO last_bands (key_id, ticker, band, score, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(key_id, ticker) DO UPDATE SET band = excluded.band, "
                "score = excluded.score, updated_at = excluded.updated_at",
                (key_id, ticker.upper(), band, score, time.time()),
            )
            self._conn.commit()

    def add_watchlist(self, key_id: int, tickers: Iterable[str]) -> list[str]:
        symbols = [t.strip().upper() for t in tickers if t and t.strip()]
        with self._lock:
            for symbol in symbols:
                self._conn.execute(
                    "INSERT OR IGNORE INTO watchlist_items (key_id, ticker) VALUES (?, ?)",
                    (key_id, symbol),
                )
            self._conn.commit()
        return self.get_watchlist(key_id)

    def set_watchlist(self, key_id: int, tickers: Iterable[str]) -> list[str]:
        symbols = [t.strip().upper() for t in tickers if t and t.strip()]
        with self._lock:
            self._conn.execute("DELETE FROM watchlist_items WHERE key_id = ?", (key_id,))
            for symbol in symbols:
                self._conn.execute(
                    "INSERT INTO watchlist_items (key_id, ticker) VALUES (?, ?)",
                    (key_id, symbol),
                )
            self._conn.commit()
        return list(symbols)

    def remove_watchlist(self, key_id: int, ticker: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM watchlist_items WHERE key_id = ? AND ticker = ?",
                (key_id, ticker.upper()),
            )
            self._conn.commit()

    def get_watchlist(self, key_id: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ticker FROM watchlist_items WHERE key_id = ? ORDER BY ticker",
                (key_id,),
            ).fetchall()
        return [r["ticker"] for r in rows]

    def all_watchlist_tickers(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT ticker FROM watchlist_items ORDER BY ticker"
            ).fetchall()
        return [r["ticker"] for r in rows]

    def watchlist_entries(self) -> list[tuple[int, str]]:
        """Per-tenant watchlist rows so poll can isolate last_bands / webhooks."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key_id, ticker FROM watchlist_items ORDER BY key_id, ticker"
            ).fetchall()
        return [(int(r["key_id"]), str(r["ticker"])) for r in rows]

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
            cur = self._conn.execute(
                "INSERT INTO webhooks (key_id, url, secret, ticker, trigger, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key_id, url, secret, symbol, trigger, _iso()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM webhooks WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return _hook_from_row(row)

    def list_webhooks(self, key_id: int) -> list[Webhook]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM webhooks WHERE key_id = ? ORDER BY id", (key_id,)
            ).fetchall()
        return [_hook_from_row(r) for r in rows]

    def get_webhook(self, webhook_id: int) -> Webhook | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM webhooks WHERE id = ?", (webhook_id,)
            ).fetchone()
        return _hook_from_row(row) if row else None

    def deactivate_webhook(self, key_id: int, webhook_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE webhooks SET active = 0 WHERE id = ? AND key_id = ?",
                (webhook_id, key_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def active_webhooks(self, key_id: int) -> list[Webhook]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM webhooks WHERE active = 1 AND key_id = ? ORDER BY id",
                (key_id,),
            ).fetchall()
        return [_hook_from_row(r) for r in rows]

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
            self._conn.execute(
                "INSERT INTO webhook_deliveries "
                "(webhook_id, ticker, event, status_code, delivered_at, ok) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (webhook_id, ticker.upper(), event, status_code, time.time(), int(ok)),
            )
            self._conn.commit()

    def save_attested_payload(
        self,
        *,
        ticker: str,
        canonical: bytes,
        inputs: bytes | None = None,
    ) -> str:
        """Store the exact canonical JSON bytes, keyed by their SHA-256.

        ``inputs`` is the canonical scoring-input JSON (see
        ``attestation_inputs``), stored beside the payload so
        ``inputs_digest`` can be recomputed. Same hash is idempotent.
        A different byte string under that hash is refused. A later save
        may fill ``inputs`` when the existing row has none; it may not
        replace inputs that are already stored. Render free disk is
        ephemeral — this row is gone after spin-down unless the sqlite
        file lives on a persistent disk.
        """
        from .attest import hash_canonical

        if not isinstance(canonical, (bytes, bytearray)):
            raise TypeError("canonical payload must be bytes")
        raw = bytes(canonical)
        inputs_raw = None if inputs is None else bytes(inputs)
        digest = hash_canonical(raw)
        symbol = ticker.strip().upper()
        with self._lock:
            existing = self._conn.execute(
                "SELECT canonical_json, inputs_json FROM attested_payloads WHERE score_hash = ?",
                (digest,),
            ).fetchone()
            if existing is not None:
                stored = existing["canonical_json"]
                if isinstance(stored, str):
                    stored = stored.encode("utf-8")
                if bytes(stored) != raw:
                    raise ValueError("refusing to replace attested payload bytes for an existing hash")
                if inputs_raw is not None:
                    prior = existing["inputs_json"]
                    if prior is None:
                        self._conn.execute(
                            "UPDATE attested_payloads SET inputs_json = ? WHERE score_hash = ?",
                            (inputs_raw, digest),
                        )
                        self._conn.commit()
                    else:
                        if isinstance(prior, str):
                            prior = prior.encode("utf-8")
                        if bytes(prior) != inputs_raw:
                            raise ValueError(
                                "refusing to replace attested inputs for an existing hash"
                            )
                return digest
            self._conn.execute(
                "INSERT INTO attested_payloads "
                "(score_hash, ticker, canonical_json, stored_at, inputs_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (digest, symbol, raw, _iso(), inputs_raw),
            )
            self._conn.commit()
        return digest

    def get_attested_payload(self, score_hash: str) -> dict[str, Any] | None:
        digest = score_hash.strip()
        with self._lock:
            row = self._conn.execute(
                "SELECT score_hash, ticker, canonical_json, inputs_json, stored_at, "
                "tx_hash, attested_at "
                "FROM attested_payloads WHERE score_hash = ?",
                (digest,),
            ).fetchone()
        if row is None:
            return None
        return _payload_from_row(row)

    def latest_attested_payload(self, ticker: str) -> dict[str, Any] | None:
        symbol = ticker.strip().upper()
        with self._lock:
            row = self._conn.execute(
                "SELECT score_hash, ticker, canonical_json, inputs_json, stored_at, "
                "tx_hash, attested_at "
                "FROM attested_payloads WHERE ticker = ? "
                "ORDER BY stored_at DESC, rowid DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        if row is None:
            return None
        return _payload_from_row(row)

    def enqueue_attest_job(
        self,
        *,
        score_hash: str,
        ticker: str,
        claimed_at: int,
        force: bool = False,
    ) -> dict[str, Any]:
        """Queue one send. A pending or confirmed job for this hash is reused.

        ``force`` inserts another pending row so a rerun can hit
        ``AlreadyAttested`` / the verify pre-check. The API does not set it.
        """
        digest = score_hash.strip()
        symbol = ticker.strip().upper()
        now = _iso()
        with self._lock:
            if not force:
                existing = self._conn.execute(
                    "SELECT * FROM attest_jobs WHERE score_hash = ? "
                    "AND status IN ('pending', 'confirmed') "
                    "ORDER BY id DESC LIMIT 1",
                    (digest,),
                ).fetchone()
                if existing is not None:
                    return _job_from_row(existing)
            self._conn.execute(
                "INSERT INTO attest_jobs ("
                "score_hash, ticker, claimed_at, status, attempts, next_attempt_at, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, 'pending', 0, 0, ?, ?)",
                (digest, symbol, int(claimed_at), now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM attest_jobs WHERE score_hash = ? ORDER BY id DESC LIMIT 1",
                (digest,),
            ).fetchone()
        return _job_from_row(row)

    def latest_attest_job(self, score_hash: str) -> dict[str, Any] | None:
        digest = score_hash.strip()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM attest_jobs WHERE score_hash = ? ORDER BY id DESC LIMIT 1",
                (digest,),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def claim_next_attest_job(self, *, now: float) -> dict[str, Any] | None:
        """Take the oldest due pending job and count one attempt. Single worker."""
        stamped = _iso()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM attest_jobs WHERE status = 'pending' AND next_attempt_at <= ? "
                "ORDER BY id LIMIT 1",
                (float(now),),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE attest_jobs SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (stamped, row["id"]),
            )
            self._conn.commit()
            fresh = self._conn.execute(
                "SELECT * FROM attest_jobs WHERE id = ?",
                (row["id"],),
            ).fetchone()
        return _job_from_row(fresh)

    def finish_attest_job(
        self,
        job_id: int,
        *,
        status: str,
        tx_hash: str | None = None,
        attested_at: int | None = None,
        error: str | None = None,
        next_attempt_at: float | None = None,
    ) -> None:
        if status not in {"pending", "confirmed", "failed"}:
            raise ValueError(f"unknown job status: {status}")
        stamped = _iso()
        with self._lock:
            row = self._conn.execute(
                "SELECT score_hash, tx_hash FROM attest_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return
            kept_tx = tx_hash or row["tx_hash"]
            self._conn.execute(
                "UPDATE attest_jobs SET status = ?, tx_hash = ?, attested_at = ?, "
                "last_error = ?, next_attempt_at = ?, updated_at = ? WHERE id = ?",
                (
                    status,
                    kept_tx,
                    attested_at,
                    error,
                    0.0 if next_attempt_at is None else float(next_attempt_at),
                    stamped,
                    job_id,
                ),
            )
            if status == "confirmed":
                if kept_tx:
                    self._conn.execute(
                        "UPDATE attested_payloads SET tx_hash = ? WHERE score_hash = ?",
                        (kept_tx, row["score_hash"]),
                    )
                if attested_at is not None:
                    self._conn.execute(
                        "UPDATE attested_payloads SET attested_at = ? WHERE score_hash = ?",
                        (int(attested_at), row["score_hash"]),
                    )
            self._conn.commit()

    def recent_deliveries(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT webhook_id, ticker, event, status_code, delivered_at, ok "
                "FROM webhook_deliveries ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
