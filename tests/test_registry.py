import pytest

from rwa_score.issuer_registry import (
    AUDITED,
    FULLY_BACKED,
    HEURISTIC_NOTE,
    REDEEMABLE,
    classify,
)


def test_classify_backed_finance() -> None:
    flags = classify("Backed Finance")
    assert flags == {"backed": True, "audited": True, "redeemable": True}


def test_classify_xstocks() -> None:
    flags = classify("xStocks")
    assert flags["backed"] is True
    assert flags["redeemable"] is True
    assert flags["audited"] is False


def test_classify_unknown() -> None:
    flags = classify("NoteVault Demo Issuer")
    assert flags == {"backed": False, "audited": False, "redeemable": False}


def test_heuristic_note_is_explicit() -> None:
    assert "heuristic" in HEURISTIC_NOTE.lower()


def test_allowlist_omits_bare_backed() -> None:
    assert "backed" not in FULLY_BACKED
    assert "backed" not in AUDITED
    assert "backed" not in REDEEMABLE


@pytest.mark.parametrize(
    "name",
    [
        "Not Backed At All",
        "UNBACKED",
        "feedbacked",
        "Anti-Ondo",
        "Anti-Ondo Finance",
        "not-backed",
        "un-backed wrapper",
        "Backed",
        "totally backed",
    ],
)
def test_classify_rejects_adversarial_and_awkward_names(name: str) -> None:
    """Breaker attack names must not classify as backed / audited / redeemable."""
    assert classify(name) == {"backed": False, "audited": False, "redeemable": False}


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Backed Finance", {"backed": True, "audited": True, "redeemable": True}),
        ("Backed Finance AG", {"backed": True, "audited": True, "redeemable": True}),
        ("xStocks", {"backed": True, "audited": False, "redeemable": True}),
        ("Securitize", {"backed": True, "audited": False, "redeemable": True}),
        ("Ondo", {"backed": True, "audited": True, "redeemable": True}),
        ("Ondo Finance", {"backed": True, "audited": True, "redeemable": True}),
        ("Paxos", {"backed": True, "audited": True, "redeemable": False}),
    ],
)
def test_classify_true_positives_still_match(name: str, expected: dict[str, bool]) -> None:
    assert classify(name) == expected
