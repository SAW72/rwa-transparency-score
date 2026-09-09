from rwa_score.issuer_registry import HEURISTIC_NOTE, classify


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
