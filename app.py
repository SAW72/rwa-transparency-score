"""Streamlit demo for the CoinMarketCap Build-a-thon (Real World Assets track).

Run offline:
    streamlit run app.py

Bind for Render / any PaaS:
    streamlit run app.py --server.port $PORT --server.address 0.0.0.0
"""

from __future__ import annotations

import os

import streamlit as st

from rwa_score import PILLAR_HELP, WEIGHTS, TransparencyScorer, __version__, create_client
from rwa_score.client import CMCError

DEMO_TICKERS = ["NVDA", "TSLA", "AAPL"]
BAND_COLOR = {
    "green": "#2EE59D",
    "yellow": "#F5C542",
    "orange": "#F08A24",
    "red": "#F04343",
}

st.set_page_config(
    page_title="RWA Transparency Score",
    page_icon="◎",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _secret(name: str) -> str:
    try:
        val = st.secrets.get(name, "")
        if val:
            return str(val)
    except Exception:  # no secrets.toml in fixture/local runs
        pass
    return os.getenv(name, "")


def _score_color(score: float) -> str:
    if score >= 75:
        return BAND_COLOR["green"]
    if score >= 50:
        return BAND_COLOR["yellow"]
    if score >= 25:
        return BAND_COLOR["orange"]
    return BAND_COLOR["red"]


@st.cache_resource
def _scorer(use_fixtures: bool, api_key: str) -> TransparencyScorer:
    client = create_client(api_key=api_key or None, use_fixtures=use_fixtures)
    return TransparencyScorer(client)


st.markdown(
    """
    <style>
      .block-container { padding-top: 1.4rem; }
      .score-card {
        background: #151D2E;
        border: 1px solid #243049;
        border-radius: 16px;
        padding: 1.1rem 1.2rem 1.2rem;
        min-height: 168px;
      }
      .score-card .ticker { font-size: 0.85rem; letter-spacing: 0.08em; color: #8FA3C2; }
      .score-card .value { font-size: 2.6rem; font-weight: 700; line-height: 1.1; margin: 0.2rem 0; }
      .score-card .issuer { color: #C5D0E2; font-size: 0.92rem; }
      .score-card .band { font-size: 0.82rem; margin-top: 0.45rem; }
      .pill { display: inline-block; padding: 0.15rem 0.55rem; border-radius: 999px;
              font-size: 0.75rem; font-weight: 600; letter-spacing: 0.04em; }
      .flag { background: #2A1A1A; border-left: 3px solid #F04343; padding: 0.45rem 0.7rem;
              border-radius: 6px; margin-bottom: 0.4rem; color: #F3CACA; font-size: 0.9rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

api_key = _secret("CMC_API_KEY").strip()
default_fixtures = _secret("USE_FIXTURES").strip().lower() in {"1", "true", "yes", "on"} or not api_key

with st.sidebar:
    st.markdown("### RWA Transparency Score")
    st.caption(f"v{__version__} · CMC Build-a-thon · Real World Assets")
    mode = st.radio(
        "Data source",
        ("Fixture / offline (no API key)", "Live CoinMarketCap API"),
        index=0 if default_fixtures else 1,
        help="Fixture mode uses canned CMC-shaped JSON so judges can run the demo without a key.",
    )
    use_fixtures = mode.startswith("Fixture")
    tickers_raw = st.text_input(
        "Tickers to score",
        value=" ".join(DEMO_TICKERS),
        help="Space or comma separated. Fixture mode ships NVDA, TSLA, AAPL.",
    )
    run = st.button("Score tickers", type="primary", use_container_width=True)
    st.divider()
    st.markdown("**Five pillars (0–100)**")
    for key, weight in WEIGHTS.items():
        st.caption(f"{key.title()} · {int(weight * 100)}% — {PILLAR_HELP[key].rsplit(' (', 1)[0]}")
    st.divider()
    st.markdown("**CMC endpoints**")
    st.caption(
        "`GET /v5/real-world-assets/map`  \n"
        "`GET /v5/real-world-assets/info`  \n"
        "`GET /v5/real-world-assets/issuers/list`  \n"
        "`GET /v5/real-world-assets/issuers`  \n"
        "`GET /v2/cryptocurrency/quotes/latest`"
    )
    st.caption("Never commit `CMC_API_KEY`. Fixture mode is the default when the key is empty.")

st.title("Tokenized-stock honesty score")
st.write(
    "Rate every tokenized equity on how honest its issuer actually is — backing, "
    "proof of reserves, redemption, price integrity, and SEC disclosure — using "
    "CoinMarketCap's RWA API."
)

if not run:
    run = True  # first paint should already show the demo set

tickers = [t.strip().upper() for t in tickers_raw.replace(",", " ").split() if t.strip()]
if not tickers:
    st.warning("Enter at least one ticker.")
    st.stop()

try:
    scorer = _scorer(use_fixtures, api_key if not use_fixtures else "")
except CMCError as exc:
    st.error(str(exc))
    st.info("Switch back to fixture / offline mode, or set `CMC_API_KEY` in `.env` or Streamlit secrets.")
    st.stop()

source = getattr(scorer.client, "source", "unknown")
if source == "fixtures":
    st.info(
        "Running on canned CMC responses (`rwa_score/data/bundle.json`). "
        "No network and no API key required — this is the judge-safe demo path."
    )
else:
    st.success("Live CoinMarketCap Pro API. The key stays in the environment and is never rendered.")

with st.spinner("Scoring tokenized stocks…"):
    results = scorer.score_many(tickers)

ok = [r for r in results if "error" not in r]
bad = [r for r in results if "error" in r]

if bad:
    for r in bad:
        st.error(f"{r['ticker']}: {r['error']}")

if not ok:
    st.stop()

st.subheader("Leaderboard")
cols = st.columns(len(ok))
for col, r in zip(cols, ok):
    color = _score_color(r["score"])
    with col:
        st.markdown(
            f"""
            <div class="score-card">
              <div class="ticker">{r['ticker']} · {r.get('token_symbol') or '—'}</div>
              <div class="value" style="color:{color}">{r['score']}</div>
              <div class="issuer">{r['issuer']}</div>
              <div class="band"><span class="pill" style="background:{color}22;color:{color}">{r['band']}</span></div>
            </div>
            """,
            unsafe_allow_html=True,
        )

st.subheader("Compare")
table = []
for r in ok:
    row = {
        "Ticker": r["ticker"],
        "Score": r["score"],
        "Band": r["band"].split(" —")[0],
        "Issuer": r["issuer"],
        "CIK": r.get("cik") or "—",
        "Token $": r.get("price_usd") if r.get("price_usd") is not None else "—",
        "24h %": r.get("percent_change_24h") if r.get("percent_change_24h") is not None else "—",
    }
    row.update({k.title(): v for k, v in r["subscores"].items()})
    table.append(row)
st.dataframe(table, use_container_width=True, hide_index=True)

st.subheader("Why this score")
labels = [f"{r['ticker']} ({r['score']})" for r in ok]
choice = st.selectbox("Inspect a token", options=list(range(len(ok))), format_func=lambda i: labels[i])
detail = ok[choice]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Issuer", detail["issuer"])
c2.metric("rwa_id", detail["rwa_id"])
c3.metric("SEC CIK", detail.get("cik") or "missing")
change = detail.get("percent_change_24h")
c4.metric("Token 24h", f"{change:+.1f}%" if change is not None else "n/a")

st.markdown("##### Pillar breakdown")
for pillar, value in detail["subscores"].items():
    color = _score_color(value)
    st.markdown(
        f"**{pillar.title()}** · {value:.0f}/100 · {int(WEIGHTS[pillar] * 100)}% weight"
    )
    st.progress(min(1.0, value / 100.0))
    st.caption(detail["reasons"][pillar])

if detail["flags"]:
    st.markdown("##### Risk flags")
    for flag in detail["flags"]:
        st.markdown(f'<div class="flag">{flag}</div>', unsafe_allow_html=True)
else:
    st.success("No risk flags on this token.")

with st.expander("Classification heuristics (issuer registry)"):
    st.json(detail["classification"])
    st.caption(
        "CMC issuer objects do not include custody, audit, or redemption fields. "
        "The registry is a documented allow-list applied to the issuer name. "
        "That gap is called out in the README for the CMC product team."
    )

with st.expander("Raw score payload"):
    st.json(detail)

st.divider()
st.caption(
    "Informational only — not financial advice. Fixture quotes are canned CMC-shaped "
    "responses for the hackathon demo. Live mode uses your own CoinMarketCap key."
)
