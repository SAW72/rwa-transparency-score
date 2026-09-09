"""Streamlit demo for the RWA Transparency Score.

Launch (fixtures, no API key):
    RWA_USE_FIXTURES=1 streamlit run app.py

Render binds 0.0.0.0:$PORT via render.yaml.
"""

from __future__ import annotations

import os

import streamlit as st

from rwa_score.client import create_client, env_flag
from rwa_score.scorer import PILLARS, WEIGHTS, ScoreError, TransparencyScorer

DISCLAIMER = (
    "This tool is for informational and hackathon demo purposes only. "
    "It is **not financial advice**, not an offer to buy or sell securities, "
    "and not a substitute for issuer filings or independent due diligence."
)

BAND_COLORS = {
    "GREEN": "#3DDC97",
    "YELLOW": "#F5C542",
    "ORANGE": "#F08A24",
    "RED": "#E5484D",
}

FIXTURE_TICKERS = ["NVDA", "TSLA", "AAPL", "META"]


def _init_scorer(use_fixtures: bool) -> TransparencyScorer:
    client = create_client(use_fixtures_mode=use_fixtures)
    return TransparencyScorer(client)


def _score_one(scorer: TransparencyScorer, ticker: str) -> dict:
    return scorer.score(ticker.strip().upper())


def _render_score_card(report: dict) -> None:
    band = report["band"]
    color = BAND_COLORS.get(band, "#8B949E")
    st.markdown(
        f"""
        <div class="score-hero" style="border-color:{color}">
          <div class="score-num">{report['score']:.1f}</div>
          <div class="score-meta">
            <div class="band" style="color:{color}">{report['band_label']}</div>
            <div class="issuer">{report['ticker']} · {report['issuer']}</div>
            <div class="summary">{report['summary']}</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if report.get("data_source") == "fixture":
        st.caption("Demo fixture data — not a live CoinMarketCap API response.")

    st.caption(
        "Backing / reserves / redemption use **issuer-name heuristics**, not audited attestations."
    )

    cols = st.columns(5)
    for col, key in zip(cols, WEIGHTS):
        sub = report["subscores"][key]
        meta = PILLARS[key]
        with col:
            st.metric(meta["label"], f"{sub:.0f}", help=f"Weight {WEIGHTS[key]:.0%}")
            st.progress(min(max(sub / 100.0, 0.0), 1.0))

    st.markdown("#### Pillar detail")
    for key in WEIGHTS:
        meta = PILLARS[key]
        heuristic = key in {"backing", "reserves", "redemption"}
        badge = " · heuristic" if heuristic else ""
        with st.expander(
            f"{meta['label']} — {report['subscores'][key]:.0f}/100 "
            f"(weight {WEIGHTS[key]:.0%}){badge}",
            expanded=False,
        ):
            st.write(meta["what"])
            st.write(report["explanations"][key])

    st.markdown("#### Risk flags")
    flags = report.get("flags") or []
    if flags:
        for flag in flags:
            st.warning(flag)
    else:
        st.success("No risk flags on this pass.")

    notes = report.get("notes") or []
    if notes:
        st.markdown("#### Notes")
        for note in notes:
            st.info(note)


st.set_page_config(
    page_title="RWA Transparency Score",
    page_icon="◎",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .score-hero {
        display: flex; gap: 1.5rem; align-items: center;
        border: 2px solid #30363d; border-radius: 16px;
        padding: 1.25rem 1.5rem; margin: 0.5rem 0 1.25rem;
        background: #161b22;
      }
      .score-num { font-size: 4rem; font-weight: 700; line-height: 1; }
      .band { font-size: 1.15rem; font-weight: 600; }
      .issuer { color: #8b949e; margin-top: 0.25rem; }
      .summary { margin-top: 0.35rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("RWA Transparency Score")
st.caption("Risk radar for tokenized stocks · CoinMarketCap Build-a-thon")

default_fixtures = env_flag("RWA_USE_FIXTURES") or not os.getenv("CMC_API_KEY")

with st.sidebar:
    st.header("Demo controls")
    use_fixtures = st.toggle(
        "Use demo fixtures",
        value=default_fixtures,
        help="Bypass the live CMC API. Required if you do not have CMC_API_KEY.",
    )
    if use_fixtures:
        st.warning("Fixture mode is on. Scores are from bundled **demo data**, not live CMC.")
    else:
        if not os.getenv("CMC_API_KEY"):
            st.error("CMC_API_KEY is not set. Switch fixtures on, or add the key in the host env.")
        else:
            st.success("Live mode: CMC_API_KEY is set. Issuer list is cached for this session.")

    st.markdown("### Pillar weights")
    for key, weight in WEIGHTS.items():
        st.write(f"**{PILLARS[key]['label']}** — {weight:.0%}")
        st.caption(PILLARS[key]["what"])

    st.markdown("### Disclaimer")
    st.write(DISCLAIMER)

if "scorer_mode" not in st.session_state or st.session_state.scorer_mode != use_fixtures:
    try:
        st.session_state.scorer = _init_scorer(use_fixtures)
        st.session_state.scorer_mode = use_fixtures
        st.session_state.last_error = None
    except Exception as exc:  # noqa: BLE001
        st.session_state.scorer = None
        st.session_state.last_error = str(exc)

if st.session_state.get("last_error"):
    st.error(st.session_state.last_error)
    st.stop()

scorer: TransparencyScorer = st.session_state.scorer

tab_score, tab_compare = st.tabs(["Score a ticker", "Compare 2–3 tickers"])

with tab_score:
    st.subheader("Search / score")
    c1, c2 = st.columns([3, 1])
    with c1:
        ticker = st.text_input(
            "Ticker",
            value="NVDA",
            placeholder="NVDA, TSLA, AAPL, META…",
            label_visibility="collapsed",
        )
    with c2:
        go = st.button("Score", type="primary", use_container_width=True)

    st.caption("Fixture catalog: " + ", ".join(FIXTURE_TICKERS))
    quick = st.columns(len(FIXTURE_TICKERS))
    picked = None
    for col, sym in zip(quick, FIXTURE_TICKERS):
        if col.button(sym, use_container_width=True):
            picked = sym

    target = (picked or (ticker if go or ticker else "")).strip().upper()
    if target:
        try:
            report = _score_one(scorer, target)
            _render_score_card(report)
        except ScoreError as exc:
            st.error(str(exc))
        except Exception as exc:  # noqa: BLE001
            st.error(f"Scoring failed: {exc}")

with tab_compare:
    st.subheader("Side-by-side")
    default_compare = ["NVDA", "TSLA", "AAPL"]
    option_pool = list(FIXTURE_TICKERS)
    typed = (ticker or "").strip().upper()
    if typed and typed not in option_pool:
        option_pool.append(typed)
    choices = st.multiselect(
        "Pick 2 or 3 tickers",
        options=option_pool,
        default=default_compare,
        max_selections=3,
    )
    extra = st.text_input("Or type extra tickers (comma-separated)", value="")
    if extra.strip():
        for part in extra.split(","):
            sym = part.strip().upper()
            if sym and sym not in choices and len(choices) < 3:
                choices.append(sym)

    if st.button("Compare", type="primary") or choices:
        if len(choices) < 2:
            st.info("Select at least two tickers to compare.")
        else:
            reports = []
            errors = []
            for sym in choices[:3]:
                try:
                    reports.append(_score_one(scorer, sym))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{sym}: {exc}")
            if errors:
                for err in errors:
                    st.error(err)
            if reports:
                cols = st.columns(len(reports))
                for col, report in zip(cols, reports):
                    with col:
                        _render_score_card(report)

                rows = []
                for report in reports:
                    row = {
                        "ticker": report["ticker"],
                        "issuer": report["issuer"],
                        "score": report["score"],
                        "band": report["band"],
                    }
                    row.update(report["subscores"])
                    rows.append(row)
                st.markdown("#### Comparison table")
                st.dataframe(rows, use_container_width=True, hide_index=True)

st.divider()
st.caption(DISCLAIMER)
