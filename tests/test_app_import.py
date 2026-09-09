"""Smoke-import the Streamlit entrypoint without launching the server."""


def test_app_module_exports_helpers() -> None:
    import app as demo_app

    assert "NVDA" in demo_app.FIXTURE_TICKERS
    assert "financial advice" in demo_app.DISCLAIMER.lower()
    assert set(demo_app.BAND_COLORS) == {"GREEN", "YELLOW", "ORANGE", "RED"}
