from rwa_score.issuer_registry import classify


def test_backed_assets_is_fully_transparent() -> None:
    flags = classify("Backed Assets")
    assert flags == {"backed": True, "audited": True, "redeemable": True}


def test_xstocks_is_backed_but_not_audited() -> None:
    flags = classify("xStocks")
    assert flags["backed"] is True
    assert flags["redeemable"] is True
    assert flags["audited"] is False


def test_backpack_is_not_treated_as_backed() -> None:
    """Regression: naive substring 'backed' used to match Backpack."""
    flags = classify("Backpack")
    assert flags == {"backed": False, "audited": False, "redeemable": False}


def test_unknown_issuer_is_opaque() -> None:
    flags = classify("ThinWrap Labs")
    assert flags == {"backed": False, "audited": False, "redeemable": False}


def test_empty_name_is_opaque() -> None:
    assert classify("")["backed"] is False
    assert classify(None)["backed"] is False  # type: ignore[arg-type]
