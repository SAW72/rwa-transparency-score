"""SQLite persistence for keys, usage, history, watchlists, and webhooks."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY,
    key_hash TEXT UNIQUE NOT NULL,
    key_prefix TEXT NOT NULL,
    name TEXT NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('free', 'paid')),
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY,
    key_id INTEGER NOT NULL REFERENCES api_keys(id),
    ts REAL NOT NULL,
    path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_key_ts ON usage_events(key_id, ts);

CREATE TABLE IF NOT EXISTS score_history (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    scored_at REAL NOT NULL,
    score REAL NOT NULL,
    band TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_ticker ON score_history(ticker, scored_at);

CREATE TABLE IF NOT EXISTS watchlist_items (
    id INTEGER PRIMARY KEY,
    key_id INTEGER NOT NULL REFERENCES api_keys(id),
    ticker TEXT NOT NULL,
    UNIQUE(key_id, ticker)
);

CREATE TABLE IF NOT EXISTS webhooks (
    id INTEGER PRIMARY KEY,
    key_id INTEGER NOT NULL REFERENCES api_keys(id),
    url TEXT NOT NULL,
    secret TEXT NOT NULL,
    ticker TEXT,
    trigger TEXT NOT NULL DEFAULT 'band_cross',
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS last_bands (
    ticker TEXT PRIMARY KEY,
    band TEXT NOT NULL,
    score REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id INTEGER PRIMARY KEY,
    webhook_id INTEGER NOT NULL REFERENCES webhooks(id),
    ticker TEXT NOT NULL,
    event TEXT NOT NULL,
    status_code INTEGER,
    delivered_at REAL NOT NULL,
    ok INTEGER NOT NULL
);
"""


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
            self._conn.executescript(SCHEMA)
            self._conn.commit()

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
    ) -> None:
        stamped = time.time() if scored_at is None else scored_at
        with self._lock:
            self._conn.execute(
                "INSERT INTO score_history "
                "(ticker, scored_at, score, band, payload_json, payload_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ticker.upper(), stamped, score, band, payload_json, payload_hash),
            )
            self._conn.commit()

    def get_history(self, ticker: str, *, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ticker, scored_at, score, band, payload_hash "
                "FROM score_history WHERE ticker = ? ORDER BY scored_at DESC LIMIT ?",
                (ticker.upper(), limit),
            ).fetchall()
        return [
            {
                "ticker": r["ticker"],
                "scored_at": r["scored_at"],
                "score": r["score"],
                "band": r["band"],
                "payload_hash": r["payload_hash"],
            }
            for r in rows
        ]

    def get_last_band(self, ticker: str) -> tuple[str, float] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT band, score FROM last_bands WHERE ticker = ?",
                (ticker.upper(),),
            ).fetchone()
        if row is None:
            return None
        return str(row["band"]), float(row["score"])

    def set_last_band(self, ticker: str, band: str, score: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO last_bands (ticker, band, score, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(ticker) DO UPDATE SET band = excluded.band, "
                "score = excluded.score, updated_at = excluded.updated_at",
                (ticker.upper(), band, score, time.time()),
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

    def active_webhooks(self) -> list[Webhook]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM webhooks WHERE active = 1"
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

    def recent_deliveries(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT webhook_id, ticker, event, status_code, delivered_at, ok "
                "FROM webhook_deliveries ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
