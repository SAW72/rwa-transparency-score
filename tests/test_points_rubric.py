"""Server-side points rubric. Callers do not set points, keys, or the day."""

from __future__ import annotations

import inspect
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
    tx_hash_format_ok,
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
    with pytest.raises(ValueError, match=r"\^0x\[0-9a-f\]\{64\}"):
        tx_hash_format_ok(raw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "raw",
    [
        "0x" + "AB" * 32,
        "0X" + "ab" * 32,
        "0X" + "AB" * 32,
        "  0x" + "ab" * 32,
        "0x" + "ab" * 32 + " ",
        "\t0x" + "ab" * 32,
        "0x" + "ab" * 32 + "\n",
        "  0X" + "aB" * 32 + "\t",
    ],
)
def test_hashes_with_prefix_case_or_padding_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match=r"\^0x\[0-9a-f\]\{64\}"):
        tx_hash_format_ok(raw)


def test_exact_lowercase_hash_is_accepted() -> None:
    raw = "0x" + "ab" * 32
    assert tx_hash_format_ok(raw) == raw


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
        {"amount": 1},
        {"created_at": "2002-04-04T00:00:00Z"},
        {"key": "rat-points-v1|once"},
        {"utc_date": "2001-03-03"},
        {"date": "1999-01-01"},
        {"metadata": {"points": 9}},
        {"metadata.points": 9},
        {"note": "ignore me"},
    ],
)
def test_unknown_client_fields_are_rejected(tmp_path: Path, client: dict[str, object]) -> None:
    ledger = _open(tmp_path)
    try:
        with pytest.raises(ValueError, match="unknown client field"):
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


def test_server_clock_sets_the_utc_day(tmp_path: Path) -> None:
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
            client=None,
        )
        empty = record(
            ledger,
            wallet=WALLET,
            action="share",
            issuer="Backed Finance",
            server_now=server,
            client={},
        )
        assert result.status == "inserted"
        assert result.entry is not None
        assert result.entry.utc_day == "2026-10-13"
        assert result.entry.metadata == {
            "issuer": "backed finance",
            "utc_date": "2026-10-13",
        }
        assert result.entry.tx_hash == synthetic_tx_key(
            WALLET, "score", "backed finance", on="2026-10-13"
        )
        assert result.entry.tx_hash.startswith("rat-points-v1|")
        assert empty.status == "inserted"
        assert empty.entry is not None
        assert empty.entry.utc_day == "2026-10-13"
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


def test_database_unique_day_caps_even_if_the_memory_cap_is_higher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same-issuer repeats are moot under the per-day cap: the second issuer
    # has a different synthetic key and still gets no row.
    monkeypatch.setitem(DAILY_CAPS, "score", 5)
    ledger = _open(tmp_path)
    try:
        record(ledger, wallet=WALLET, action="score", issuer="Ondo", server_now=DAY)
        second = record(ledger, wallet=WALLET, action="score", issuer="ONDO", server_now=DAY)
        third = record(ledger, wallet=WALLET, action="score", issuer="Dinari", server_now=DAY)
        assert second.status == "duplicate"
        assert third.status == "capped"
        assert third.awarded == 0
        assert ledger.totals()[0].points == 5
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
            tx_hash_format_ok=chain if action == "attest" else None,
            **kwargs,  # type: ignore[arg-type]
        )
        second = record(
            ledger,
            wallet=WALLET,
            action=action,
            issuer="Robinhood",
            server_now=DAY,
            tx_hash_format_ok=("0x" + "22" * 32) if action == "attest" else None,
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


def test_attest_stores_only_an_exact_lowercase_hash(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    exact = "0x" + "ab" * 32
    try:
        with pytest.raises(ValueError, match="verified on-chain"):
            record(ledger, wallet=WALLET, action="attest", issuer="Ondo", server_now=DAY)
        with pytest.raises(ValueError, match=r"\^0x\[0-9a-f\]\{64\}"):
            record(
                ledger,
                wallet=WALLET,
                action="attest",
                issuer="Ondo",
                tx_hash_format_ok="not-a-hash",
                server_now=DAY,
            )
        for raw in ("0x" + "AB" * 32, "0X" + "ab" * 32, " " + exact, exact + "\n"):
            with pytest.raises(ValueError, match=r"\^0x\[0-9a-f\]\{64\}"):
                record(
                    ledger,
                    wallet=WALLET,
                    action="attest",
                    issuer="Ondo",
                    tx_hash_format_ok=raw,
                    server_now=DAY,
                )
        with pytest.raises(ValueError, match="not accepted"):
            record(
                ledger,
                wallet=WALLET,
                action="score",
                issuer="Ondo",
                tx_hash_format_ok=exact,
                server_now=DAY,
            )
        inserted = record(
            ledger,
            wallet=WALLET,
            action="attest",
            issuer="xStocks",
            tx_hash_format_ok=exact,
            server_now=DAY,
        )
        replay = record(
            ledger,
            wallet=WALLET,
            action="attest",
            issuer="Ondo",
            tx_hash_format_ok=exact,
            server_now=DAY,
        )
        assert inserted.status == "inserted"
        assert inserted.awarded == 25
        assert inserted.entry is not None
        assert inserted.entry.tx_hash == exact
        assert inserted.entry.utc_day == "2026-10-12"
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


_RACE_ISSUERS = (
    "Ondo",
    "Paxos",
    "Securitize",
    "xStocks",
    "Dinari",
    "Robinhood",
    "Backed Finance",
    "Ondo",
)


def _race_record(path: str, wallet: str, issuer: str, stamp: str, queue: object) -> None:
    """One process in the daily-cap race. Top-level so spawn can import it."""
    from datetime import datetime

    from rwa_score.points import PointsLedger
    from rwa_score.points.rubric import record as record_row

    ledger = PointsLedger(path)
    try:
        result = record_row(
            ledger,
            wallet=wallet,
            action="score",
            issuer=issuer,
            server_now=datetime.fromisoformat(stamp),
        )
        queue.put(result.status)  # type: ignore[attr-defined]
    except Exception as exc:
        queue.put(f"error:{type(exc).__name__}:{exc}")  # type: ignore[attr-defined]
    finally:
        ledger.close()


def test_daily_cap_is_one_row_across_processes(tmp_path: Path) -> None:
    import multiprocessing

    path = tmp_path / "race.sqlite"
    prepared = PointsLedger(path)
    prepared.close()
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=_race_record,
            args=(str(path), WALLET, issuer, DAY.isoformat(), queue),
        )
        for issuer in _RACE_ISSUERS
    ]
    for proc in processes:
        proc.start()
    statuses: list[object] = []
    for proc in processes:
        proc.join(60)
        assert proc.exitcode == 0
    for _ in processes:
        statuses.append(queue.get(timeout=10))
    assert statuses.count("inserted") == 1
    assert set(statuses) <= {"inserted", "capped", "duplicate"}
    reopened = PointsLedger(path)
    try:
        rows = reopened.by_wallet(WALLET)
        assert len(rows) == 1
        assert rows[0].action == "score"
        assert rows[0].points == 5
        assert rows[0].utc_day == "2026-10-12"
        assert reopened.totals()[0].points == 5
    finally:
        reopened.close()


def test_attest_hash_is_single_use_across_wallets(tmp_path: Path) -> None:
    """One attest hash can be stored once, across every wallet.

    Comparing that hash to an on-chain receipt is R3, before routes.
    """
    ledger = _open(tmp_path)
    chain = "0x" + "ab" * 32
    other_chain = "0x" + "cd" * 32
    try:
        first = record(
            ledger,
            wallet=WALLET,
            action="attest",
            issuer="Ondo",
            tx_hash_format_ok=chain,
            server_now=DAY,
        )
        reused = record(
            ledger,
            wallet=OTHER,
            action="attest",
            issuer="Paxos",
            tx_hash_format_ok=chain,
            server_now=NEXT_DAY,
        )
        fresh = record(
            ledger,
            wallet=OTHER,
            action="attest",
            issuer="Paxos",
            tx_hash_format_ok=other_chain,
            server_now=NEXT_DAY,
        )
        assert first.status == "inserted"
        assert first.awarded == 25
        assert reused.status == "rejected"
        assert reused.awarded == 0
        assert reused.entry is None
        assert fresh.status == "inserted"
        assert fresh.awarded == 25
        assert [row.tx_hash for row in ledger.by_wallet(WALLET)] == [chain]
        assert [row.tx_hash for row in ledger.by_wallet(OTHER)] == [other_chain]
    finally:
        ledger.close()
