"""Attestation hash is stable and changes when the cited breakdown is edited."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from rwa_score.api.attest import (
    ATTESTATION_FIELDS,
    attestation_payload,
    canonical_bytes,
    hash_canonical,
    score_hash,
)
from rwa_score.api.store import Store
from rwa_score.api.verify import main
from rwa_score.scorer import TransparencyScorer

# cast 1.8.3 prints a uint256 as the integer plus a bracketed decimal.
CAST_183_VERIFY = (
    "true\n"
    "1700000000 [1.7e9]\n"
    "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266\n"
)


def _base_payload() -> dict:
    return {
        "as_of": 1_700_000_000,
        "band": "GREEN",
        "basis": {"available": True, "percent_spread": 1.25},
        "cik": "0001045810",
        "data_source": "fixture",
        "inputs_digest": "0x" + "ab" * 32,
        "issuer": "Backed Finance",
        "rwa_id": 2,
        "score": 90.4,
        "scorer_version": "unknown",
        "subscores": {
            "backing": 90.0,
            "basis": 97.9,
            "disclosure": 80.0,
            "price": 99.7,
            "redemption": 85.0,
            "reserves": 90.0,
        },
        "ticker": "NVDA",
        "verification": {
            "backing": {"level": "self-reported", "score": 90.0, "source": "heuristic_fallback"},
        },
        "weights": {
            "backing": 0.20,
            "basis": 0.15,
            "disclosure": 0.15,
            "price": 0.15,
            "redemption": 0.15,
            "reserves": 0.20,
        },
    }


_FIELD_MUTATIONS = [
    ("ticker", "TSLA"),
    ("rwa_id", 3),
    ("issuer", "Other Issuer"),
    ("score", 10.0),
    ("band", "RED"),
    ("subscores", {"backing": 1.0}),
    ("weights", {"backing": 1.0}),
    ("cik", "0000000001"),
    ("data_source", "live"),
    ("verification", {"backing": {"score": 1.0, "level": "x", "source": "y"}}),
    ("basis", {"available": False}),
    ("as_of", 1_700_000_001),
    ("scorer_version", "abcdef1"),
    ("inputs_digest", "0x" + "cd" * 32),
]


def test_hash_stable_across_two_scores(fixture_scorer: TransparencyScorer) -> None:
    a = fixture_scorer.score("NVDA")
    b = fixture_scorer.score("NVDA")
    assert score_hash(a) == score_hash(b)
    assert score_hash(a).startswith("0x")
    assert len(score_hash(a)) == 66


def test_edited_score_changes_hash(fixture_scorer: TransparencyScorer) -> None:
    report = fixture_scorer.score("NVDA")
    original = score_hash(report)
    tampered = dict(report)
    tampered["score"] = round(float(report["score"]) - 5, 1)
    assert score_hash(tampered) != original


def test_payload_is_the_breakdown_subset(fixture_scorer: TransparencyScorer) -> None:
    report = fixture_scorer.score("TSLA")
    payload = attestation_payload(report)
    assert set(payload) == set(ATTESTATION_FIELDS)
    assert "explanations" not in payload
    assert payload["subscores"] == report["subscores"]
    assert payload["weights"] == report["weights"]
    assert "basis" in payload["subscores"]
    assert "basis" in payload["weights"]
    assert "basis" in payload["verification"]
    assert payload["basis"] == report["basis"]
    assert payload["as_of"] == 0
    assert payload["scorer_version"]
    assert payload["inputs_digest"].startswith("0x")
    assert len(payload["inputs_digest"]) == 66


def test_basis_is_never_silently_dropped(fixture_scorer: TransparencyScorer) -> None:
    report = fixture_scorer.score("NVDA")
    assert "basis" in report["subscores"]
    assert "basis" in report["weights"]
    original = score_hash(report)
    stripped = dict(report)
    stripped["subscores"] = {k: v for k, v in report["subscores"].items() if k != "basis"}
    stripped["weights"] = {k: v for k, v in report["weights"].items() if k != "basis"}
    stripped["verification"] = {k: v for k, v in report["verification"].items() if k != "basis"}
    stripped["basis"] = None
    payload = attestation_payload(stripped)
    assert "basis" in payload["subscores"]
    assert "basis" in payload["weights"]
    assert "basis" in payload["verification"]
    assert score_hash(stripped) != original


def test_hash_stable_for_stored_payload_regardless_of_key_order() -> None:
    payload = _base_payload()
    raw = canonical_bytes(payload)
    parsed = json.loads(raw.decode("utf-8"))
    reordered = {key: parsed[key] for key in reversed(list(parsed))}
    nested = dict(reordered)
    nested["subscores"] = {key: parsed["subscores"][key] for key in reversed(list(parsed["subscores"]))}
    assert canonical_bytes(reordered) == raw
    assert canonical_bytes(nested) == raw
    assert hash_canonical(canonical_bytes(reordered)) == hash_canonical(raw)
    assert hash_canonical(canonical_bytes(nested)) == hash_canonical(raw)


@pytest.mark.parametrize(("field", "new_value"), _FIELD_MUTATIONS)
def test_hash_changes_when_any_field_changes(field: str, new_value: object) -> None:
    original = _base_payload()
    assert field in ATTESTATION_FIELDS
    mutated = dict(original)
    mutated[field] = new_value
    assert mutated[field] != original[field]
    assert hash_canonical(canonical_bytes(mutated)) != hash_canonical(canonical_bytes(original))


def test_stored_bytes_round_trip(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    report = fixture_scorer.score("NVDA")
    raw = canonical_bytes(attestation_payload(report))
    store = Store(tmp_path / "payloads.sqlite")
    digest = store.save_attested_payload(ticker="NVDA", canonical=raw)
    loaded = store.get_attested_payload(digest)
    assert loaded is not None
    assert loaded["canonical"] == raw
    assert hash_canonical(loaded["canonical"]) == digest
    assert hash_canonical(loaded["canonical"]) == score_hash(report)
    store.close()


def _seed(tmp_path: Path, ticker: str = "NVDA") -> Path:
    payload = _base_payload()
    payload["ticker"] = ticker
    raw = canonical_bytes(payload)
    db = tmp_path / "verify.sqlite"
    store = Store(db)
    store.save_attested_payload(ticker=ticker, canonical=raw)
    store.close()
    return db


def test_verify_json_parses_cast_183_attested_at(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    db = _seed(tmp_path)
    monkeypatch.setattr("rwa_score.api.verify.shutil.which", lambda _name: "/usr/bin/cast")
    monkeypatch.setattr(
        "rwa_score.api.verify.subprocess.check_output",
        lambda *_args, **_kwargs: CAST_183_VERIFY,
    )
    code = main(
        [
            "NVDA",
            "--json",
            "--db",
            str(db),
            "--contract",
            "0x5FbDB2315678afecb367f032d93F642f64180aa3",
            "--rpc-url",
            "http://127.0.0.1:8545",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["on_chain"]["attested_at"] == 1700000000
    assert isinstance(payload["on_chain"]["attested_at"], int)
    assert "1700000000 [1.7e9]" in payload["on_chain"]["raw"]
    assert payload["stored"] is True
    assert payload["hash_ok"] is True


def test_verify_does_not_call_the_scorer(monkeypatch, capsys, tmp_path: Path) -> None:
    db = _seed(tmp_path)
    score = Mock(side_effect=AssertionError("scorer called"))
    monkeypatch.setattr("rwa_score.scorer.TransparencyScorer.score", score)
    code = main(["NVDA", "--fixtures", "--json", "--db", str(db)])
    assert code == 0
    score.assert_not_called()
    payload = json.loads(capsys.readouterr().out)
    assert payload["stored"] is True
    assert payload["score_hash"].startswith("0x")
    assert "no live re-score" in payload["note"].lower()


def test_verify_clear_when_nothing_stored(capsys, tmp_path: Path) -> None:
    db = tmp_path / "empty.sqlite"
    Store(db).close()
    code = main(["NVDA", "--json", "--db", str(db)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stored"] is False
    assert payload["score_hash"] is None
    assert payload["on_chain"] is None
    assert "does not re-score" in payload["note"]
    assert "No stored attestation payload" in payload["note"]
