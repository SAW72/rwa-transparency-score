"""Streamlit demo for RAT Score (RWA Transparency Score).

Launch (fixtures, no API key):
    RWA_USE_FIXTURES=1 streamlit run app.py

Render binds 0.0.0.0:$PORT via render.yaml.
"""

from __future__ import annotations

import base64
import html
import os
from pathlib import Path

import streamlit as st

from rwa_score.client import create_client, env_flag
from rwa_score.scorer import PILLARS, WEIGHTS, ScoreError, TransparencyScorer

PAGE_TITLE = "RAT Score | RWA Transparency Score"
BRAND_H1 = "RAT Score"
BRAND_SUB = "RWA Transparency Score"
TAGLINE = "Risk radar for tokenized stocks · CoinMarketCap Build-a-thon"

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
FAVICON_PATH = ASSETS_DIR / "favicon.png"
MONOGRAM_PATH = ASSETS_DIR / "rat-monogram.png"

DISCLAIMER = (
    "Informational and educational hackathon demo only. Not financial, investment, "
    "legal, or tax advice. Not an offer, solicitation, or recommendation to buy, "
    "sell, or hold any security, digital asset, tokenized stock, or other instrument. "
    "Scores are automated heuristics (including issuer-name matching) plus third-party "
    "CoinMarketCap data or bundled demo fixtures — not audited attestations, not legal "
    "or audit opinions, and not a substitute for issuer filings, prospectuses, offering "
    "documents, or your own independent research. Data may be incomplete, delayed, "
    "inaccurate, or outdated. Nothing here guarantees accuracy, completeness, or fitness "
    "for any purpose. Past or present scores are not indicative of future results. This "
    "demo is not provided by a broker-dealer, exchange, ATS, funding portal, or registered "
    "investment adviser, and it does not create any advisory or fiduciary relationship. "
    "Do your own research. Use at your own risk."
)

BAND_COLORS = {
    "GREEN": "#3DDC97",
    "YELLOW": "#F5C542",
    "ORANGE": "#F08A24",
    "RED": "#E5484D",
}

FIXTURE_TICKERS = ["NVDA", "TSLA", "AAPL", "META"]
DEFAULT_SLOTS = ["NVDA", "TSLA", "AAPL", "META"]
MAX_COMPARE_SLOTS = 4

st.set_page_config(
    page_title=PAGE_TITLE,
    page_icon=str(FAVICON_PATH) if FAVICON_PATH.is_file() else "◎",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _asset_data_uri(path: Path) -> str | None:
    if not path.is_file():
        return None
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    suffix = path.suffix.lower()
    mime = {".png": "image/png", ".webp": "image/webp", ".svg": "image/svg+xml"}.get(
        suffix, "image/png"
    )
    return f"data:{mime};base64,{payload}"


def _init_scorer(use_fixtures: bool) -> TransparencyScorer:
    client = create_client(use_fixtures_mode=use_fixtures)
    return TransparencyScorer(client)


@st.cache_resource
def _cached_scorer(use_fixtures: bool) -> TransparencyScorer:
    """One client + scorer per process (and fixture/live mode).

    Widget reruns must not rebuild CMCClient — that would re-hit issuers/list.
    """
    return _init_scorer(use_fixtures)


def normalize_ticker(raw: str) -> str:
    return (raw or "").strip().upper()


def assign_ticker_to_slot(slots: list[str], index: int, ticker: str) -> list[str]:
    """Replace one comparison slot. Returns a new list; does not mutate `slots`."""
    if not 0 <= index < len(slots):
        raise IndexError(f"slot index {index} out of range")
    symbol = normalize_ticker(ticker)
    if not symbol:
        raise ValueError("ticker is empty")
    updated = list(slots)
    updated[index] = symbol
    return updated


def _score_one(scorer: TransparencyScorer, ticker: str) -> dict:
    return scorer.score(normalize_ticker(ticker))


def _score_slots(
    scorer: TransparencyScorer, slots: list[str]
) -> list[tuple[str, dict | None, str | None]]:
    """Score each slot independently so one unknown ticker does not hide the row."""
    results: list[tuple[str, dict | None, str | None]] = []
    for raw in slots:
        symbol = normalize_ticker(raw)
        if not symbol:
            results.append(("", None, "Empty slot."))
            continue
        try:
            results.append((symbol, _score_one(scorer, symbol), None))
        except ScoreError as exc:
            results.append((symbol, None, str(exc)))
        except Exception as exc:  # noqa: BLE001
            results.append((symbol, None, f"Scoring failed: {exc}"))
    return results


def _ensure_slot_state() -> None:
    if "slots" not in st.session_state:
        st.session_state.slots = list(DEFAULT_SLOTS)
    if "active_slot" not in st.session_state:
        st.session_state.active_slot = 0
    # Recover from a stale session that somehow lost a slot.
    slots = list(st.session_state.slots)
    if len(slots) != MAX_COMPARE_SLOTS:
        padded = (slots + list(DEFAULT_SLOTS))[:MAX_COMPARE_SLOTS]
        st.session_state.slots = padded
    if not 0 <= int(st.session_state.active_slot) < MAX_COMPARE_SLOTS:
        st.session_state.active_slot = 0


def _place_in_slot(ticker: str, index: int) -> None:
    st.session_state.slots = assign_ticker_to_slot(list(st.session_state.slots), index, ticker)
    st.session_state.active_slot = index


def _score_card_html(report: dict, *, selected: bool = False) -> str:
    """Build the compact score card. Dynamic fields are HTML-escaped."""
    band = report["band"]
    color = BAND_COLORS.get(band, "#8B949E")
    ring = "3px" if selected else "2px"
    selected_attr = " selected" if selected else ""
    ticker = html.escape(str(report["ticker"]))
    issuer = html.escape(str(report["issuer"]))
    summary = html.escape(str(report["summary"]))
    band_label = html.escape(str(report["band_label"]))
    return f"""
        <div class="score-hero compact{selected_attr}" style="border-color:{color};border-width:{ring}">
          <div class="score-num">{report['score']:.1f}</div>
          <div class="score-meta">
            <div class="band" style="color:{color}">{band_label}</div>
            <div class="issuer">{ticker} · {issuer}</div>
            <div class="summary">{summary}</div>
          </div>
        </div>
        """


def _error_card_html(ticker: str, message: str, *, selected: bool = False) -> str:
    """Build the unavailable-slot card. Dynamic fields are HTML-escaped."""
    ring = " selected" if selected else ""
    safe_ticker = html.escape(ticker or "Empty slot")
    safe_message = html.escape(message)
    return f"""
        <div class="score-hero compact error{ring}">
          <div class="score-num">—</div>
          <div class="score-meta">
            <div class="band" style="color:#E5484D">Unavailable</div>
            <div class="issuer">{safe_ticker}</div>
            <div class="summary">{safe_message}</div>
          </div>
        </div>
        """


def _render_compare_card(report: dict, *, selected: bool = False) -> None:
    st.markdown(
        _score_card_html(report, selected=selected),
        unsafe_allow_html=True,
    )

    if report.get("data_source") == "fixture":
        st.caption("Demo fixture data — not a live CoinMarketCap API response.")
    else:
        st.caption("Live CoinMarketCap data.")

    st.caption(
        "Backing / reserves / redemption use **issuer-name heuristics**, not audited attestations."
    )

    metric_bits = []
    for key in WEIGHTS:
        meta = PILLARS[key]
        metric_bits.append(f"**{meta['label']}** {report['subscores'][key]:.0f}")
    st.markdown(" · ".join(metric_bits))

    flags = report.get("flags") or []
    if flags:
        for flag in flags:
            st.warning(flag)
    else:
        st.caption("No risk flags on this pass.")

    with st.expander("Pillar detail", expanded=False):
        for key in WEIGHTS:
            meta = PILLARS[key]
            heuristic = key in {"backing", "reserves", "redemption"}
            badge = " · heuristic" if heuristic else ""
            st.markdown(
                f"**{meta['label']}** — {report['subscores'][key]:.0f}/100 "
                f"(weight {WEIGHTS[key]:.0%}){badge}"
            )
            st.caption(meta["what"])
            st.write(report["explanations"][key])

        notes = report.get("notes") or []
        if notes:
            st.markdown("**Notes**")
            for note in notes:
                st.info(note)


def _render_slot_error(ticker: str, message: str, *, selected: bool = False) -> None:
    st.markdown(
        _error_card_html(ticker, message, selected=selected),
        unsafe_allow_html=True,
    )
    st.error(message)


st.markdown(
    """
    <style>
      .rat-brand { margin: 0 0 0.35rem; }
      .rat-brand-row { display: flex; align-items: center; gap: 0.75rem; }
      .rat-chip {
        width: 36px; height: 36px; flex: 0 0 36px;
        border-radius: 10px; overflow: hidden;
        border: 1px solid rgba(61, 220, 151, 0.35);
        background: #161B22;
      }
      .rat-chip img { width: 36px; height: 36px; display: block; }
      .rat-titles h1 {
        margin: 0; padding: 0;
        font-size: 2.05rem; font-weight: 700; line-height: 1.1;
        color: #E6EDF3; letter-spacing: -0.02em;
      }
      .rat-sub {
        margin: 0.2rem 0 0;
        color: #8b949e;
        font-size: 0.95rem;
        font-weight: 500;
      }
      .rat-tagline {
        margin: 0.55rem 0 0.15rem;
        color: #8b949e;
        font-size: 0.95rem;
      }
      .mode-chip {
        display: inline-flex; align-items: center;
        border: 1px solid #3DDC97;
        color: #3DDC97;
        background: transparent;
        border-radius: 999px;
        padding: 0.18rem 0.72rem;
        font-size: 0.75rem;
        font-weight: 600;
        letter-spacing: 0.06em;
        text-transform: uppercase;
      }
      .score-hero {
        position: relative;
        isolation: isolate;
        overflow: hidden;
        display: flex; gap: 1.5rem; align-items: center;
        border: 2px solid #30363d; border-radius: 16px;
        padding: 1.25rem 1.5rem; margin: 0.5rem 0 1.25rem;
        background: #161b22;
      }
      .score-hero::before {
        content: "";
        position: absolute;
        inset: -35% -10% -35% -25%;
        pointer-events: none;
        z-index: 0;
        background:
          radial-gradient(circle at 28% 48%, transparent 16%, rgba(61,220,151,0.12) 17%, transparent 18%),
          radial-gradient(circle at 28% 48%, transparent 30%, rgba(61,220,151,0.10) 31%, transparent 32%),
          radial-gradient(circle at 28% 48%, transparent 44%, rgba(61,220,151,0.08) 45%, transparent 46%);
      }
      .score-hero > * { position: relative; z-index: 1; }
      .score-hero.compact {
        flex-direction: column; align-items: flex-start; gap: 0.35rem;
        padding: 0.85rem 1rem; margin: 0.25rem 0 0.75rem; min-height: 10.5rem;
      }
      .score-hero.compact.selected { box-shadow: 0 0 0 1px #3DDC97 inset; }
      .score-hero.compact.error { border-color: #E5484D; }
      .score-num { font-size: 4rem; font-weight: 700; line-height: 1; }
      .score-hero.compact .score-num { font-size: 2.35rem; }
      .band { font-size: 1.15rem; font-weight: 600; }
      .score-hero.compact .band { font-size: 0.95rem; }
      .issuer { color: #8b949e; margin-top: 0.25rem; }
      .summary { margin-top: 0.35rem; }
      .score-hero.compact .summary { font-size: 0.85rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

_monogram_uri = (
    _asset_data_uri(MONOGRAM_PATH)
    or _asset_data_uri(ASSETS_DIR / "rat-icon-192.png")
    or _asset_data_uri(ASSETS_DIR / "rat-monogram.svg")
)
_chip_html = (
    f'<div class="rat-chip"><img src="{_monogram_uri}" alt="" width="36" height="36" /></div>'
    if _monogram_uri
    else '<div class="rat-chip" aria-hidden="true"></div>'
)
st.markdown(
    f"""
    <div class="rat-brand">
      <div class="rat-brand-row">
        {_chip_html}
        <div class="rat-titles">
          <h1>{BRAND_H1}</h1>
          <p class="rat-sub">{BRAND_SUB}</p>
        </div>
      </div>
      <p class="rat-tagline">{TAGLINE}</p>
    </div>
    """,
    unsafe_allow_html=True,
)

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
            st.success(
                "Live mode: CMC_API_KEY is set. Issuer directory is cached for this "
                "process. Basic plan 429s are retried; wait a minute if it still fails."
            )

    st.markdown("### Pillar weights")
    for key, weight in WEIGHTS.items():
        st.write(f"**{PILLARS[key]['label']}** — {weight:.0%}")
        st.caption(PILLARS[key]["what"])

    st.markdown("### Disclaimer")
    st.write(DISCLAIMER)

mode_label = "Fixture" if use_fixtures else "Live"
st.markdown(
    f'<div class="mode-chip" title="Data mode">{mode_label}</div>',
    unsafe_allow_html=True,
)

try:
    scorer = _cached_scorer(use_fixtures)
    st.session_state.last_error = None
except Exception as exc:  # noqa: BLE001
    st.session_state.last_error = str(exc)
    st.error(str(exc))
    st.stop()

_ensure_slot_state()

st.subheader("Score / Compare")
st.caption(
    "Compact search assigns a ticker into one of the four slots. "
    "All four compare side by side in one row."
)

search_col, assign_col, _pad = st.columns([1.15, 0.55, 3.3], gap="small")
with search_col:
    query = st.text_input(
        "Ticker search",
        placeholder="Search ticker…",
        label_visibility="collapsed",
        key="ticker_query",
    )
with assign_col:
    assign_clicked = st.button("Assign", type="primary", use_container_width=True)

typed = normalize_ticker(query)
if use_fixtures:
    st.caption(
        "Fixture catalog: "
        + ", ".join(FIXTURE_TICKERS)
        + ". Unknown tickers error in that slot."
    )
else:
    st.caption("Live mode: any CMC-mapped ticker can fill a slot.")

if assign_clicked:
    if typed:
        _place_in_slot(typed, int(st.session_state.active_slot))
    else:
        st.info("Type a ticker, then Assign — or click a slot to place it.")

if typed:
    if st.button(f"Use {typed}", key="use_typed_ticker"):
        _place_in_slot(typed, int(st.session_state.active_slot))

slot_cols = st.columns(MAX_COMPARE_SLOTS, gap="small")
for index, symbol in enumerate(st.session_state.slots):
    with slot_cols[index]:
        selected = index == int(st.session_state.active_slot)
        label = f"● {symbol}" if selected else symbol
        if st.button(
            label,
            key=f"slot_{index}",
            type="primary" if selected else "secondary",
            use_container_width=True,
        ):
            if typed:
                _place_in_slot(typed, index)
            else:
                st.session_state.active_slot = index
            st.rerun()

active = int(st.session_state.active_slot)
active_symbol = st.session_state.slots[active]
st.caption(
    f"Selected slot {active + 1} · **{active_symbol}** — next search replaces this name."
)

results = _score_slots(scorer, list(st.session_state.slots))

compare_cols = st.columns(MAX_COMPARE_SLOTS, gap="small")
for col, (symbol, report, error), index in zip(
    compare_cols, results, range(MAX_COMPARE_SLOTS)
):
    with col:
        selected = index == int(st.session_state.active_slot)
        if error or report is None:
            _render_slot_error(symbol, error or "Could not score this ticker.", selected=selected)
        else:
            _render_compare_card(report, selected=selected)

ok_reports = [report for _symbol, report, error in results if report is not None and not error]
if ok_reports:
    rows = []
    for report in ok_reports:
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
