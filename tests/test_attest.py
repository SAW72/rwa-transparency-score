"""Attestation hash is stable and changes when the cited breakdown is edited."""

from __future__ import annotations

from rwa_score.api.attest import attestation_payload, score_hash
from rwa_score.scorer import TransparencyScorer


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
    }
    assert "explanations" not in payload
    assert payload["subscores"] == report["subscores"]
