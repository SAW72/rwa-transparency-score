"""Server-side points rubric. Unpaid activity counters.

There is no token. Points have no cash value, and nothing here promises a
later payment. The caller names an action. This module sets the points.
A caller-supplied points value is refused. A caller-supplied ``tx_hash``
is refused. The UTC day is the server clock.

Not wired into the API, the share flow, or Streamlit.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from rwa_score.issuer_registry import AUDITED, FULLY_BACKED, ISSUER_NOTES, REDEEMABLE

from .ledger import (
    ACTIONS,
    SYNTHETIC_KEY_PREFIX,
    LedgerEntry,
    PointsLedger,
    normalize_wallet,
    synthetic_tx_key,
)

# Fixed awards. Bug is not in this map: its amount comes from severity.
ACTION_POINTS: dict[str, int] = {
    "score": 5,
    "attest": 25,
    "share": 15,
    "integration": 100,
}

# Server scale inside the published 100–500 band. The caller passes a label.
BUG_SEVERITY_POINTS: dict[str, int] = {
    "low": 100,
    "medium": 250,
    "high": 400,
    "critical": 500,
}

# No single row can store more than this. Bug critical sits on the cap.
PER_ROW_CAP = 500

# New rows for one wallet and one action on one UTC day.
# Score is one: a second score that day does not count, whatever the issuer.
DAILY_CAPS: dict[str, int] = {
    "score": 1,
    "attest": 1,
    "share": 1,
    "integration": 1,
    "bug": 1,
}

_ONCHAIN_TX = re.compile(r"^0x[0-9a-f]{64}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ISSUER_FOLD = re.compile(r"[\s_\-]+")
_CLIENT_KEY_FOLD = re.compile(r"[\s_\-]+")

RecordStatus = Literal["inserted", "duplicate", "capped"]


@dataclass(frozen=True)
class RecordResult:
    """``awarded`` is 0 unless ``status`` is ``inserted``.

    ``capped`` means the wallet already used its rows for that action on
    the server's UTC day. ``duplicate`` is an existing row (same issuer, or
    the same verified hash) and does not add points.
    """

    status: RecordStatus
    entry: LedgerEntry | None
    awarded: int


def known_issuers() -> frozenset[str]:
    """Folded names from ``issuer_registry``. That module is the allowlist."""
    raw = set(FULLY_BACKED) | set(AUDITED) | set(REDEEMABLE) | set(ISSUER_NOTES)
    return frozenset(_fold_issuer(name) for name in raw)


def normalize_verified_tx_hash(value: str) -> str:
    """Return a verified on-chain hash in lowercase, or raise ``ValueError``.

    The only accepted form is ``0x`` plus 64 hex characters. Mixed case is
    folded to lowercase. A bare digest, a non-hex string, and a synthetic
    key are rejected.
    """
    if not isinstance(value, str):
        raise ValueError("on-chain tx hash must be 0x plus 64 hex characters")
    text = value.strip().lower()
    if not _ONCHAIN_TX.fullmatch(text):
        raise ValueError("on-chain tx hash must be 0x plus 64 hex characters")
    return text


def points_for(action: str, severity: str | None = None) -> int:
    """Points for one action. The amount comes from the tables above."""
    name = _require_action(action)
    if name == "bug":
        if not isinstance(severity, str):
            raise ValueError("bug severity is required")
        label = severity.strip().lower()
        if label not in BUG_SEVERITY_POINTS:
            shown = label[:40]
            raise ValueError(f"unknown bug severity: {shown!r}")
        amount = BUG_SEVERITY_POINTS[label]
    else:
        if severity is not None:
            raise ValueError("severity applies only to bug")
        amount = ACTION_POINTS[name]
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
        raise ValueError("points must be a positive integer")
    if amount > PER_ROW_CAP:
        raise ValueError("points exceed the per-row cap")
    if name == "bug" and not 100 <= amount <= 500:
        raise ValueError("bug points must be between 100 and 500")
    return amount


def record(
    ledger: PointsLedger,
    *,
    wallet: str,
    action: str,
    issuer: str,
    verified_tx_hash: str | None = None,
    severity: str | None = None,
    client: Mapping[str, Any] | None = None,
    server_now: datetime | None = None,
) -> RecordResult:
    """Append one rubric row, or return duplicate / capped without adding.

    ``server_now`` is the process clock (tests inject it). A date inside
    ``client`` is ignored. ``client`` cannot carry points or a tx hash.
    ``verified_tx_hash`` is accepted only for ``attest``, and only when it
    is ``0x`` plus 64 hex characters. Every other action uses
    ``synthetic_tx_key``.
    """
    _refuse_client_points_and_tx_hash(client)
    name = _require_action(action)
    amount = points_for(name, severity)
    canonical_issuer = _require_known_issuer(issuer)
    day = _server_utc_day(server_now)
    normalized = normalize_wallet(wallet)
    on_chain = name == "attest"
    if on_chain:
        if verified_tx_hash is None:
            raise ValueError("attest requires a verified on-chain tx hash")
        key = normalize_verified_tx_hash(verified_tx_hash)
    else:
        if verified_tx_hash is not None:
            raise ValueError("tx hash is not accepted for this action")
        key = synthetic_tx_key(normalized, name, canonical_issuer, on=day)
        _require_server_key(key)
    metadata: dict[str, Any] = {"issuer": canonical_issuer, "utc_date": day}
    if name == "bug" and isinstance(severity, str):
        metadata["severity"] = severity.strip().lower()
    # Hold the ledger lock across the cap check and the insert. append takes
    # the same lock, which is an RLock.
    with ledger._lock:
        rows = ledger.by_wallet(normalized)
        existing = _row_for_key(rows, name, key)
        if existing is not None:
            return RecordResult(status="duplicate", entry=existing, awarded=0)
        if _rows_on_day(rows, name, day) >= DAILY_CAPS[name]:
            return RecordResult(status="capped", entry=None, awarded=0)
        appended = ledger.append(
            wallet=normalized,
            action=name,
            points=amount,
            tx_hash=key,
            metadata=metadata,
        )
    if appended.status == "duplicate":
        return RecordResult(status="duplicate", entry=appended.entry, awarded=0)
    if appended.entry.points != amount or appended.entry.points > PER_ROW_CAP:
        raise RuntimeError("stored points did not match the rubric")
    return RecordResult(
        status="inserted",
        entry=appended.entry,
        awarded=appended.entry.points,
    )


def _require_action(action: str) -> str:
    if not isinstance(action, str):
        raise ValueError("unknown action")
    name = action.strip().lower()
    if name not in ACTIONS:
        shown = name[:40]
        raise ValueError(f"unknown action: {shown!r}")
    return name


def _fold_issuer(issuer: str) -> str:
    if not isinstance(issuer, str):
        raise ValueError("issuer is required")
    text = _ISSUER_FOLD.sub(" ", issuer.strip().lower()).strip()
    if not text:
        raise ValueError("issuer is required")
    return text


def _require_known_issuer(issuer: str) -> str:
    folded = _fold_issuer(issuer)
    if folded not in known_issuers():
        raise ValueError("unknown issuer")
    return folded


def _server_utc_day(server_now: datetime | None) -> str:
    if server_now is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(server_now, datetime):
        if server_now.tzinfo is None:
            moment = server_now.replace(tzinfo=timezone.utc)
        else:
            moment = server_now
    else:
        raise ValueError("server clock must be a datetime")
    return moment.astimezone(timezone.utc).date().isoformat()


def _refuse_client_points_and_tx_hash(client: Mapping[str, Any] | None) -> None:
    """Dates in ``client`` are not read. Points and tx hashes are refused."""
    if client is None:
        return
    if not isinstance(client, Mapping) or isinstance(client, (str, bytes)):
        raise ValueError("client fields must be an object")
    for key in client:
        if not isinstance(key, str):
            raise ValueError("client fields must be an object")
        folded = _CLIENT_KEY_FOLD.sub("", key.strip().lower())
        if folded == "points":
            raise ValueError("points are set by the server")
        if folded in {"txhash", "transactionhash", "verifiedtxhash"}:
            raise ValueError("tx_hash is not accepted from the client")


def _require_server_key(key: str) -> None:
    digest = key[len(SYNTHETIC_KEY_PREFIX) :]
    if not key.startswith(SYNTHETIC_KEY_PREFIX) or not _HEX64.fullmatch(digest):
        raise ValueError("synthetic key was not built by the server")


def _row_for_key(rows: list[LedgerEntry], action: str, key: str) -> LedgerEntry | None:
    for row in rows:
        if row.action == action and row.tx_hash == key:
            return row
    return None


def _rows_on_day(rows: list[LedgerEntry], action: str, day: str) -> int:
    count = 0
    for row in rows:
        if row.action != action:
            continue
        meta = row.metadata or {}
        if meta.get("utc_date") == day:
            count += 1
    return count
