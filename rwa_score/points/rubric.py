"""Server-side points rubric. Unpaid activity counters.

There is no token. Points have no cash value, and nothing here promises a
later payment. The caller names an action. This module sets the points.
A caller-supplied points value is refused. A caller-supplied ``tx_hash``
is refused. The UTC day is the server clock.

Not wired into the API, the share flow, or Streamlit. The on-chain receipt
check for an attest hash is R3, before routes. This module checks format only.
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

# Placeholder amounts pending Spencer. The published band is what this
# table currently uses; the numbers are not final.
ACTION_POINTS: dict[str, int] = {
    "score": 5,
    "attest": 25,
    "share": 15,
    "integration": 100,
}

# Placeholder severity scale pending Spencer, inside the 100–500 band.
# The caller passes a label. The amount comes from this table.
BUG_SEVERITY_POINTS: dict[str, int] = {
    "low": 100,
    "medium": 250,
    "high": 400,
    "critical": 500,
}

# Placeholder per-row cap pending Spencer. The database CHECK matches it.
PER_ROW_CAP = 500

# Documents one row per wallet, action, and UTC day. Enforcement is
# UNIQUE(wallet, action, utc_day). Raising these numbers in memory does
# not admit a second row, and ``PointsLedger._append`` cannot either.
DAILY_CAPS: dict[str, int] = {
    "score": 1,
    "attest": 1,
    "share": 1,
    "integration": 1,
    "bug": 1,
}

# Empty on purpose. A client payload has no accepted fields.
CLIENT_PAYLOAD_FIELDS: frozenset[str] = frozenset()

_ONCHAIN_TX = re.compile(r"^0x[0-9a-f]{64}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ISSUER_FOLD = re.compile(r"[\s_\-]+")

RecordStatus = Literal["inserted", "duplicate", "capped", "rejected"]


@dataclass(frozen=True)
class RecordResult:
    """``awarded`` is 0 unless ``status`` is ``inserted``.

    ``capped`` means this wallet already has a row for that action on the
    server's UTC day. ``duplicate`` is the same issuer or the same hash for
    this wallet. ``rejected`` means the attest hash was already stored for
    another wallet. Same-issuer repeats are moot under the per-day cap: the
    day already allows one row, so a second issuer also gets no slot.
    """

    status: RecordStatus
    entry: LedgerEntry | None
    awarded: int


def known_issuers() -> frozenset[str]:
    """Folded names from ``issuer_registry``. That module is the allowlist."""
    raw = set(FULLY_BACKED) | set(AUDITED) | set(REDEEMABLE) | set(ISSUER_NOTES)
    return frozenset(_fold_issuer(name) for name in raw)


def tx_hash_format_ok(value: str) -> str:
    """Return ``value`` when it matches ``^0x[0-9a-f]{64}$`` exactly.

    This is a format check. A ``0X`` prefix, uppercase hex, and surrounding
    whitespace are rejected and are not rewritten. Comparing the hash to an
    on-chain receipt is R3, before routes.
    """
    return _accept_tx_hash_format(value)


def _accept_tx_hash_format(value: str) -> str:
    if not isinstance(value, str) or not _ONCHAIN_TX.fullmatch(value):
        raise ValueError("on-chain tx hash must match ^0x[0-9a-f]{64}$")
    return value


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
    tx_hash_format_ok: str | None = None,
    severity: str | None = None,
    client: Mapping[str, Any] | None = None,
    server_now: datetime | None = None,
) -> RecordResult:
    """Append one rubric row, or return duplicate, capped, or rejected.

    ``server_now`` is the process clock (tests inject it). It stays a
    public hook. ``client`` may only contain ``CLIENT_PAYLOAD_FIELDS``,
    which is empty, so any client field is rejected. The parameter
    ``tx_hash_format_ok`` is accepted only for ``attest``, and only when
    the string matches ``^0x[0-9a-f]{64}$``. That is format only. The
    on-chain receipt check is R3, before routes. One attest hash can be
    stored once, across every wallet. Every other action uses
    ``synthetic_tx_key``.

    The check and the insert share one ``BEGIN IMMEDIATE``.
    ``UNIQUE(wallet, action, utc_day)`` is the daily cap.
    """
    _reject_unknown_client_fields(client)
    name = _require_action(action)
    amount = points_for(name, severity)
    canonical_issuer = _require_known_issuer(issuer)
    day = _server_utc_day(server_now)
    normalized = normalize_wallet(wallet)
    supplied_hash = tx_hash_format_ok
    if name == "attest":
        if supplied_hash is None:
            raise ValueError("attest requires a verified on-chain tx hash")
        key = _accept_tx_hash_format(supplied_hash)
    else:
        if supplied_hash is not None:
            raise ValueError("tx hash is not accepted for this action")
        key = synthetic_tx_key(normalized, name, canonical_issuer, on=day)
        _require_server_key(key)
    metadata: dict[str, Any] = {"issuer": canonical_issuer, "utc_date": day}
    if name == "bug" and isinstance(severity, str):
        metadata["severity"] = severity.strip().lower()
    # Same-issuer repeats are moot under the per-day cap. A repeated issuer
    # matches (wallet, action, tx_hash) and comes back duplicate. A second
    # issuer has a different synthetic key, and UNIQUE(wallet, action, utc_day)
    # still allows only one row, so that issuer gets no slot either.
    stored = ledger._insert_activity(
        wallet=normalized,
        action=name,
        points=amount,
        tx_hash=key,
        metadata=metadata,
        utc_day=day,
    )
    if stored.status == "inserted":
        if stored.entry is None or stored.entry.points != amount or stored.entry.points > PER_ROW_CAP:
            raise RuntimeError("stored points did not match the rubric")
        return RecordResult(status="inserted", entry=stored.entry, awarded=stored.entry.points)
    return RecordResult(status=stored.status, entry=stored.entry, awarded=0)


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


def _reject_unknown_client_fields(client: Mapping[str, Any] | None) -> None:
    """Reject every client key. ``CLIENT_PAYLOAD_FIELDS`` is empty."""
    if client is None:
        return
    if not isinstance(client, Mapping) or isinstance(client, (str, bytes)):
        raise ValueError("client fields must be an object")
    _reject_client_mapping(client, "")


def _reject_client_mapping(mapping: Mapping[Any, Any], prefix: str) -> None:
    for key, value in mapping.items():
        if not isinstance(key, str):
            raise ValueError("unknown client field")
        path = f"{prefix}.{key}" if prefix else key
        if path not in CLIENT_PAYLOAD_FIELDS:
            raise ValueError(f"unknown client field: {path}")
        if isinstance(value, Mapping) and not isinstance(value, (str, bytes)):
            _reject_client_mapping(value, path)


def _require_server_key(key: str) -> None:
    digest = key[len(SYNTHETIC_KEY_PREFIX) :]
    if not key.startswith(SYNTHETIC_KEY_PREFIX) or not _HEX64.fullmatch(digest):
        raise ValueError("synthetic key was not built by the server")
