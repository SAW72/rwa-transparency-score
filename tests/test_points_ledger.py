"""Append-only points ledger. Duplicates and raw edits must not change totals."""

from __future__ import annotations

import hashlib
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
        first = ledger.append(
            wallet=WALLET,
            action="score",
            points=5,
            tx_hash=key,
            metadata={"issuer": "NVDA"},
        )
        assert first.status == "inserted"
        assert first.entry.points == 5
        assert first.entry.metadata == {"issuer": "NVDA"}

        again = ledger.append(
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

        chain = "0x" + "AB" * 32
        attested = ledger.append(wallet=WALLET, action="attest", points=25, tx_hash=chain)
        folded = ledger.append(wallet=WALLET, action="attest", points=25, tx_hash=chain.lower())
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
        inserted = ledger.append(wallet=WALLET, action="share", points=15, tx_hash="share-1")
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
                INSERT INTO points_ledger (wallet, action, points, tx_hash, created_at)
                VALUES (?, 'share', 99, 'share-1', '2026-01-01T00:00:00Z')
                ON CONFLICT (wallet, action, tx_hash) DO UPDATE SET points = 99
                """,
                (WALLET,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            other.execute(
                """
                INSERT OR REPLACE INTO points_ledger (
                    wallet, action, points, tx_hash, created_at
                ) VALUES (?, 'share', 99, 'share-1', '2026-01-01T00:00:00Z')
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
        result = ledger.append(wallet=f"  {mixed}  ", action="integration", points=100, tx_hash="int-1")
        prefixed = "0X" + "AB" * 20
        also = ledger.append(wallet=prefixed, action="score", points=5, tx_hash="score-1")
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
            ledger.append(wallet=wallet, action="score", points=5, tx_hash="once")
        assert ledger.totals() == []
    finally:
        ledger.close()


@pytest.mark.parametrize("points", [0, -1, -5, True, False, 1.5, "5"])
def test_non_positive_points_are_rejected(tmp_path: Path, points: object) -> None:
    ledger = _open(tmp_path)
    try:
        with pytest.raises(ValueError, match="points"):
            ledger.append(wallet=WALLET, action="bug", points=points, tx_hash="bug-1")  # type: ignore[arg-type]
        assert ledger.by_wallet(WALLET) == []
    finally:
        ledger.close()


def test_unknown_action_is_rejected(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    try:
        with pytest.raises(ValueError, match="unknown action"):
            ledger.append(wallet=WALLET, action="referral", points=5, tx_hash="nope")
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
        ledger.append(wallet=WALLET, action="score", points=5, tx_hash="once")
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
        ledger.append(wallet=WALLET, action="score", points=5, tx_hash="a")
        ledger.append(wallet=OTHER, action="bug", points=100, tx_hash="b")
        totals = ledger.totals()
        assert [row.wallet for row in totals] == [OTHER, WALLET]
        assert [row.points for row in totals] == [100, 5]
    finally:
        ledger.close()
