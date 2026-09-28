"""Attestation hash is stable and changes when the cited breakdown is edited."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from rwa_score.api.attest import (
    ATTESTATION_FIELDS,
    PINNED_ATTESTATION_CONTRACT,
    attestation_payload,
    canonical_bytes,
    clear_scorer_version_cache,
    hash_canonical,
    score_hash,
)
from rwa_score.api.store import Store
from rwa_score.api.verify import (
    EXIT_CAST_MISSING,
    EXIT_HASH_MISMATCH,
    EXIT_NO_MATCH,
    EXIT_NOT_STORED,
    EXIT_RPC_ERROR,
    main,
)
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


def test_scorer_version_fallback_order(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """RENDER_GIT_COMMIT, then one `git rev-parse HEAD`, then unknown. Cached."""
    report = {"ticker": "NVDA", "data_source": "fixture"}
    seen: list[list[str]] = []

    def git_ok(args, **_kwargs):
        seen.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout=("f" * 40) + "\n", stderr="")

    monkeypatch.setattr("rwa_score.api.attest.subprocess.run", git_ok)
    monkeypatch.setenv("RENDER_GIT_COMMIT", "a" * 40)
    monkeypatch.setenv("SOURCE_VERSION", "should-not-be-used")
    monkeypatch.setenv("GIT_COMMIT", "should-not-be-used")
    clear_scorer_version_cache()
    assert attestation_payload(report)["scorer_version"] == "a" * 40
    assert attestation_payload(report)["scorer_version"] == "a" * 40
    assert seen == []

    clear_scorer_version_cache()
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    assert attestation_payload(report)["scorer_version"] == "f" * 40
    assert attestation_payload(report)["scorer_version"] == "f" * 40
    assert seen == [["git", "rev-parse", "HEAD"]]

    clear_scorer_version_cache()
    monkeypatch.setenv("RENDER_GIT_COMMIT", "   ")
    assert attestation_payload(report)["scorer_version"] == "f" * 40
    assert seen == [["git", "rev-parse", "HEAD"], ["git", "rev-parse", "HEAD"]]
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)

    def git_missing(args, **_kwargs):
        seen.append(list(args))
        raise FileNotFoundError("git")

    clear_scorer_version_cache()
    monkeypatch.setattr("rwa_score.api.attest.subprocess.run", git_missing)
    with caplog.at_level("WARNING"):
        assert attestation_payload(report)["scorer_version"] == "unknown"
        assert attestation_payload(report)["scorer_version"] == "unknown"
    assert "scorer_version is unknown" in caplog.text
    assert seen.count(["git", "rev-parse", "HEAD"]) == 3

    def git_fails(args, **_kwargs):
        seen.append(list(args))
        return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: not a git repository")

    clear_scorer_version_cache()
    monkeypatch.setattr("rwa_score.api.attest.subprocess.run", git_fails)
    assert attestation_payload(report)["scorer_version"] == "unknown"
    assert seen[-1] == ["git", "rev-parse", "HEAD"]


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


def test_canonical_bytes_reject_nan_and_infinity() -> None:
    payload = _base_payload()
    payload["score"] = float("nan")
    with pytest.raises(ValueError):
        canonical_bytes(payload)
    payload["score"] = float("inf")
    with pytest.raises(ValueError):
        canonical_bytes(payload)


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


def _cast_outputs(chain_id: str = "84532", verify: str = CAST_183_VERIFY):
    def run(args, **_kwargs):
        cmd = args[1] if len(args) > 1 else ""
        if cmd == "chain-id":
            return chain_id + "\n"
        if cmd == "call":
            return verify
        raise AssertionError(cmd)

    return run


def test_verify_json_parses_cast_183_attested_at(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    db = _seed(tmp_path)
    monkeypatch.setattr("rwa_score.api.verify.shutil.which", lambda _name: "/usr/bin/cast")
    monkeypatch.setattr("rwa_score.api.verify.subprocess.check_output", _cast_outputs())
    attester = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
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
            "--attester",
            attester,
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["on_chain"]["attested_at"] == 1700000000
    assert isinstance(payload["on_chain"]["attested_at"], int)
    assert "1700000000 [1.7e9]" in payload["on_chain"]["raw"]
    assert payload["on_chain"]["chain_id"] == 84532
    assert payload["on_chain"]["attester_ok"] is True
    assert payload["match"] is True
    assert payload["stored"] is True
    assert payload["hash_ok"] is True


def test_verify_does_not_call_the_scorer(monkeypatch, capsys, tmp_path: Path) -> None:
    db = _seed(tmp_path)
    score = Mock(side_effect=AssertionError("scorer called"))
    monkeypatch.setattr("rwa_score.scorer.TransparencyScorer.score", score)
    code = main(["NVDA", "--fixtures", "--api-url", "http://example", "--api-key", "rat_x", "--json", "--db", str(db)])
    assert code == 0
    score.assert_not_called()
    captured = capsys.readouterr()
    assert "obsolete" in captured.err.lower()
    assert "do not re-score" in captured.err.lower()
    payload = json.loads(captured.out)
    assert payload["stored"] is True
    assert payload["score_hash"].startswith("0x")
    assert "no live re-score" in payload["note"].lower()
    assert "obsolete" in payload["note"].lower()


def test_verify_clear_when_nothing_stored(capsys, tmp_path: Path) -> None:
    db = tmp_path / "empty.sqlite"
    Store(db).close()
    code = main(["NVDA", "--json", "--db", str(db)])
    assert code == EXIT_NOT_STORED
    payload = json.loads(capsys.readouterr().out)
    assert payload["stored"] is False
    assert payload["score_hash"] is None
    assert payload["on_chain"] is None
    assert payload["match"] is False
    assert "does not re-score" in payload["note"]
    assert "No stored attestation payload" in payload["note"]


def test_verify_tampered_bytes_exit_nonzero(capsys, tmp_path: Path) -> None:
    import sqlite3

    db = _seed(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE attested_payloads SET canonical_json = ? WHERE ticker = ?",
        (b'{"ticker":"NVDA","score":1}', "NVDA"),
    )
    conn.commit()
    conn.close()
    code = main(["NVDA", "--json", "--db", str(db)])
    assert code == EXIT_HASH_MISMATCH
    payload = json.loads(capsys.readouterr().out)
    assert payload["stored"] is True
    assert payload["hash_ok"] is False
    assert payload["match"] is False


def test_verify_refuses_wrong_chain_and_attester(monkeypatch, capsys, tmp_path: Path) -> None:
    db = _seed(tmp_path)
    monkeypatch.setattr("rwa_score.api.verify.shutil.which", lambda _name: "/usr/bin/cast")
    monkeypatch.setattr(
        "rwa_score.api.verify.subprocess.check_output",
        _cast_outputs(chain_id="1"),
    )
    code = main(
        ["NVDA", "--json", "--db", str(db), "--rpc-url", "http://127.0.0.1:8545", "--attester", "0x" + "11" * 20]
    )
    assert code == EXIT_NO_MATCH
    body = json.loads(capsys.readouterr().out)
    assert body["on_chain"]["chain_id"] == 1
    assert body["match"] is False
    assert "error" not in body["on_chain"]

    monkeypatch.setattr("rwa_score.api.verify.subprocess.check_output", _cast_outputs())
    code = main(
        ["NVDA", "--json", "--db", str(db), "--rpc-url", "http://127.0.0.1:8545", "--attester", "0x" + "22" * 20]
    )
    assert code == EXIT_NO_MATCH
    body = json.loads(capsys.readouterr().out)
    assert body["on_chain"]["chain_id"] == 84532
    assert body["on_chain"]["attester_ok"] is False
    assert body["match"] is False


def test_verify_rpc_error_and_missing_cast(monkeypatch, capsys, tmp_path: Path) -> None:
    db = _seed(tmp_path)
    monkeypatch.setattr("rwa_score.api.verify.shutil.which", lambda _name: None)
    code = main(["NVDA", "--json", "--db", str(db), "--rpc-url", "http://127.0.0.1:8545"])
    assert code == EXIT_CAST_MISSING
    body = json.loads(capsys.readouterr().out)
    assert body["on_chain"]["error"] == "cast_missing"

    def boom(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["cast"], output="connection refused http://127.0.0.1:8545")

    monkeypatch.setattr("rwa_score.api.verify.shutil.which", lambda _name: "/usr/bin/cast")
    monkeypatch.setattr("rwa_score.api.verify.subprocess.check_output", boom)
    code = main(["NVDA", "--json", "--db", str(db), "--rpc-url", "http://127.0.0.1:8545"])
    assert code == EXIT_RPC_ERROR
    body = json.loads(capsys.readouterr().out)
    assert body["match"] is False
    assert "8545" not in body["on_chain"]["error"]
    assert "[rpc]" in body["on_chain"]["error"]


def test_verify_contract_defaults_to_pinned(monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.delenv("RWA_ATTESTATION_CONTRACT", raising=False)
    db = tmp_path / "empty.sqlite"
    Store(db).close()
    code = main(["NVDA", "--json", "--db", str(db)])
    assert code == EXIT_NOT_STORED
    body = json.loads(capsys.readouterr().out)
    assert body["contract"] == PINNED_ATTESTATION_CONTRACT
    assert body["contract"] == "0x2F073a3628D498d92956e7eFE2b26633eDa75b00"
