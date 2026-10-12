"""Server-side points rubric. Callers do not set points, keys, or the day."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rwa_score.issuer_registry import AUDITED, FULLY_BACKED, ISSUER_NOTES, REDEEMABLE
from rwa_score.points import PointsLedger, synthetic_tx_key
from rwa_score.points.rubric import (
    ACTION_POINTS,
    BUG_SEVERITY_POINTS,
    DAILY_CAPS,
    PER_ROW_CAP,
    known_issuers,
    normalize_verified_tx_hash,
    points_for,
    record,
)

WALLET = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
DAY = datetime(2026, 10, 12, 18, 30, tzinfo=timezone.utc)
NEXT_DAY = datetime(2026, 10, 13, 0, 5, tzinfo=timezone.utc)
CHAIN = "0x" + "ab" * 32

ROOT = Path(__file__).resolve().parents[1]


def _open(tmp_path: Path) -> PointsLedger:
    return PointsLedger(tmp_path / "points.sqlite")


@pytest.mark.parametrize(
    ("action", "severity", "expected"),
    [
        ("score", None, 5),
        ("SCORE", None, 5),
        ("attest", None, 25),
        ("share", None, 15),
        ("integration", None, 100),
        ("bug", "low", 100),
        ("bug", "LOW", 100),
        ("bug", "medium", 250),
        ("bug", "high", 400),
        ("bug", "critical", 500),
    ],
)
def test_server_points_table(action: str, severity: str | None, expected: int) -> None:
    assert points_for(action, severity) == expected
    assert 1 <= expected <= PER_ROW_CAP
    if action.strip().lower() == "bug":
        assert 100 <= expected <= 500


def test_configured_awards_match_the_published_table() -> None:
    assert ACTION_POINTS == {
        "score": 5,
        "attest": 25,
        "share": 15,
        "integration": 100,
    }
    assert BUG_SEVERITY_POINTS["low"] == 100
    assert BUG_SEVERITY_POINTS["critical"] == 500
    assert min(BUG_SEVERITY_POINTS.values()) >= 100
    assert max(BUG_SEVERITY_POINTS.values()) <= 500
    assert max(ACTION_POINTS.values()) <= PER_ROW_CAP
    assert DAILY_CAPS["score"] == 1
    assert set(DAILY_CAPS) == {"score", "attest", "share", "integration", "bug"}


def test_record_does_not_accept_points_tx_hash_or_a_client_date() -> None:
    params = inspect.signature(record).parameters
    assert "points" not in params
    assert "tx_hash" not in params
    assert "date" not in params
    assert "on" not in params
    assert "utc_date" not in params


@pytest.mark.parametrize(
    ("action", "severity"),
    [
        ("referral", None),
        ("bug", None),
        ("bug", "info"),
        ("bug", "500"),
        ("bug", ""),
        ("score", "low"),
    ],
)
def test_unknown_action_or_severity_is_rejected(action: str, severity: str | None) -> None:
    with pytest.raises(ValueError):
        points_for(action, severity)


def test_per_row_cap_refuses_an_oversized_award(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ACTION_POINTS, "score", PER_ROW_CAP + 1)
    with pytest.raises(ValueError, match="per-row cap"):
        points_for("score")
    monkeypatch.setitem(BUG_SEVERITY_POINTS, "low", 50)
    with pytest.raises(ValueError, match="100 and 500"):
        points_for("bug", "low")


def test_oversized_award_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ACTION_POINTS, "score", PER_ROW_CAP + 1)
    ledger = _open(tmp_path)
    try:
        with pytest.raises(ValueError, match="per-row cap"):
            record(ledger, wallet=WALLET, action="score", issuer="Ondo", server_now=DAY)
        assert ledger.totals() == []
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "ab" * 32,
        "0x" + "gg" * 32,
        "0x" + "ab" * 31,
        "0x" + "ab" * 33,
        "0x" + "ab" * 32 + "zz",
        "rat-points-v1|" + "ab" * 32,
        "0x" + "ab" * 31 + "g" + "cd",
        "0x" + "ab" * 32 + "\n0",
        None,
        0,
    ],
)
def test_non_hex_tx_hashes_are_rejected(raw: object) -> None:
    with pytest.raises(ValueError, match="0x plus 64 hex"):
        normalize_verified_tx_hash(raw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0x" + "ab" * 32, "0x" + "ab" * 32),
        ("0x" + "AB" * 32, "0x" + "ab" * 32),
        ("  0X" + "aB" * 32 + "\t", "0x" + "ab" * 32),
    ],
)
def test_verified_hashes_are_lowercased(raw: str, expected: str) -> None:
    assert normalize_verified_tx_hash(raw) == expected


def test_known_issuers_come_from_the_registry() -> None:
    raw = set(FULLY_BACKED) | set(AUDITED) | set(REDEEMABLE) | set(ISSUER_NOTES)
    assert known_issuers() == frozenset(name.strip().lower() for name in raw)
    assert "ondo" in known_issuers()
    assert "nvda" not in known_issuers()
    assert "notevault demo issuer" not in known_issuers()


@pytest.mark.parametrize("issuer", sorted(known_issuers()))
def test_registry_issuers_can_be_recorded(tmp_path: Path, issuer: str) -> None:
    ledger = _open(tmp_path)
    try:
        result = record(
            ledger,
            wallet=WALLET,
            action="score",
            issuer=issuer.upper(),
            server_now=DAY,
        )
        assert result.status == "inserted"
        assert result.awarded == 5
        assert result.entry is not None
        assert result.entry.metadata is not None
        assert result.entry.metadata["issuer"] == issuer
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "issuer",
    ["", "   ", "NVDA", "AAPL", "NoteVault Demo Issuer", "Not Backed At All", "ACME", "unbacked"],
)
def test_unknown_issuers_are_rejected(tmp_path: Path, issuer: str) -> None:
    ledger = _open(tmp_path)
    try:
        with pytest.raises(ValueError, match="issuer"):
            record(ledger, wallet=WALLET, action="score", issuer=issuer, server_now=DAY)
        assert ledger.totals() == []
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "client",
    [
        {"points": 9999},
        {"Points": 1},
        {"tx_hash": CHAIN},
        {"txHash": CHAIN},
        {"TX-HASH": CHAIN},
        {"transaction_hash": CHAIN},
        {"verified_tx_hash": CHAIN},
    ],
)
def test_client_cannot_pass_points_or_tx_hash(tmp_path: Path, client: dict[str, object]) -> None:
    ledger = _open(tmp_path)
    try:
        with pytest.raises(ValueError, match="points|tx_hash"):
            record(
                ledger,
                wallet=WALLET,
                action="score",
                issuer="Ondo",
                server_now=DAY,
                client=client,
            )
        assert ledger.totals() == []
    finally:
        ledger.close()


def test_client_date_is_ignored(tmp_path: Path) -> None:
    eastern = timezone(timedelta(hours=-4))
    # 21:00 in a -4 zone is 01:00 the next UTC day.
    server = datetime(2026, 10, 12, 21, 0, tzinfo=eastern)
    ledger = _open(tmp_path)
    try:
        result = record(
            ledger,
            wallet=WALLET,
            action="score",
            issuer="  Backed-Finance  ",
            server_now=server,
            client={
                "date": "1999-01-01",
                "on": "2000-02-02",
                "utc_date": "2001-03-03",
                "created_at": "2002-04-04T00:00:00Z",
                "note": "ignore me",
            },
        )
        assert result.status == "inserted"
        assert result.entry is not None
        assert result.entry.metadata == {
            "issuer": "backed finance",
            "utc_date": "2026-10-13",
        }
        assert result.entry.tx_hash == synthetic_tx_key(
            WALLET, "score", "backed finance", on="2026-10-13"
        )
        assert result.entry.tx_hash.startswith("rat-points-v1|")
        stored = json.dumps(result.entry.metadata)
        assert "1999" not in stored
        assert "note" not in result.entry.metadata
    finally:
        ledger.close()


def test_same_issuer_does_not_multiply_and_score_cap_is_one(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    try:
        first = record(ledger, wallet=WALLET, action="score", issuer="Ondo", server_now=DAY)
        again = record(ledger, wallet=WALLET.upper(), action="SCORE", issuer="ondo", server_now=DAY)
        other_issuer = record(
            ledger, wallet=WALLET, action="score", issuer="Paxos", server_now=DAY
        )
        assert first.status == "inserted"
        assert first.awarded == 5
        assert again.status == "duplicate"
        assert again.awarded == 0
        assert again.entry is not None
        assert again.entry.id == first.entry.id  # type: ignore[union-attr]
        assert other_issuer.status == "capped"
        assert other_issuer.entry is None
        assert ledger.totals()[0].points == 5
        assert ledger.by_wallet(WALLET)[0].points == 5
    finally:
        ledger.close()


def test_same_issuer_stays_one_row_when_the_daily_cap_is_higher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(DAILY_CAPS, "score", 5)
    ledger = _open(tmp_path)
    try:
        record(ledger, wallet=WALLET, action="score", issuer="Ondo", server_now=DAY)
        second = record(ledger, wallet=WALLET, action="score", issuer="ONDO", server_now=DAY)
        third = record(ledger, wallet=WALLET, action="score", issuer="Dinari", server_now=DAY)
        assert second.status == "duplicate"
        assert third.status == "inserted"
        assert third.awarded == 5
        assert ledger.totals()[0].points == 10
    finally:
        ledger.close()


def test_daily_cap_is_per_wallet_action_and_utc_day(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    try:
        score = record(ledger, wallet=WALLET, action="score", issuer="Ondo", server_now=DAY)
        share = record(ledger, wallet=WALLET, action="share", issuer="Ondo", server_now=DAY)
        other = record(ledger, wallet=OTHER, action="score", issuer="Ondo", server_now=DAY)
        tomorrow = record(
            ledger, wallet=WALLET, action="score", issuer="Paxos", server_now=NEXT_DAY
        )
        assert score.status == "inserted"
        assert share.status == "inserted"
        assert share.awarded == 15
        assert other.status == "inserted"
        assert tomorrow.status == "inserted"
        totals = {row.wallet: row.points for row in ledger.totals()}
        assert totals[WALLET] == 5 + 15 + 5
        assert totals[OTHER] == 5
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("action", "severity", "expected"),
    [
        ("attest", None, 25),
        ("share", None, 15),
        ("integration", None, 100),
        ("bug", "critical", 500),
        ("bug", "low", 100),
    ],
)
def test_each_action_is_capped_per_wallet_per_day(
    tmp_path: Path, action: str, severity: str | None, expected: int
) -> None:
    ledger = _open(tmp_path)
    kwargs: dict[str, object] = {}
    if severity is not None:
        kwargs["severity"] = severity
    chain = "0x" + "11" * 32
    try:
        first = record(
            ledger,
            wallet=WALLET,
            action=action,
            issuer="Securitize",
            server_now=DAY,
            verified_tx_hash=chain if action == "attest" else None,
            **kwargs,  # type: ignore[arg-type]
        )
        second = record(
            ledger,
            wallet=WALLET,
            action=action,
            issuer="Robinhood",
            server_now=DAY,
            verified_tx_hash=("0x" + "22" * 32) if action == "attest" else None,
            **kwargs,  # type: ignore[arg-type]
        )
        assert first.status == "inserted"
        assert first.awarded == expected
        assert first.entry is not None
        assert first.entry.points == expected
        assert first.entry.points <= PER_ROW_CAP
        assert second.status == "capped"
        assert ledger.totals()[0].points == expected
    finally:
        ledger.close()


def test_attest_stores_the_lowercased_verified_hash_only(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    mixed = "0x" + "AB" * 32
    try:
        with pytest.raises(ValueError, match="verified on-chain"):
            record(ledger, wallet=WALLET, action="attest", issuer="Ondo", server_now=DAY)
        with pytest.raises(ValueError, match="0x plus 64 hex"):
            record(
                ledger,
                wallet=WALLET,
                action="attest",
                issuer="Ondo",
                verified_tx_hash="not-a-hash",
                server_now=DAY,
            )
        with pytest.raises(ValueError, match="not accepted"):
            record(
                ledger,
                wallet=WALLET,
                action="score",
                issuer="Ondo",
                verified_tx_hash=mixed,
                server_now=DAY,
            )
        inserted = record(
            ledger,
            wallet=WALLET,
            action="attest",
            issuer="xStocks",
            verified_tx_hash=mixed,
            server_now=DAY,
            client={"date": "1999-01-01"},
        )
        replay = record(
            ledger,
            wallet=WALLET,
            action="attest",
            issuer="Ondo",
            verified_tx_hash=mixed.lower(),
            server_now=DAY,
        )
        assert inserted.status == "inserted"
        assert inserted.awarded == 25
        assert inserted.entry is not None
        assert inserted.entry.tx_hash == mixed.lower()
        assert inserted.entry.points == 25
        assert not inserted.entry.tx_hash.startswith("rat-points-v1|")
        assert replay.status == "duplicate"
        assert replay.awarded == 0
        assert ledger.totals()[0].points == 25
    finally:
        ledger.close()


def test_bug_severity_sets_points_and_the_same_issuer_does_not_multiply(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    try:
        low = record(
            ledger,
            wallet=WALLET,
            action="bug",
            issuer="Robinhood",
            severity="high",
            server_now=DAY,
        )
        again = record(
            ledger,
            wallet=WALLET,
            action="bug",
            issuer="robinhood",
            severity="low",
            server_now=DAY,
        )
        assert low.status == "inserted"
        assert low.awarded == 400
        assert low.entry is not None
        assert low.entry.metadata is not None
        assert low.entry.metadata["severity"] == "high"
        assert again.status == "duplicate"
        assert again.entry is not None
        assert again.entry.points == 400
        assert ledger.totals()[0].points == 400
    finally:
        ledger.close()


def test_rubric_is_not_wired_into_api_share_or_streamlit() -> None:
    files = [ROOT / "app.py", ROOT / "rwa_score" / "share.py"]
    api = ROOT / "rwa_score" / "api"
    files.extend(sorted(api.rglob("*.py")))
    indexer = ROOT / "rwa_score" / "indexer"
    if indexer.exists():
        files.extend(sorted(indexer.rglob("*.py")))
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "points.rubric" not in text
        assert "points import record" not in text
