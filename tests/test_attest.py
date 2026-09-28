"""Attestation hash is stable and changes when the cited breakdown is edited."""

from __future__ import annotations

import json

from rwa_score.api.attest import attestation_payload, score_hash
from rwa_score.api.verify import main
from rwa_score.scorer import TransparencyScorer

# cast 1.8.3 prints a uint256 as the integer plus a bracketed decimal.
CAST_183_VERIFY = (
    "true\n"
    "1700000000 [1.7e9]\n"
    "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266\n"
)


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
    assert set(payload) == {
        "ticker",
        "rwa_id",
        "issuer",
        "score",
        "band",
        "subscores",
        "weights",
        "cik",
        "data_source",
        "verification",
        "basis",
    }
    assert "explanations" not in payload
    assert payload["subscores"] == report["subscores"]
    assert payload["weights"] == report["weights"]
    assert "basis" in payload["subscores"]
    assert "basis" in payload["weights"]
    assert "basis" in payload["verification"]
    assert payload["basis"] == report["basis"]


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


def test_verify_json_parses_cast_183_attested_at(monkeypatch, capsys) -> None:
    monkeypatch.setattr("rwa_score.api.verify.shutil.which", lambda _name: "/usr/bin/cast")
    monkeypatch.setattr(
        "rwa_score.api.verify.subprocess.check_output",
        lambda *_args, **_kwargs: CAST_183_VERIFY,
    )
    code = main(
        [
            "NVDA",
            "--fixtures",
            "--json",
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
