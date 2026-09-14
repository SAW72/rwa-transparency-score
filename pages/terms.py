"""Streamlit page for the Terms of Service (URL ``/terms``)."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from rwa_score.legal import CONTACT_EMAIL, legal_markdown

ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
FAVICON_PATH = ASSETS_DIR / "favicon.png"

st.set_page_config(
    page_title="Terms of Service — RAT Score",
    page_icon=str(FAVICON_PATH) if FAVICON_PATH.is_file() else "◎",
    layout="wide",
)

st.markdown(legal_markdown("terms"))
st.divider()
st.markdown("[Back to RAT Score](/) · [Privacy Policy](/privacy)")
st.caption(
    "Product Disclaimer is in the app sidebar/footer and README. "
    f"These pages do not replace it. Contact: {CONTACT_EMAIL}"
)
