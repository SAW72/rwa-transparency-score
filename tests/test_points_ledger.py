"""Append-only points ledger. Duplicates and raw edits must not change totals."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from rwa_score.points import PointsLedger, synthetic_tx_key

WALLET = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20


def _open(tmp_path: Path) -> PointsLedger:
    return PointsLedger(tmp_path / "points.sqlite")


def test_duplicate_insert_is_rejected_and_does_not_double_count(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    try:
        key = synthetic_tx_key(WALLET, "score", "NVDA", on="2026-10-12")
        first = ledger._append(
            wallet=WALLET,
            action="score",
            points=5,
            tx_hash=key,
            metadata={"issuer": "NVDA"},
        )
        assert first.status == "inserted"
        assert first.entry.points == 5
        assert first.entry.metadata == {"issuer": "NVDA"}

        again = ledger._append(
            wallet=WALLET.upper(),
            action="SCORE",
            points=50,
            tx_hash=key.upper(),
        )
        assert again.status == "duplicate"
        assert again.entry.id == first.entry.id
        assert again.entry.points == 5

        rows = ledger.by_wallet(WALLET)
        assert [row.id for row in rows] == [first.entry.id]
        assert [row.points for row in rows] == [5]
        totals = ledger.totals()
        assert len(totals) == 1
        assert totals[0].wallet == WALLET
        assert totals[0].points == 5
        assert totals[0].by_action == {"score": 5}

        chain = "0x" + "ab" * 32
        with pytest.raises(ValueError, match=r"\^0x\[0-9a-f\]\{64\}"):
            ledger._append(wallet=WALLET, action="attest", points=25, tx_hash="0x" + "AB" * 32)
        attested = ledger._append(wallet=WALLET, action="attest", points=25, tx_hash=chain)
        folded = ledger._append(wallet=WALLET, action="attest", points=25, tx_hash=chain)
        assert attested.status == "inserted"
        assert folded.status == "duplicate"
        assert ledger.totals()[0].points == 30
        assert ledger.totals()[0].by_action == {"attest": 25, "score": 5}
    finally:
        ledger.close()


def test_raw_update_and_delete_are_blocked(tmp_path: Path) -> None:
    path = tmp_path / "points.sqlite"
    ledger = PointsLedger(path)
    try:
        inserted = ledger._append(wallet=WALLET, action="share", points=15, tx_hash="share-1")
        assert inserted.status == "inserted"
    finally:
        ledger.close()

    other = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            other.execute("UPDATE points_ledger SET points = 1")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            other.execute("DELETE FROM points_ledger")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            other.execute(
                """
                INSERT INTO points_ledger (
                    wallet, action, points, tx_hash, created_at, utc_day
                )
                VALUES (?, 'share', 99, 'share-1', '2026-01-01T00:00:00Z', '2026-01-01')
                ON CONFLICT (wallet, action, tx_hash) DO UPDATE SET points = 99
                """,
                (WALLET,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            other.execute(
                """
                INSERT OR REPLACE INTO points_ledger (
                    wallet, action, points, tx_hash, created_at, utc_day
                ) VALUES (?, 'share', 99, 'share-1', '2026-01-01T00:00:00Z', '2026-01-01')
                """,
                (WALLET,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            other.execute("DELETE FROM points_ledger_keys")
        row = other.execute("SELECT points FROM points_ledger").fetchone()
        assert row is not None
        assert int(row[0]) == 15
        triggers = other.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        ).fetchall()
        assert "points_ledger_block_delete" in [name for (name,) in triggers]
        assert "points_ledger_block_update" in [name for (name,) in triggers]
        assert "points_ledger_block_reinsert" in [name for (name,) in triggers]
    finally:
        other.close()

    reopened = PointsLedger(path)
    try:
        assert reopened.totals()[0].points == 15
        for name in ("update", "delete", "remove", "upsert"):
            assert not callable(getattr(reopened, name, None))
    finally:
        reopened.close()


def test_wallets_are_normalized_to_lowercase(tmp_path: Path) -> None:
    mixed = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    ledger = _open(tmp_path)
    try:
        result = ledger._append(wallet=f"  {mixed}  ", action="integration", points=100, tx_hash="int-1")
        prefixed = "0X" + "AB" * 20
        also = ledger._append(wallet=prefixed, action="score", points=5, tx_hash="score-1")
        assert result.entry.wallet == mixed.lower()
        assert also.entry.wallet == prefixed.lower()
        assert ledger.by_wallet(mixed)[0].wallet == mixed.lower()
        assert ledger.by_wallet(mixed.lower())[0].id == result.entry.id
        assert ledger.totals()[0].wallet == mixed.lower()
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "wallet",
    [
        "",
        "0x1234",
        "ab" * 20,
        "0x" + "g" * 40,
        "0x" + "ab" * 19,
        "0x" + "ab" * 21,
    ],
)
def test_bad_addresses_are_rejected(tmp_path: Path, wallet: str) -> None:
    ledger = _open(tmp_path)
    try:
        with pytest.raises(ValueError, match="wallet"):
            ledger._append(wallet=wallet, action="score", points=5, tx_hash="once")
        assert ledger.totals() == []
    finally:
        ledger.close()


@pytest.mark.parametrize("points", [0, -1, -5, True, False, 1.5, "5"])
def test_non_positive_points_are_rejected(tmp_path: Path, points: object) -> None:
    ledger = _open(tmp_path)
    try:
        with pytest.raises(ValueError, match="points"):
            ledger._append(wallet=WALLET, action="bug", points=points, tx_hash="bug-1")  # type: ignore[arg-type]
        assert ledger.by_wallet(WALLET) == []
    finally:
        ledger.close()


def test_unknown_action_is_rejected(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    try:
        with pytest.raises(ValueError, match="unknown action"):
            ledger._append(wallet=WALLET, action="referral", points=5, tx_hash="nope")
        assert ledger.totals() == []
    finally:
        ledger.close()


def test_synthetic_key_is_deterministic() -> None:
    wallet = "0x" + "Ab" * 20
    day = "2026-10-12"
    first = synthetic_tx_key(wallet, "score", "nvda", on=day)
    second = synthetic_tx_key(wallet.lower(), "SCORE", "NVDA", on=date(2026, 10, 12))
    assert first == second
    material = f"{wallet.lower()}|score|NVDA|{day}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    assert first == "rat-points-v1|" + digest
    assert first.startswith("rat-points-v1|")
    assert len(first) == len("rat-points-v1|") + 64
    assert synthetic_tx_key(wallet, "score", "aapl", on=day) != first
    assert synthetic_tx_key(wallet, "attest", "nvda", on=day) != first
    assert synthetic_tx_key(wallet, "score", "nvda", on="2026-10-13") != first
    assert synthetic_tx_key(OTHER, "score", "nvda", on=day) != first


def test_db_file_is_owner_read_write_where_supported(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX file modes are not available")
    previous = os.umask(0)
    path = tmp_path / "nested" / "points.sqlite"
    ledger = None
    try:
        ledger = PointsLedger(path)
        ledger._append(wallet=WALLET, action="score", points=5, tx_hash="once")
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600
    finally:
        os.umask(previous)
        if ledger is not None:
            ledger.close()
    reopened = PointsLedger(path)
    try:
        assert (path.stat().st_mode & 0o777) == 0o600
        assert reopened.totals()[0].points == 5
    finally:
        reopened.close()


def test_two_wallets_rank_by_total(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    try:
        ledger._append(wallet=WALLET, action="score", points=5, tx_hash="a")
        ledger._append(wallet=OTHER, action="bug", points=100, tx_hash="b")
        totals = ledger.totals()
        assert [row.wallet for row in totals] == [OTHER, WALLET]
        assert [row.points for row in totals] == [100, 5]
    finally:
        ledger.close()


def test_append_is_not_a_public_write() -> None:
    assert "append" not in PointsLedger.__dict__


def test_new_database_records_schema_version(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    try:
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == 2
        row = ledger._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_points_ledger_attest_tx'"
        ).fetchone()
        assert row is not None
        assert "UNIQUE" in str(row[0]).upper()
        assert "attest" in str(row[0])
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "tx_hash",
    [
        "0X" + "ab" * 32,
        "0x" + "AB" * 32,
        "  0x" + "ab" * 32,
        "0x" + "ab" * 32 + " ",
        "\n0x" + "ab" * 32,
    ],
)
def test_onchain_hashes_are_not_rewritten(tmp_path: Path, tx_hash: str) -> None:
    ledger = _open(tmp_path)
    try:
        with pytest.raises(ValueError, match=r"\^0x\[0-9a-f\]\{64\}"):
            ledger._append(
                wallet=WALLET,
                action="attest",
                points=25,
                tx_hash=tx_hash,
                utc_day="2026-10-12",
            )
        assert ledger.totals() == []
    finally:
        ledger.close()


def test_append_cannot_bypass_the_daily_cap(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    try:
        first = ledger._append(
            wallet=WALLET, action="score", points=5, tx_hash="a", utc_day="2026-10-12"
        )
        assert first.entry.utc_day == "2026-10-12"
        with pytest.raises(ValueError, match="daily cap"):
            ledger._append(
                wallet=WALLET, action="score", points=5, tx_hash="b", utc_day="2026-10-12"
            )
        later = ledger._append(
            wallet=WALLET, action="score", points=5, tx_hash="c", utc_day="2026-10-13"
        )
        assert later.status == "inserted"
        assert len(ledger.by_wallet(WALLET)) == 2
    finally:
        ledger.close()


def test_database_rejects_points_above_500(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    try:
        stored = ledger._append(
            wallet=WALLET, action="bug", points=500, tx_hash="cap-ok", utc_day="2026-10-12"
        )
        assert stored.entry.points == 500
        with pytest.raises(sqlite3.IntegrityError, match="points <= 500"):
            ledger._append(
                wallet=OTHER, action="bug", points=501, tx_hash="too-big", utc_day="2026-10-12"
            )
        ledger._conn.execute("BEGIN IMMEDIATE")
        try:
            ledger._conn.execute(
                """
                INSERT INTO points_ledger (
                    wallet, action, points, tx_hash, created_at, utc_day
                ) VALUES (?, 'score', 501, 'raw-501', '2026-10-12T00:00:00Z', '2026-10-12')
                """,
                (OTHER,),
            )
            ledger._conn.execute("COMMIT")
            raise AssertionError("points above 500 were stored")
        except sqlite3.IntegrityError as exc:
            ledger._conn.execute("ROLLBACK")
            assert "points <= 500" in str(exc)
        assert ledger.by_wallet(OTHER) == []
        assert ledger.totals()[0].points == 500
    finally:
        ledger.close()


def test_attest_hash_unique_index_rejects_a_second_wallet(tmp_path: Path) -> None:
    chain = "0x" + "cd" * 32
    ledger = _open(tmp_path)
    try:
        ledger._append(
            wallet=WALLET, action="attest", points=25, tx_hash=chain, utc_day="2026-10-12"
        )
        with pytest.raises(ValueError, match="already used"):
            ledger._append(
                wallet=OTHER, action="attest", points=25, tx_hash=chain, utc_day="2026-10-13"
            )
        ledger._conn.execute("BEGIN IMMEDIATE")
        try:
            ledger._conn.execute(
                """
                INSERT INTO points_ledger (
                    wallet, action, points, tx_hash, created_at, utc_day
                ) VALUES (?, 'attest', 25, ?, '2026-10-13T00:00:00Z', '2026-10-13')
                """,
                (OTHER, chain),
            )
            ledger._conn.execute("COMMIT")
            raise AssertionError("attest hash was stored twice")
        except sqlite3.IntegrityError as exc:
            ledger._conn.execute("ROLLBACK")
            assert "unique" in str(exc).lower()
        assert ledger.by_wallet(OTHER) == []
        assert len(ledger.by_wallet(WALLET)) == 1
    finally:
        ledger.close()


_R1_SCHEMA = """
CREATE TABLE points_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    action TEXT NOT NULL,
    points INTEGER NOT NULL CHECK (typeof(points) = 'integer' AND points > 0),
    tx_hash TEXT NOT NULL,
    metadata TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (wallet, action, tx_hash)
);
CREATE INDEX idx_points_ledger_wallet ON points_ledger (wallet);
CREATE TABLE points_ledger_keys (
    wallet TEXT NOT NULL,
    action TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    PRIMARY KEY (wallet, action, tx_hash)
);
CREATE TRIGGER points_ledger_block_update
BEFORE UPDATE ON points_ledger
BEGIN
    SELECT RAISE(ABORT, 'points ledger is append-only');
END;
CREATE TRIGGER points_ledger_block_delete
BEFORE DELETE ON points_ledger
BEGIN
    SELECT RAISE(ABORT, 'points ledger is append-only');
END;
CREATE TRIGGER points_ledger_block_reinsert
BEFORE INSERT ON points_ledger
WHEN EXISTS (
    SELECT 1 FROM points_ledger_keys
    WHERE wallet = NEW.wallet AND action = NEW.action AND tx_hash = NEW.tx_hash
)
BEGIN
    SELECT RAISE(ABORT, 'points ledger is append-only');
END;
CREATE TRIGGER points_ledger_remember_key
AFTER INSERT ON points_ledger
BEGIN
    INSERT INTO points_ledger_keys (wallet, action, tx_hash)
    VALUES (NEW.wallet, NEW.action, NEW.tx_hash);
END;
CREATE TRIGGER points_ledger_keys_block_update
BEFORE UPDATE ON points_ledger_keys
BEGIN
    SELECT RAISE(ABORT, 'points ledger is append-only');
END;
CREATE TRIGGER points_ledger_keys_block_delete
BEFORE DELETE ON points_ledger_keys
BEGIN
    SELECT RAISE(ABORT, 'points ledger is append-only');
END;
"""


def _write_r1(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_R1_SCHEMA)
    return conn


def _r1_columns(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [str(row[1]) for row in conn.execute("PRAGMA table_info(points_ledger)")]
    finally:
        conn.close()


def test_r1_file_gains_a_utc_day(tmp_path: Path) -> None:
    path = tmp_path / "r1.sqlite"
    conn = _write_r1(path)
    try:
        conn.execute(
            """
            INSERT INTO points_ledger (wallet, action, points, tx_hash, metadata, created_at)
            VALUES (?, 'score', 5, 'once', ?, '2026-10-11T23:00:00Z')
            """,
            (WALLET, json.dumps({"utc_date": "2026-10-12", "issuer": "NVDA"})),
        )
        conn.execute(
            """
            INSERT INTO points_ledger (wallet, action, points, tx_hash, metadata, created_at)
            VALUES (?, 'share', 15, 'share-1', NULL, '2026-10-11T01:00:00Z')
            """,
            (WALLET,),
        )
        conn.commit()
    finally:
        conn.close()

    ledger = PointsLedger(path)
    try:
        rows = ledger.by_wallet(WALLET)
        assert [(row.action, row.points, row.utc_day) for row in rows] == [
            ("score", 5, "2026-10-12"),
            ("share", 15, "2026-10-11"),
        ]
        assert rows[0].metadata == {"issuer": "NVDA", "utc_date": "2026-10-12"}
        assert ledger._conn.execute("PRAGMA user_version").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            ledger._conn.execute("UPDATE points_ledger SET points = 1")
    finally:
        ledger.close()

    reopened = PointsLedger(path)
    try:
        assert reopened.totals()[0].points == 20
        assert reopened._conn.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        reopened.close()


def test_r1_migration_refuses_two_rows_on_the_same_day(tmp_path: Path) -> None:
    path = tmp_path / "r1.sqlite"
    conn = _write_r1(path)
    try:
        for tx_hash in ("a", "b"):
            conn.execute(
                """
                INSERT INTO points_ledger (wallet, action, points, tx_hash, metadata, created_at)
                VALUES (?, 'score', 5, ?, ?, '2026-10-12T01:00:00Z')
                """,
                (WALLET, tx_hash, json.dumps({"utc_date": "2026-10-12"})),
            )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="left unchanged"):
        PointsLedger(path)

    check = sqlite3.connect(path)
    try:
        columns = [str(row[1]) for row in check.execute("PRAGMA table_info(points_ledger)")]
        assert "utc_day" not in columns
        assert check.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] == 2
        assert check.execute("PRAGMA user_version").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            check.execute("UPDATE points_ledger SET points = 1")
    finally:
        check.close()


def test_r1_migration_refuses_points_above_500(tmp_path: Path) -> None:
    path = tmp_path / "r1.sqlite"
    conn = _write_r1(path)
    try:
        conn.execute(
            """
            INSERT INTO points_ledger (wallet, action, points, tx_hash, created_at)
            VALUES (?, 'bug', 600, 'big', '2026-10-12T01:00:00Z')
            """,
            (WALLET,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="points above 500"):
        PointsLedger(path)
    assert "utc_day" not in _r1_columns(path)
    check = sqlite3.connect(path)
    try:
        assert check.execute("SELECT points FROM points_ledger").fetchone()[0] == 600
    finally:
        check.close()
