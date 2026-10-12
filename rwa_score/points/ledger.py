"""Append-only ledger of off-chain points.

Points are unpaid activity counters. There is no token. Nothing here is cash.
The table is insert-only: ``append`` is the only write, and the database
rejects UPDATE, DELETE, and REPLACE.

``rwa_score.api.store.Store`` keeps API keys and pending transactions in
process memory and does not open a database. This ledger uses the local
SQLite connection shape that store used for its own file: a path,
``check_same_thread=False``, ``sqlite3.Row``, a thread lock, and a schema
script applied on init. It does not read a hosted database URL.

Actions are the fixed set in ``docs/POINTS-PROGRAM.md``:

- ``score`` — 5
- ``attest`` — 25
- ``share`` — 15
- ``integration`` — 100
- ``bug`` — a positive integer (that note uses 100–500 by severity)

``append`` records the positive integer it is given. It does not convert it.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

ACTIONS: tuple[str, ...] = ("score", "attest", "share", "integration", "bug")

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_EVM_TX = re.compile(r"^0x[0-9a-f]{64}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

AppendStatus = Literal["inserted", "duplicate"]


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def normalize_wallet(wallet: str) -> str:
    """Return a lowercase ``0x`` + 20-byte hex address, or raise ``ValueError``."""
    if not isinstance(wallet, str):
        raise ValueError("wallet must be a 0x-prefixed 20-byte hex address")
    text = wallet.strip()
    if text[:2].lower() == "0x":
        text = "0x" + text[2:]
    if not _ADDRESS.fullmatch(text):
        raise ValueError("wallet must be a 0x-prefixed 20-byte hex address")
    return text.lower()


def normalize_tx_hash(tx_hash: str) -> str:
    """Strip ``tx_hash``. Fold EVM hashes and 64-hex digests to lowercase.

    Other keys are kept as written after stripping, so a caller-supplied
    label stays stable. Empty text is rejected.
    """
    if not isinstance(tx_hash, str):
        raise ValueError("tx_hash must be non-empty text")
    text = tx_hash.strip()
    if not text or len(text) > 128:
        raise ValueError("tx_hash must be non-empty text of at most 128 characters")
    lowered = text.lower()
    if _EVM_TX.fullmatch(lowered) or _HEX64.fullmatch(lowered):
        return lowered
    return text


def _require_action(action: str) -> str:
    if not isinstance(action, str):
        raise ValueError("unknown action")
    name = action.strip().lower()
    if name not in ACTIONS:
        shown = name[:40]
        raise ValueError(f"unknown action: {shown!r}")
    return name


def _require_points(points: int) -> int:
    if isinstance(points, bool) or not isinstance(points, int) or points <= 0:
        raise ValueError("points must be a positive integer")
    return points


def _canonical_issuer(issuer: str) -> str:
    if not isinstance(issuer, str):
        raise ValueError("issuer is required")
    text = issuer.strip()
    if not text:
        raise ValueError("issuer is required")
    return text.upper()


def _utc_day(on: date | datetime | str | None) -> str:
    if on is None:
        return datetime.now(timezone.utc).date().isoformat()
    if isinstance(on, datetime):
        aware = on if on.tzinfo is not None else on.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).date().isoformat()
    if isinstance(on, date):
        return on.isoformat()
    if isinstance(on, str):
        text = on.strip()
        if not _DAY.fullmatch(text):
            raise ValueError("UTC date must be YYYY-MM-DD")
        datetime.strptime(text, "%Y-%m-%d")
        return text
    raise ValueError("UTC date must be YYYY-MM-DD")


def synthetic_tx_key(
    wallet: str,
    action: str,
    issuer: str,
    *,
    on: date | datetime | str | None = None,
) -> str:
    """Deterministic stand-in for ``tx_hash`` on off-chain actions.

    The digest is lowercase SHA-256 hex of::

        wallet|action|issuer|YYYY-MM-DD

    ``wallet`` is the normalized ``0x`` address, ``action`` is one of
    ``ACTIONS``, ``issuer`` is stripped and uppercased, and the date is a
    UTC calendar day (``on`` defaults to today in UTC). The same inputs on
    the same UTC day always return the same key, so a repeated insert for
    that issuer does not add a second row.
    """
    day = _utc_day(on)
    material = "|".join(
        (
            normalize_wallet(wallet),
            _require_action(action),
            _canonical_issuer(issuer),
            day,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _metadata_json(metadata: Mapping[str, Any] | None) -> str | None:
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a JSON object")
    try:
        return json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata must be a JSON object") from exc


def init_db(conn: sqlite3.Connection) -> None:
    """Apply ``schema.sql``. Safe to call more than once on the same file."""
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _connect(path: str | Path) -> sqlite3.Connection:
    if str(path) == ":memory:":
        target = ":memory:"
    else:
        file_path = Path(path)
        if file_path.parent != Path("."):
            file_path.parent.mkdir(parents=True, exist_ok=True)
        target = str(file_path)
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA recursive_triggers = ON")
    return conn


@dataclass(frozen=True)
class LedgerEntry:
    id: int
    wallet: str
    action: str
    points: int
    tx_hash: str
    metadata: dict[str, Any] | None
    created_at: str


@dataclass(frozen=True)
class AppendResult:
    """``status`` is ``"inserted"`` or ``"duplicate"``.

    A duplicate is the existing row. The new points are not recorded.
    """

    status: AppendStatus
    entry: LedgerEntry


@dataclass(frozen=True)
class WalletTotal:
    wallet: str
    points: int
    by_action: dict[str, int]


def _entry_from_row(row: sqlite3.Row) -> LedgerEntry:
    raw_meta = row["metadata"]
    metadata = None if raw_meta is None else json.loads(raw_meta)
    return LedgerEntry(
        id=int(row["id"]),
        wallet=row["wallet"],
        action=row["action"],
        points=int(row["points"]),
        tx_hash=row["tx_hash"],
        metadata=metadata,
        created_at=row["created_at"],
    )


class PointsLedger:
    """Process-local SQLite ledger. ``append`` is the only write."""

    backend = "sqlite"

    def __init__(self, path: str | Path) -> None:
        self.path: Path | None = None if str(path) == ":memory:" else Path(path)
        self._lock = threading.Lock()
        self._conn = _connect(path)
        with self._lock:
            init_db(self._conn)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def append(
        self,
        *,
        wallet: str,
        action: str,
        points: int,
        tx_hash: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> AppendResult:
        """Insert one row, or return ``status="duplicate"`` when the key exists.

        The unique key is ``(wallet, action, tx_hash)``. A duplicate does not
        change the stored points.
        """
        normalized = normalize_wallet(wallet)
        act = _require_action(action)
        amount = _require_points(points)
        tx = normalize_tx_hash(tx_hash)
        meta = _metadata_json(metadata)
        created_at = _iso()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO points_ledger (
                        wallet, action, points, tx_hash, metadata, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (normalized, act, amount, tx, meta, created_at),
                )
                row_id = int(cur.lastrowid)
                self._conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._conn.execute("ROLLBACK")
                # UNIQUE is the constraint backstop. A repeated key also hits
                # the BEFORE INSERT trigger, whose message says append-only.
                message = str(exc).lower()
                if "unique" not in message and "append-only" not in message:
                    raise
                existing = self._conn.execute(
                    """
                    SELECT * FROM points_ledger
                    WHERE wallet = ? AND action = ? AND tx_hash = ?
                    """,
                    (normalized, act, tx),
                ).fetchone()
                if existing is None:
                    raise
                return AppendResult(status="duplicate", entry=_entry_from_row(existing))
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            row = self._conn.execute(
                "SELECT * FROM points_ledger WHERE id = ?",
                (row_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("inserted points row was not readable")
        return AppendResult(status="inserted", entry=_entry_from_row(row))

    def by_wallet(self, wallet: str) -> list[LedgerEntry]:
        """Rows for one wallet, oldest insert first."""
        normalized = normalize_wallet(wallet)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM points_ledger WHERE wallet = ? ORDER BY id",
                (normalized,),
            ).fetchall()
        return [_entry_from_row(row) for row in rows]

    def totals(self) -> list[WalletTotal]:
        """Point sums per wallet, highest total first.

        Ties break by wallet address. ``by_action`` is the same sum split
        across the fixed action set that this wallet has rows for.
        """
        with self._lock:
            sums = self._conn.execute(
                """
                SELECT wallet, SUM(points) AS points
                FROM points_ledger
                GROUP BY wallet
                ORDER BY points DESC, wallet ASC
                """
            ).fetchall()
            breakdown = self._conn.execute(
                """
                SELECT wallet, action, SUM(points) AS points
                FROM points_ledger
                GROUP BY wallet, action
                """
            ).fetchall()
        by_action: dict[str, dict[str, int]] = {}
        for row in breakdown:
            by_action.setdefault(str(row["wallet"]), {})[str(row["action"])] = int(row["points"])
        return [
            WalletTotal(
                wallet=str(row["wallet"]),
                points=int(row["points"]),
                by_action=dict(by_action.get(str(row["wallet"]), {})),
            )
            for row in sums
        ]
