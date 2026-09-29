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
    attestation_inputs,
    attestation_payload,
    canonical_bytes,
    clear_scorer_version_cache,
    data_as_of,
    hash_canonical,
    inputs_bytes,
    inputs_digest,
    recompute_inputs_digest,
    score_hash,
)
from rwa_score.api.store import Store
from rwa_score.api.verify import (
    EXIT_CAST_MISSING,
    EXIT_CHAIN_UNCHECKED,
    EXIT_DB,
    EXIT_HASH_MISMATCH,
    EXIT_INPUTS_MISSING,
    EXIT_NO_MATCH,
    EXIT_NOT_STORED,
    EXIT_OK,
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
        "data_as_of": None,
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
    ("data_as_of", 1_700_000_050),
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
    assert payload["basis"]["percent_spread"] == report["basis"]["percent_spread"]
    assert payload["basis"]["wrapper_count"] == report["basis"]["wrapper_count"]
    assert "market_url" not in json.dumps(payload["basis"])
    assert payload["as_of"] == 0
    assert payload["data_as_of"] is None
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
    store = Store()
    digest = store.save_attested_payload(ticker="NVDA", canonical=raw)
    loaded = store.get_attested_payload(digest)
    assert loaded is not None
    assert loaded["canonical"] == raw
    assert hash_canonical(loaded["canonical"]) == digest
    assert hash_canonical(loaded["canonical"]) == score_hash(report)
    store.close()


def _write_bundle(
    path: Path,
    *,
    ticker: str,
    canonical: bytes,
    inputs: bytes | None,
    score_hash: str | None = None,
) -> Path:
    from rwa_score.api.verify import export_payload

    body = export_payload(
        ticker=ticker,
        score_hash=score_hash or hash_canonical(canonical),
        canonical=canonical,
        inputs=inputs,
    )
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _seed(tmp_path: Path, ticker: str = "NVDA") -> Path:
    payload = _base_payload()
    payload["ticker"] = ticker
    inputs = {"cmc": {"ticker": ticker}}
    payload["inputs_digest"] = recompute_inputs_digest(inputs)
    raw = canonical_bytes(payload)
    return _write_bundle(
        tmp_path / "verify.payload.json",
        ticker=ticker,
        canonical=raw,
        inputs=canonical_bytes(inputs),
    )


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
            "--payload-file",
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
    monkeypatch.delenv("BASE_SEPOLIA_RPC_URL", raising=False)
    db = _seed(tmp_path)
    score = Mock(side_effect=AssertionError("scorer called"))
    monkeypatch.setattr("rwa_score.scorer.TransparencyScorer.score", score)
    code = main(
        [
            "NVDA",
            "--fixtures",
            "--api-url",
            "http://example",
            "--api-key",
            "rat_x",
            "--offline",
            "--json",
            "--payload-file",
            str(db),
        ]
    )
    assert code == EXIT_OK
    score.assert_not_called()
    captured = capsys.readouterr()
    assert "obsolete" in captured.err.lower()
    assert "do not re-score" in captured.err.lower()
    payload = json.loads(captured.out)
    assert payload["stored"] is True
    assert payload["score_hash"].startswith("0x")
    assert "no live re-score" in payload["note"].lower()
    assert "obsolete" in payload["note"].lower()


def test_verify_clear_when_nothing_stored(capsys) -> None:
    code = main(["NVDA", "--json"])
    assert code == EXIT_NOT_STORED
    payload = json.loads(capsys.readouterr().out)
    assert payload["stored"] is False
    assert payload["score_hash"] is None
    assert payload["on_chain"] is None
    assert payload["match"] is False
    assert "does not re-score" in payload["note"]
    assert "no stored payload" in payload["note"]
    assert "pre-fix" not in payload["note"]


def test_verify_tampered_bytes_exit_nonzero(capsys, tmp_path: Path) -> None:
    import base64

    db = _seed(tmp_path)
    body = json.loads(db.read_text(encoding="utf-8"))
    body["canonical_b64"] = base64.b64encode(b'{"ticker":"NVDA","score":1}').decode("ascii")
    db.write_text(json.dumps(body), encoding="utf-8")
    code = main(["NVDA", "--json", "--payload-file", str(db)])
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
        ["NVDA", "--json", "--payload-file", str(db), "--rpc-url", "http://127.0.0.1:8545", "--attester", "0x" + "11" * 20]
    )
    assert code == EXIT_NO_MATCH
    body = json.loads(capsys.readouterr().out)
    assert body["on_chain"]["chain_id"] == 1
    assert body["match"] is False
    assert "error" not in body["on_chain"]

    monkeypatch.setattr("rwa_score.api.verify.subprocess.check_output", _cast_outputs())
    code = main(
        ["NVDA", "--json", "--payload-file", str(db), "--rpc-url", "http://127.0.0.1:8545", "--attester", "0x" + "22" * 20]
    )
    assert code == EXIT_NO_MATCH
    body = json.loads(capsys.readouterr().out)
    assert body["on_chain"]["chain_id"] == 84532
    assert body["on_chain"]["attester_ok"] is False
    assert body["match"] is False


def test_verify_rpc_error_and_missing_cast(monkeypatch, capsys, tmp_path: Path) -> None:
    db = _seed(tmp_path)
    monkeypatch.setattr("rwa_score.api.verify.shutil.which", lambda _name: None)
    code = main(["NVDA", "--json", "--payload-file", str(db), "--rpc-url", "http://127.0.0.1:8545"])
    assert code == EXIT_CAST_MISSING
    body = json.loads(capsys.readouterr().out)
    assert body["on_chain"]["error"] == "cast_missing"

    def boom(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["cast"], output="connection refused http://127.0.0.1:8545")

    monkeypatch.setattr("rwa_score.api.verify.shutil.which", lambda _name: "/usr/bin/cast")
    monkeypatch.setattr("rwa_score.api.verify.subprocess.check_output", boom)
    code = main(["NVDA", "--json", "--payload-file", str(db), "--rpc-url", "http://127.0.0.1:8545"])
    assert code == EXIT_RPC_ERROR
    body = json.loads(capsys.readouterr().out)
    assert body["match"] is False
    assert "8545" not in body["on_chain"]["error"]
    assert "[rpc]" in body["on_chain"]["error"]


def test_verify_contract_defaults_to_pinned(monkeypatch, capsys) -> None:
    monkeypatch.delenv("RWA_ATTESTATION_CONTRACT", raising=False)
    code = main(["NVDA", "--json"])
    assert code == EXIT_NOT_STORED
    body = json.loads(capsys.readouterr().out)
    assert body["contract"] == PINNED_ATTESTATION_CONTRACT
    assert body["contract"] == "0x2F073a3628D498d92956e7eFE2b26633eDa75b00"
    assert "no stored payload" in body["note"]
    assert "pre-fix" not in body["note"]


def test_inputs_digest_covers_every_pillar_and_drops_rpc(
    fixture_scorer: TransparencyScorer,
) -> None:
    report = fixture_scorer.score("NVDA")
    original = inputs_digest(report)
    assert original == recompute_inputs_digest(attestation_inputs(report))
    assert "rpc_url" not in canonical_bytes(attestation_inputs(report)).decode("ascii")

    backing = dict(report)
    verification = {key: dict(value) for key, value in report["verification"].items()}
    backing_block = dict(verification["backing"])
    backing_block["meta"] = dict(backing_block.get("meta") or {})
    backing_block["meta"]["matched"] = not backing_block["meta"].get("matched")
    verification["backing"] = backing_block
    backing["verification"] = verification
    assert inputs_digest(backing) != original

    prose = dict(report)
    prose["explanations"] = dict(report["explanations"])
    prose["explanations"]["backing"] = "rewritten explanation"
    assert inputs_digest(prose) == original

    with_secret = dict(report)
    secret_verification = {key: dict(value) for key, value in report["verification"].items()}
    reserves = dict(secret_verification["reserves"])
    reserves["meta"] = dict(reserves.get("meta") or {})
    reserves["meta"]["rpc_url"] = "https://secret.example/KEY"
    reserves["source"] = "chainlink_por"
    secret_verification["reserves"] = reserves
    with_secret["verification"] = secret_verification
    blob = canonical_bytes(attestation_inputs(with_secret)).decode("ascii")
    assert "secret" not in blob
    assert "rpc_url" not in blob
    assert data_as_of(report) is None
    stamped = dict(with_secret)
    stamped_verification = {key: dict(value) for key, value in secret_verification.items()}
    stamped_reserves = dict(stamped_verification["reserves"])
    stamped_reserves["meta"] = dict(stamped_reserves["meta"])
    stamped_reserves["meta"]["updated_at"] = 1_700_000_111
    stamped_verification["reserves"] = stamped_reserves
    stamped["verification"] = stamped_verification
    assert data_as_of(stamped) == 1_700_000_111
    assert attestation_payload(stamped)["data_as_of"] == 1_700_000_111


def test_verify_rejects_payload_ticker_mismatch(capsys, tmp_path: Path) -> None:
    payload = _base_payload()
    payload["ticker"] = "TSLA"
    raw = canonical_bytes(payload)
    db = _write_bundle(tmp_path / "cross.payload.json", ticker="NVDA", canonical=raw, inputs=None)
    code = main(["NVDA", "--json", "--payload-file", str(db)])
    assert code == EXIT_NO_MATCH
    body = json.loads(capsys.readouterr().out)
    assert body["match"] is False
    assert body["ticker"] == "NVDA"
    assert body["ticker_ok"] is False
    assert body["payload_ticker"] == "TSLA"
    assert "does not match the requested ticker" in body["note"]
    assert "Traceback" not in capsys.readouterr().err


def test_verify_recomputes_stored_inputs(capsys, tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    from rwa_score.api.attest import inputs_bytes

    report = fixture_scorer.score("NVDA")
    raw = canonical_bytes(attestation_payload(report))
    inputs = inputs_bytes(report)
    db = _write_bundle(tmp_path / "inputs.payload.json", ticker="NVDA", canonical=raw, inputs=inputs)
    code = main(["NVDA", "--offline", "--json", "--payload-file", str(db)])
    assert code == EXIT_OK
    body = json.loads(capsys.readouterr().out)
    assert body["inputs_stored"] is True
    assert body["inputs_digest_ok"] is True
    assert body["offline"] is True
    assert body["match"] is None
    assert "nothing was checked on-chain" in body["note"].lower()
    assert recompute_inputs_digest(body["inputs"]) == body["payload"]["inputs_digest"]

    import base64

    body = json.loads(db.read_text(encoding="utf-8"))
    body["inputs_b64"] = base64.b64encode(canonical_bytes({"cmc": {"price": 1}})).decode("ascii")
    db.write_text(json.dumps(body), encoding="utf-8")
    code = main(["NVDA", "--json", "--payload-file", str(db)])
    assert code == EXIT_HASH_MISMATCH
    body = json.loads(capsys.readouterr().out)
    assert body["inputs_digest_ok"] is False
    assert body["match"] is False
    assert "Traceback" not in capsys.readouterr().err


def test_verify_malformed_payload_is_clean_json(capsys, tmp_path: Path) -> None:
    import base64

    db = _seed(tmp_path)
    cases = [
        b'{"ticker":"NVDA","score":',
        b"\xff\xfe{}",
        b"[1,2]",
    ]
    for blob in cases:
        body = json.loads(db.read_text(encoding="utf-8"))
        body["canonical_b64"] = base64.b64encode(blob).decode("ascii")
        db.write_text(json.dumps(body), encoding="utf-8")
        code = main(["NVDA", "--json", "--payload-file", str(db)])
        captured = capsys.readouterr()
        assert code == EXIT_HASH_MISMATCH
        assert "Traceback" not in captured.err
        assert "Traceback" not in captured.out
        parsed = json.loads(captured.out)
        assert parsed["error"] == "malformed_payload"
        assert parsed["match"] is False

    corrupt = tmp_path / "corrupt.payload.json"
    corrupt.write_bytes(b"this is not a payload")
    code = main(["NVDA", "--json", "--payload-file", str(corrupt)])
    captured = capsys.readouterr()
    assert code == EXIT_DB
    assert "Traceback" not in captured.err
    body = json.loads(captured.out)
    assert body["error"] == "payload_error"
    assert body["match"] is False


def test_verify_missing_db_is_not_created(capsys, tmp_path: Path) -> None:
    missing = tmp_path / "no" / "such" / "dir" / "x.sqlite"
    code = main(["NVDA", "--json", "--payload-file", str(missing)])
    captured = capsys.readouterr()
    assert code == EXIT_DB
    assert not missing.exists()
    assert not missing.parent.exists()
    assert "Traceback" not in captured.err
    body = json.loads(captured.out)
    assert body["error"] == "payload_missing"
    assert body["match"] is False
    assert body["stored"] is False


def test_verify_flags_legacy_payload_missing_new_fields(capsys, tmp_path: Path) -> None:
    payload = _base_payload()
    for key in ("as_of", "data_as_of", "scorer_version", "inputs_digest"):
        payload.pop(key)
    raw = canonical_bytes(payload)
    db = _write_bundle(tmp_path / "legacy.payload.json", ticker="NVDA", canonical=raw, inputs=None)
    code = main(["NVDA", "--json", "--payload-file", str(db)])
    assert code == EXIT_INPUTS_MISSING
    body = json.loads(capsys.readouterr().out)
    assert body["legacy"] is True
    assert body["hash_ok"] is True
    assert body["inputs_stored"] is False
    assert body["match"] is False
    assert "Legacy payload is missing" in body["note"]
    assert "inputs were not stored" in body["note"].lower()


def test_verify_exit_codes_are_distinct() -> None:
    codes = (
        EXIT_OK,
        EXIT_DB,
        EXIT_NOT_STORED,
        EXIT_HASH_MISMATCH,
        EXIT_NO_MATCH,
        EXIT_RPC_ERROR,
        EXIT_CAST_MISSING,
        EXIT_CHAIN_UNCHECKED,
        EXIT_INPUTS_MISSING,
    )
    assert codes == tuple(range(9))
    assert len(set(codes)) == len(codes)


def test_verify_without_rpc_fails_closed_unless_offline(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    monkeypatch.delenv("BASE_SEPOLIA_RPC_URL", raising=False)
    db = _seed(tmp_path)
    code = main(["NVDA", "--json", "--payload-file", str(db)])
    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_CHAIN_UNCHECKED
    assert body["match"] is False
    assert body["hash_ok"] is True
    assert "nothing was checked on-chain" in body["note"].lower()

    code = main(["NVDA", "--offline", "--json", "--payload-file", str(db)])
    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert body["match"] is None
    assert body["offline"] is True
    assert "nothing was checked on-chain" in body["note"].lower()


def test_verify_missing_inputs_json_is_its_own_exit(capsys, tmp_path: Path) -> None:
    payload = _base_payload()
    raw = canonical_bytes(payload)
    db = _write_bundle(tmp_path / "no-inputs.payload.json", ticker="NVDA", canonical=raw, inputs=None)
    code = main(["NVDA", "--offline", "--json", "--payload-file", str(db)])
    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_INPUTS_MISSING
    assert code not in (EXIT_OK, EXIT_HASH_MISMATCH, EXIT_NOT_STORED)
    assert body["inputs_stored"] is False
    assert body["match"] is False
    assert body["hash_ok"] is True
    assert "inputs were not stored" in body["note"].lower()


def test_verify_null_inputs_with_digest_exits_missing(capsys, tmp_path: Path) -> None:
    payload = _base_payload()
    raw = canonical_bytes(payload)
    db = _write_bundle(
        tmp_path / "null-inputs.payload.json",
        ticker="NVDA",
        canonical=raw,
        inputs=canonical_bytes({"cmc": {"ticker": "NVDA"}}),
    )
    body = json.loads(db.read_text(encoding="utf-8"))
    body["inputs_b64"] = None
    db.write_text(json.dumps(body), encoding="utf-8")
    code = main(["NVDA", "--offline", "--json", "--payload-file", str(db)])
    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_INPUTS_MISSING
    assert body["hash_ok"] is True
    assert body["payload"]["inputs_digest"]
    assert body["inputs_stored"] is False
    assert body["match"] is False
    assert "inputs were not stored" in body["note"].lower()


def test_missing_row_says_no_stored_payload(capsys) -> None:
    code = main(["NVDA", "--hash", "0x" + "11" * 32, "--json"])
    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_NOT_STORED
    assert "no stored payload" in body["note"]
    assert "pre-fix" not in body["note"]


def test_prefix_wording_only_when_hash_is_on_chain(monkeypatch, capsys) -> None:
    digest = "0x" + "ab" * 32

    def attested(**_kwargs):
        return {
            "ok": True,
            "chain_id": 84532,
            "attester_ok": True,
            "attested_at": 1,
            "attester": "0x" + "11" * 20,
        }

    monkeypatch.setattr("rwa_score.api.verify.on_chain_verify", attested)
    code = main(
        ["NVDA", "--hash", digest, "--json", "--rpc-url", "http://127.0.0.1:8545"]
    )
    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_NOT_STORED
    assert "pre-fix attestation, stored payload unavailable" in body["note"]
    assert body["on_chain"]["ok"] is True

    def absent(**_kwargs):
        return {"ok": False, "chain_id": 84532, "attester_ok": False}

    monkeypatch.setattr("rwa_score.api.verify.on_chain_verify", absent)
    code = main(
        ["NVDA", "--hash", digest, "--json", "--rpc-url", "http://127.0.0.1:8545"]
    )
    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_NOT_STORED
    assert "no stored payload" in body["note"]
    assert "pre-fix" not in body["note"]
    assert body["on_chain"] is None


def test_inputs_allowlist_drops_headers_keys_tokens_and_urls(
    fixture_scorer: TransparencyScorer,
) -> None:
    import copy

    report = copy.deepcopy(fixture_scorer.score("NVDA"))
    report["headers"] = {"Authorization": "Bearer SECRET-HEADER"}
    report["api_key"] = "rat_live_secret"
    report["price"]["api_key"] = "cmc-key-secret"
    report["price"]["headers"] = {"X-Api-Key": "cmc-header-secret"}
    report["price"]["tokens"][0]["token"] = "sekrit-token"
    report["price"]["tokens"][0]["market_url"] = "https://evil.example/token"
    report["basis"]["api_key"] = "basis-key-secret"
    report["basis"]["market_url"] = "https://evil.example/basis"
    report["verification"]["reserves"]["meta"] = {
        "rpc_url": "https://rpc.example/KEY",
        "headers": {"Authorization": "Bearer por-header"},
        "api_key": "por-key-secret",
        "docs_url": "https://docs.example/secret",
        "pillar": "reserves",
        "matched": True,
        "issuer": "Backed Finance",
    }
    report["heuristics"]["api_key"] = "heur-key-secret"
    report["heuristics"]["token"] = "heur-token-secret"
    report["issuer"] = "https://issuer.example/secret"
    blob = inputs_bytes(report).decode("ascii")
    payload_blob = canonical_bytes(attestation_payload(report)).decode("ascii")
    text = blob + payload_blob
    for secret in (
        "SECRET-HEADER",
        "rat_live_secret",
        "cmc-key-secret",
        "cmc-header-secret",
        "sekrit-token",
        "evil.example",
        "basis-key-secret",
        "rpc.example",
        "por-header",
        "por-key-secret",
        "docs.example",
        "heur-key-secret",
        "heur-token-secret",
        "issuer.example",
        "api_key",
        "rpc_url",
        "market_url",
        "headers",
        "Authorization",
        "https://",
    ):
        assert secret not in text
    parsed = json.loads(blob)
    assert parsed["verifiers"]["reserves"]["meta"]["matched"] is True
    assert parsed["cmc"]["price"]["price"] == report["price"]["price"]
    assert parsed["cmc"]["issuer"] is None
    assert parsed["heuristics"]["backed"] is True


def test_verify_hash_for_another_ticker_exits_4(capsys, tmp_path: Path) -> None:
    payload = _base_payload()
    payload["ticker"] = "TSLA"
    inputs = {"cmc": {"ticker": "TSLA"}}
    payload["inputs_digest"] = recompute_inputs_digest(inputs)
    raw = canonical_bytes(payload)
    digest = hash_canonical(raw)
    db = _write_bundle(
        tmp_path / "cross.payload.json",
        ticker="TSLA",
        canonical=raw,
        inputs=canonical_bytes(inputs),
        score_hash=digest,
    )
    code = main(["NVDA", "--hash", digest, "--json", "--payload-file", str(db)])
    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_NO_MATCH
    assert code != EXIT_NOT_STORED
    assert body["stored"] is True
    assert body["ticker_ok"] is False
    assert body["payload_ticker"] == "TSLA"
    assert body["match"] is False
