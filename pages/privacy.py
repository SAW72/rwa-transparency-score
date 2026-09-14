"""Streamlit page for the Privacy Policy (URL ``/privacy``)."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from rwa_score.legal import CONTACT_EMAIL, legal_markdown

ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
FAVICON_PATH = ASSETS_DIR / "favicon.png"

st.set_page_config(
    page_title="Privacy Policy — RAT Score",
    page_icon=str(FAVICON_PATH) if FAVICON_PATH.is_file() else "◎",
    layout="wide",
)

st.markdown(legal_markdown("privacy"))
st.divider()
st.page_link("app.py", label="Back to RAT Score")
st.page_link("pages/terms.py", label="Terms of Service")
st.caption(
    "Product Disclaimer is in the app sidebar/footer and README. "
    f"These pages do not replace it. Contact: {CONTACT_EMAIL}"
)
