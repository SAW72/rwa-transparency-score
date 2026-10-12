"""Append-only ledger of off-chain points.

Points are unpaid activity counters. There is no token. Nothing here is cash.
The table is insert-only: the database rejects UPDATE, DELETE, and REPLACE.

The public write for a points award is ``record`` in ``rwa_score.points.rubric``.
``PointsLedger._append`` is for ledger tests. It is not a route entry point,
and it cannot add a second row for the same wallet, action, and UTC day.

``rwa_score.api.store.Store`` keeps API keys and pending transactions in
process memory and does not open a database. This ledger uses the local
SQLite connection shape that store used for its own file: a path,
``check_same_thread=False``, ``sqlite3.Row``, a thread lock, and a schema
script applied on init. It does not read a hosted database URL.

Actions are the fixed set in ``docs/POINTS-PROGRAM.md``. Point amounts are
placeholders pending Spencer and live in the rubric, not in this module.
"""

from __future__ import annotations

import hashlib
import json
import os
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

# Off-chain rows use this prefix plus a SHA-256 hex digest. The rubric builds
# that string. A client-supplied tx hash is not a synthetic key.
SYNTHETIC_KEY_PREFIX = "rat-points-v1|"

# schema.sql. R1 files have no utc_day column and user_version 0.
SCHEMA_VERSION = 2

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Exact on-chain form. No strip, no case fold. Receipt comparison is R3.
_EVM_TX = re.compile(r"^0x[0-9a-f]{64}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TRIGGER_NAMES = (
    "points_ledger_block_update",
    "points_ledger_block_delete",
    "points_ledger_block_reinsert",
    "points_ledger_remember_key",
    "points_ledger_keys_block_update",
    "points_ledger_keys_block_delete",
)

AppendStatus = Literal["inserted", "duplicate"]
InsertStatus = Literal["inserted", "duplicate", "capped", "rejected"]


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
    """Return a stored key, or raise ``ValueError``.

    An on-chain hash is accepted only when the original string matches
    ``^0x[0-9a-f]{64}$``. A ``0X`` prefix, uppercase hex, and surrounding
    whitespace are rejected rather than rewritten. That check is format
    only. Comparing the hash to an on-chain receipt is R3, before routes.

    Server keys (``rat-points-v1|`` plus 64 hex) are folded to the canonical
    prefix and lowercase digest. Other labels are kept as written after
    stripping. Empty text is rejected.
    """
    if not isinstance(tx_hash, str):
        raise ValueError("tx_hash must be non-empty text")
    stripped = tx_hash.strip()
    if stripped[:2].lower() == "0x":
        if not _EVM_TX.fullmatch(tx_hash):
            raise ValueError("on-chain tx hash must match ^0x[0-9a-f]{64}$")
        return tx_hash
    if not stripped or len(stripped) > 128:
        raise ValueError("tx_hash must be non-empty text of at most 128 characters")
    lowered = stripped.lower()
    if lowered.startswith(SYNTHETIC_KEY_PREFIX):
        digest = lowered[len(SYNTHETIC_KEY_PREFIX) :]
        if _HEX64.fullmatch(digest):
            return SYNTHETIC_KEY_PREFIX + digest
    if _HEX64.fullmatch(lowered):
        return lowered
    return stripped


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


def _require_utc_day(utc_day: str) -> str:
    if not isinstance(utc_day, str) or not _DAY.fullmatch(utc_day):
        raise ValueError("UTC date must be YYYY-MM-DD")
    datetime.strptime(utc_day, "%Y-%m-%d")
    return utc_day


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
        return _require_utc_day(on.strip())
    raise ValueError("UTC date must be YYYY-MM-DD")


def synthetic_tx_key(
    wallet: str,
    action: str,
    issuer: str,
    *,
    on: date | datetime | str | None = None,
) -> str:
    """Server-built stand-in for ``tx_hash`` on off-chain actions.

    The key is ``rat-points-v1|`` plus the lowercase SHA-256 hex of::

        wallet|action|issuer|YYYY-MM-DD

    ``wallet`` is the normalized ``0x`` address, ``action`` is one of
    ``ACTIONS``, ``issuer`` is stripped and uppercased, and the date is a
    UTC calendar day (``on`` defaults to today in UTC). The same inputs on
    the same UTC day always return the same key, so a repeated insert for
    that issuer does not add a second row. This function does not read a
    client ``tx_hash``.
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
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{SYNTHETIC_KEY_PREFIX}{digest}"


def _metadata_json(metadata: Mapping[str, Any] | None) -> str | None:
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a JSON object")
    try:
        return json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata must be a JSON object") from exc


def _schema_statements() -> list[str]:
    """Split ``schema.sql`` without breaking trigger bodies on semicolons.

    ``executescript`` issues a COMMIT before it runs, so a migration cannot
    use it inside ``BEGIN IMMEDIATE``.
    """
    statements: list[str] = []
    buf: list[str] = []
    depth = 0
    for line in _SCHEMA_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not buf and (not stripped or stripped.startswith("--")):
            continue
        buf.append(line)
        upper = stripped.upper()
        if upper == "BEGIN" or upper.startswith("BEGIN "):
            depth += 1
        elif upper in {"END", "END;"}:
            depth = max(0, depth - 1)
        if depth == 0 and stripped.endswith(";"):
            text = "\n".join(buf).strip()
            if text:
                statements.append(text)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _day_from_r1_row(row: sqlite3.Row) -> str:
    raw_meta = row["metadata"]
    if raw_meta:
        try:
            metadata = json.loads(raw_meta)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "existing row has unreadable metadata; the file was left unchanged"
            ) from exc
        utc_date = metadata.get("utc_date") if isinstance(metadata, dict) else None
        if isinstance(utc_date, str) and _DAY.fullmatch(utc_date):
            datetime.strptime(utc_date, "%Y-%m-%d")
            return utc_date
    created = str(row["created_at"])
    if len(created) >= 10 and _DAY.fullmatch(created[:10]):
        datetime.strptime(created[:10], "%Y-%m-%d")
        return created[:10]
    raise ValueError("existing row has no UTC day; the file was left unchanged")


def _migrate_r1(conn: sqlite3.Connection) -> None:
    """Rebuild an R1 file in place. The caller holds ``BEGIN IMMEDIATE``.

    ``utc_day`` comes from ``metadata.utc_date`` when that is a calendar day,
    otherwise from the first ten characters of ``created_at``. Two rows that
    share a wallet, action, and day, a row with points above 500, or a
    repeated attest hash abort the migration. Rollback leaves the R1 file
    as it was. Append-only triggers block UPDATE, so this copies rows into
    a new table instead of altering the old one.
    """
    rows = list(conn.execute("SELECT * FROM points_ledger ORDER BY id"))
    prepared: list[tuple[sqlite3.Row, str]] = []
    seen_days: set[tuple[str, str, str]] = set()
    seen_attest: set[str] = set()
    for row in rows:
        points = int(row["points"])
        if points > 500:
            raise ValueError("existing row has points above 500; the file was left unchanged")
        day = _day_from_r1_row(row)
        identity = (str(row["wallet"]), str(row["action"]), day)
        if identity in seen_days:
            raise ValueError(
                "existing rows share a wallet, action, and UTC day; the file was left unchanged"
            )
        seen_days.add(identity)
        if row["action"] == "attest":
            tx_hash = str(row["tx_hash"])
            if tx_hash in seen_attest:
                raise ValueError(
                    "existing attest hash is already used; the file was left unchanged"
                )
            seen_attest.add(tx_hash)
        prepared.append((row, day))

    for name in _TRIGGER_NAMES:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute("DROP INDEX IF EXISTS idx_points_ledger_wallet")
    conn.execute("DROP INDEX IF EXISTS idx_points_ledger_attest_tx")
    conn.execute("DROP TABLE IF EXISTS points_ledger_keys")
    conn.execute("ALTER TABLE points_ledger RENAME TO points_ledger_r1")
    for statement in _schema_statements():
        conn.execute(statement)
    for row, day in prepared:
        conn.execute(
            """
            INSERT INTO points_ledger (
                id, wallet, action, points, tx_hash, metadata, created_at, utc_day
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(row["id"]),
                row["wallet"],
                row["action"],
                int(row["points"]),
                row["tx_hash"],
                row["metadata"],
                row["created_at"],
                day,
            ),
        )
    conn.execute("DROP TABLE points_ledger_r1")


def init_db(conn: sqlite3.Connection) -> None:
    """Apply ``schema.sql``, or rebuild an R1 file that has no ``utc_day``.

    A new file is ``user_version`` 2. An R1 file is migrated inside one
    ``BEGIN IMMEDIATE``. If the migration raises, the file is unchanged.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'points_ledger'"
        ).fetchone()
        if exists is None:
            for statement in _schema_statements():
                conn.execute(statement)
        else:
            columns = {str(info[1]) for info in conn.execute("PRAGMA table_info(points_ledger)")}
            if "utc_day" not in columns:
                _migrate_r1(conn)
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _restrict_db_permissions(path: Path) -> None:
    """Owner read/write only, on platforms that store a Unix mode.

    SQLite would otherwise create the file with the process umask, which is
    often world-readable. A failure to set the mode is ignored: some
    filesystems do not implement it.
    """
    if os.name != "posix":
        return
    try:
        fd = os.open(os.fspath(path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            os.chmod(path, 0o600)
    except OSError:
        return
    finally:
        os.close(fd)


def _connect(path: str | Path) -> sqlite3.Connection:
    if str(path) == ":memory:":
        target = ":memory:"
    else:
        file_path = Path(path)
        if file_path.parent != Path("."):
            file_path.parent.mkdir(parents=True, exist_ok=True)
        _restrict_db_permissions(file_path)
        target = str(file_path)
    # timeout and busy_timeout let other processes wait on BEGIN IMMEDIATE
    # instead of failing with "database is locked".
    conn = sqlite3.connect(target, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA recursive_triggers = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        _restrict_db_permissions(Path(path))
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
    utc_day: str


@dataclass(frozen=True)
class AppendResult:
    """``status`` is ``"inserted"`` or ``"duplicate"``.

    A duplicate is the existing row. The new points are not recorded.
    """

    status: AppendStatus
    entry: LedgerEntry


@dataclass(frozen=True)
class InsertResult:
    """Outcome of one check-and-insert.

    ``rejected`` is an attest hash that another wallet already stored.
    ``capped`` is an existing row for this wallet, action, and UTC day.
    """

    status: InsertStatus
    entry: LedgerEntry | None


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
        utc_day=row["utc_day"],
    )


class PointsLedger:
    """Process-local SQLite ledger.

    Routes record points through ``rwa_score.points.rubric.record``. There
    is no public ``append``. The thread lock serializes use of this
    connection. The daily cap and the single-use attest hash are SQLite
    constraints, so another process cannot insert around them.
    """

    backend = "sqlite"

    def __init__(self, path: str | Path) -> None:
        self.path: Path | None = None if str(path) == ":memory:" else Path(path)
        self._lock = threading.Lock()
        self._conn = _connect(path)
        try:
            with self._lock:
                init_db(self._conn)
        except Exception:
            self._conn.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _insert_activity(
        self,
        *,
        wallet: str,
        action: str,
        points: int,
        tx_hash: str,
        metadata: Mapping[str, Any] | None = None,
        utc_day: str,
    ) -> InsertResult:
        """Check and insert in one ``BEGIN IMMEDIATE`` on this connection.

        The unique key ``(wallet, action, tx_hash)`` is a duplicate. An
        existing ``(wallet, action, utc_day)`` is the daily cap. An attest
        hash already stored for any wallet is rejected. ``points`` above
        500 hits the table CHECK and is re-raised.
        """
        normalized = normalize_wallet(wallet)
        act = _require_action(action)
        amount = _require_points(points)
        tx = normalize_tx_hash(tx_hash)
        meta = _metadata_json(metadata)
        day = _require_utc_day(utc_day)
        created_at = _iso()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = self._insert_in_transaction(
                    normalized, act, amount, tx, meta, created_at, day
                )
                self._conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._conn.execute("ROLLBACK")
                result = self._classify_integrity(exc, normalized, act, tx, day)
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return result

    def _insert_in_transaction(
        self,
        wallet: str,
        action: str,
        points: int,
        tx_hash: str,
        metadata: str | None,
        created_at: str,
        utc_day: str,
    ) -> InsertResult:
        existing = self._conn.execute(
            """
            SELECT * FROM points_ledger
            WHERE wallet = ? AND action = ? AND tx_hash = ?
            """,
            (wallet, action, tx_hash),
        ).fetchone()
        if existing is not None:
            return InsertResult(status="duplicate", entry=_entry_from_row(existing))
        # Same-issuer repeats are moot under the per-day cap: this unique
        # day already allows one row, so a second issuer also gets no slot.
        # The key check above still returns duplicate for the same issuer.
        day_row = self._conn.execute(
            """
            SELECT 1 FROM points_ledger
            WHERE wallet = ? AND action = ? AND utc_day = ?
            """,
            (wallet, action, utc_day),
        ).fetchone()
        if day_row is not None:
            return InsertResult(status="capped", entry=None)
        if action == "attest":
            reused = self._conn.execute(
                """
                SELECT 1 FROM points_ledger
                WHERE action = 'attest' AND tx_hash = ?
                """,
                (tx_hash,),
            ).fetchone()
            if reused is not None:
                return InsertResult(status="rejected", entry=None)
        cursor = self._conn.execute(
            """
            INSERT INTO points_ledger (
                wallet, action, points, tx_hash, metadata, created_at, utc_day
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet, action, points, tx_hash, metadata, created_at, utc_day),
        )
        row = self._conn.execute(
            "SELECT * FROM points_ledger WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
        if row is None:
            raise RuntimeError("inserted points row was not readable")
        return InsertResult(status="inserted", entry=_entry_from_row(row))

    def _classify_integrity(
        self,
        exc: sqlite3.IntegrityError,
        wallet: str,
        action: str,
        tx_hash: str,
        utc_day: str,
    ) -> InsertResult:
        message = str(exc).lower()
        if "check constraint failed" in message:
            raise exc
        existing = self._conn.execute(
            """
            SELECT * FROM points_ledger
            WHERE wallet = ? AND action = ? AND tx_hash = ?
            """,
            (wallet, action, tx_hash),
        ).fetchone()
        if existing is not None:
            return InsertResult(status="duplicate", entry=_entry_from_row(existing))
        if action == "attest":
            reused = self._conn.execute(
                """
                SELECT 1 FROM points_ledger
                WHERE action = 'attest' AND tx_hash = ?
                """,
                (tx_hash,),
            ).fetchone()
            if reused is not None:
                return InsertResult(status="rejected", entry=None)
        day_row = self._conn.execute(
            """
            SELECT 1 FROM points_ledger
            WHERE wallet = ? AND action = ? AND utc_day = ?
            """,
            (wallet, action, utc_day),
        ).fetchone()
        if day_row is not None:
            return InsertResult(status="capped", entry=None)
        raise exc

    def _append(
        self,
        *,
        wallet: str,
        action: str,
        points: int,
        tx_hash: str,
        metadata: Mapping[str, Any] | None = None,
        utc_day: str | None = None,
    ) -> AppendResult:
        """Test-only insert. Routes use ``record``.

        A second row for the same wallet, action, and UTC day raises
        ``ValueError``. A reused attest hash raises ``ValueError``. Neither
        path adds a row.
        """
        day = datetime.now(timezone.utc).date().isoformat() if utc_day is None else utc_day
        result = self._insert_activity(
            wallet=wallet,
            action=action,
            points=points,
            tx_hash=tx_hash,
            metadata=metadata,
            utc_day=day,
        )
        if result.status == "capped":
            raise ValueError("daily cap reached")
        if result.status == "rejected":
            raise ValueError("attest tx hash was already used")
        if result.entry is None:
            raise RuntimeError("points row was not readable")
        return AppendResult(status=result.status, entry=result.entry)

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
