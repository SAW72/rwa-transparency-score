"""Streamlit demo for RAT Score (RWA Transparency Score).

Launch (fixtures, no API key):
    RWA_USE_FIXTURES=1 streamlit run app.py

Render binds 0.0.0.0:$PORT via render.yaml and must start with
``python -m rwa_score.health`` so GET /health is registered before the SPA.
The dashboard Start Command must match that launcher — ``streamlit run app.py``
leaves cold-start ``/health`` as Streamlit HTML.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import streamlit as st

from rwa_score.client import create_client, env_flag
from rwa_score.explainer import AI_FOOTNOTE, explain_score
from rwa_score.health import install_health_route, serve_health_if_requested
from rwa_score.score_card import share_score_card
from rwa_score.scorer import (
    PILLARS,
    WEIGHTS,
    ScoreError,
    TransparencyScorer,
    band_code,
    remaining_heuristic_paths,
)
from rwa_score.ticker_search import (
    CATEGORIES,
    SEARCH_MIN_CHARS,
    TickerOption,
    format_option,
    load_search_catalog,
    normalize_ticker,
    resolve_assign_symbol,
    search_tickers,
)
from rwa_score.verifiers import VerificationLevel
from rwa_score.x_client import x_credentials_ready

EXPLAIN_CACHE_TTL_SECONDS = 24 * 3600.0
# Per-symbol wall-clock cache for the explainer — process-local dict.
_explain_cache: dict[str, tuple[float, str]] = {}

install_health_route()

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


VERIFICATION_BADGE_COLORS = {
    VerificationLevel.SELF_REPORTED.value: "#8B949E",
    VerificationLevel.ON_CHAIN_POR.value: "#3DDC97",
    VerificationLevel.ATTESTED.value: "#58A6FF",
    VerificationLevel.EXAMINED.value: "#D2A8FF",
    "heuristic fallback": "#F08A24",
}


def _verification_badge_label(pillar_key: str, report: dict) -> tuple[str, str]:
    """Return (badge_text, evidence_line) for a pillar."""
    block = (report.get("verification") or {}).get(pillar_key) or {}
    level = block.get("level") or VerificationLevel.SELF_REPORTED.value
    source = block.get("source") or ""
    evidence = block.get("evidence") or "No evidence citation."
    if source == "heuristic_fallback" or "heuristic fallback" in (evidence or "").lower():
        badge = f"Verification: heuristic fallback · {level}"
    else:
        badge = f"Verification: {level}"
    return badge, evidence


def heuristic_legend_lines(report: dict | None = None) -> list[str]:
    """Educational leftover-heuristic lines. Never invents a live path."""
    base = [
        "GREEN/YELLOW/ORANGE/RED bands are automated heuristics — not audited attestations.",
        "Price, disclosure, and cross-issuer basis are always self-reported CMC (or fixture) fields.",
        "Backing / reserves / redemption are live only when a verifier hits a published source; otherwise **heuristic fallback** (labeled, never silent).",
        "xStocks names without a published Chainlink PoR aggregator stay heuristic — that is expected coverage, not a failed probe.",
        "Fixture mode uses bundled demo data and skips live attestation / PoR / redemption HTTP.",
        "Educational demo — not financial advice.",
    ]
    if not report:
        return base
    rows = remaining_heuristic_paths(
        report.get("verification") or {},
        data_source=str(report.get("data_source") or ""),
        live_verifiers=bool(report.get("live_verifiers")),
    )
    extra = [f"{row['label']}: {row['kind']}" for row in rows]
    return base + extra


def selected_slot_verification_lines(report: dict) -> list[str]:
    """Compact per-pillar verification labels for the active compare slot."""
    lines: list[str] = []
    for key in WEIGHTS:
        badge, _evidence = _verification_badge_label(key, report)
        lines.append(f"{PILLARS[key]['label']} — {badge}")
    return lines


FIXTURE_TICKERS = ["NVDA", "TSLA", "AAPL", "META"]
DEFAULT_SLOTS = ["NVDA", "TSLA", "AAPL", "META"]
MAX_COMPARE_SLOTS = 4
CANDIDATE_STRIP_LIMIT = 4
SEARCH_MATCH_KEY = "search_match_pick"
SEARCH_FIELD_MAX = "17rem"  # ~272px — ticker-sized, not full-bleed
# Process-local fallback when session_state is unavailable (unit tests).
_score_memo: dict[str, dict] = {}
_COMPANY_SUFFIXES = frozenset({"corp", "inc", "ltd", "llc", "co", "the", "plc", "sa"})
_DOT_BY_BAND = {"GREEN": "●", "YELLOW": "●", "ORANGE": "◐", "RED": "○"}

st.set_page_config(
    page_title=PAGE_TITLE,
    page_icon=str(FAVICON_PATH) if FAVICON_PATH.is_file() else "◎",
    layout="wide",
    initial_sidebar_state="expanded",
)
serve_health_if_requested()


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


def _score_cache_key(scorer: TransparencyScorer, symbol: str) -> str:
    source = str(getattr(scorer.client, "source", "") or "")
    return f"{source}:{symbol}"


def _report_cache() -> dict:
    """Session cache of scored reports. Falls back to the process memo."""
    try:
        cache = st.session_state.get("score_reports")
        if not isinstance(cache, dict):
            cache = {}
            st.session_state["score_reports"] = cache
        return cache
    except Exception:  # noqa: BLE001 — pytest / no ScriptRunContext
        return _score_memo


def _score_one(scorer: TransparencyScorer, ticker: str) -> dict:
    """Return a cached report. Unchanged slots are never re-scored."""
    symbol = normalize_ticker(ticker)
    key = _score_cache_key(scorer, symbol)
    session_cache = _report_cache()
    hit = session_cache.get(key)
    if hit is not None:
        _score_memo[key] = hit
        return hit
    hit = _score_memo.get(key)
    if hit is not None:
        session_cache[key] = hit
        return hit
    report = scorer.score(symbol)
    session_cache[key] = report
    _score_memo[key] = report
    return report


def _cached_explanation(report: dict) -> str:
    """24h per-symbol cache around ``explain_score``. Never raises."""
    symbol = str(report.get("ticker") or "").upper()
    now = time.monotonic()
    hit = _explain_cache.get(symbol)
    if hit is not None and (now - hit[0]) <= EXPLAIN_CACHE_TTL_SECONDS:
        return hit[1]
    try:
        text = explain_score(report)
    except Exception as exc:  # noqa: BLE001 — card must still render
        text = f"Explanation unavailable ({exc}). This is an automated summary, not financial advice."
    if symbol:
        _explain_cache[symbol] = (now, text)
    return text


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
    slots = list(st.session_state.slots)
    if len(slots) != MAX_COMPARE_SLOTS:
        slots = (slots + list(DEFAULT_SLOTS))[:MAX_COMPARE_SLOTS]
        st.session_state.slots = slots
    if "active_slot" not in st.session_state:
        st.session_state.active_slot = default_active_slot(slots)
    if not 0 <= int(st.session_state.active_slot) < MAX_COMPARE_SLOTS:
        st.session_state.active_slot = default_active_slot(slots)


def _place_in_slot(ticker: str, index: int) -> None:
    st.session_state.slots = assign_ticker_to_slot(list(st.session_state.slots), index, ticker)
    st.session_state.active_slot = index


def default_active_slot(slots: list[str]) -> int:
    """First empty slot, else slot 0."""
    for index, raw in enumerate(slots):
        if not normalize_ticker(raw):
            return index
    return 0


def next_place_index(slots: list[str], active: int, ticker: str = "") -> int:
    """First empty slot; if the row is full, the active replace target.

    ``ticker`` is unused — match and Use share this helper and always pass it.
    """
    _ = ticker
    for index, raw in enumerate(slots):
        if not normalize_ticker(raw):
            return index
    if 0 <= active < len(slots):
        return active
    return 0


def place_search_match(
    slots: list[str], active: int, ticker: str
) -> tuple[list[str], int]:
    """Search-match click and Use-chip click share this slot-fill path.

    Empty row: fill the first empty slot, then aim at the next empty.
    Full row: replace the active slot, then advance 0→1→2→3→0.
    """
    index = next_place_index(slots, active, ticker)
    updated = assign_ticker_to_slot(slots, index, ticker)
    if all(normalize_ticker(raw) for raw in updated):
        return updated, (index + 1) % MAX_COMPARE_SLOTS
    return updated, default_active_slot(updated)


def browse_categories(catalog: list[TickerOption]) -> tuple:
    """Chips for buckets that exist on the CMC/fixture map. Grows when the map does."""
    present = {cid for opt in catalog for cid in opt.categories}
    shown = tuple(cat for cat in CATEGORIES if cat.id in present)
    return shown or CATEGORIES


def chip_query(category) -> str:
    """Search text a chip should type — first documented keyword."""
    words = getattr(category, "keywords", ()) or ()
    return str(words[0] if words else getattr(category, "label", "") or "")


def short_company_name(name: str) -> str:
    """Drop Inc/Corp-style suffixes so cards show a short company name."""
    parts = [part for part in (name or "").strip().split() if part]
    while parts and parts[-1].rstrip(".,").lower() in _COMPANY_SUFFIXES:
        parts.pop()
    return " ".join(parts) or (name or "").strip()


def catalog_company(ticker: str, catalog: list[TickerOption]) -> str:
    """Underlying company name from the search directory, if present."""
    symbol = normalize_ticker(ticker)
    for opt in catalog:
        if opt.symbol == symbol:
            return short_company_name(opt.name)
    return ""


def mode_cue(report: dict) -> str:
    """LIVE vs fixture badge for a compare card."""
    return "FIXTURE" if report.get("data_source") == "fixture" else "LIVE"


def pillar_dots(report: dict) -> str:
    """One compact dot per pillar (filled / half / empty by band)."""
    subs = report.get("subscores") or {}
    return "".join(
        _DOT_BY_BAND.get(band_code(float(subs.get(key) or 0)), "·") for key in WEIGHTS
    )


def weakest_pillar_line(report: dict) -> str:
    """One-line weakest-pillar cue so cards are not score-only."""
    subs = report.get("subscores") or {}
    if not subs:
        return ""
    key = min(WEIGHTS, key=lambda item: float(subs.get(item) or 0))
    return f"Weakest: {PILLARS[key]['label']} {float(subs.get(key) or 0):.0f}"


def _maybe_rerun() -> None:
    """Rerun only inside a live Streamlit script (no-op in unit tests)."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
    except Exception:  # noqa: BLE001
        return
    if get_script_run_ctx() is None:
        return
    st.rerun()


def _auto_place(ticker: str) -> None:
    """Drop a pick into the next compare slot and advance (no Assign button)."""
    symbol = normalize_ticker(ticker)
    if not symbol:
        return
    if "slots" not in st.session_state:
        st.session_state.slots = list(DEFAULT_SLOTS)
    if "active_slot" not in st.session_state:
        st.session_state.active_slot = default_active_slot(
            list(st.session_state.slots)
        )
    updated, nxt = place_search_match(
        list(st.session_state.slots), int(st.session_state.active_slot), symbol
    )
    st.session_state.slots = updated
    st.session_state.active_slot = nxt
    # Drop Search + match dropdown on the next run (cannot mutate the
    # ticker_query widget after it already exists on this run).
    st.session_state["_clear_search"] = True
    _maybe_rerun()


def _render_search_picker(catalog: list[TickerOption], use_fixtures: bool) -> None:
    """Categories → compact Search + attached match dropdown.

    Chip click writes ``ticker_query`` before the Search box is created so
    matches appear on this run — no Enter, no extra rerun.
    After a successful place, ``_clear_search`` empties the box first so
    the match dropdown is not created.
    """
    if st.session_state.get("_clear_search"):
        st.session_state["_clear_search"] = False
        st.session_state.ticker_query = ""
        st.session_state.pop(SEARCH_MATCH_KEY, None)

    chip_cats = browse_categories(catalog)
    n_chips = max(len(chip_cats), 1)
    # Trailing spacer keeps chips content-sized / left-packed, not full-bleed.
    chip_cols = st.columns([1] * n_chips + [max(n_chips + 2, 6)], gap="small")
    for index, cat in enumerate(chip_cats):
        with chip_cols[index]:
            if st.button(
                chip_display_label(cat.label),
                key=f"cat_chip_{cat.id}",
                use_container_width=False,
            ):
                st.session_state.ticker_query = chip_query(cat)

    query = st.text_input(
        "Search",
        placeholder="Ticker, name, or category",
        label_visibility="visible",
        key="ticker_query",
    )
    matches = search_tickers(query, catalog, limit=CANDIDATE_STRIP_LIMIT)
    if matches:
        options = [opt.symbol for opt in matches]
        labels = {opt.symbol: format_option(opt) for opt in matches}
        stored = st.session_state.get(SEARCH_MATCH_KEY)
        if stored not in options and SEARCH_MATCH_KEY in st.session_state:
            del st.session_state[SEARCH_MATCH_KEY]
        picked = st.selectbox(
            "Matches",
            options,
            index=None,
            format_func=lambda symbol: labels.get(symbol, symbol),
            placeholder="Choose a ticker",
            key=SEARCH_MATCH_KEY,
            label_visibility="collapsed",
        )
        if picked:
            _auto_place(picked)
    else:
        st.session_state.pop(SEARCH_MATCH_KEY, None)
        if len((query or "").strip()) >= SEARCH_MIN_CHARS:
            st.caption("No directory matches — type a ticker or tap a category.")

    if use_fixtures:
        st.caption(
            "Fixture catalog: "
            + ", ".join(FIXTURE_TICKERS)
            + " + XOM, PLD. Prefix (NIV → NVDA / Nvidia) or a category "
            "(oil, AI, real estate, auto), then choose a match."
        )
    else:
        st.caption(
            "Live mode: the cached CMC RWA map is the directory. "
            "Prefix-match ticker/name or tap a category, then choose a match."
        )


def _ticker_catalog(scorer: TransparencyScorer) -> list[TickerOption]:
    """Directory the scorer already loads (live CMC map or fixture map)."""
    cached = getattr(scorer, "_search_catalog", None)
    if cached is not None:
        return cached
    catalog = load_search_catalog(scorer.client)
    scorer._search_catalog = catalog
    return catalog


def chip_display_label(label: str) -> str:
    """Chip text: spaces around slashes so wrap cannot split a word."""
    return (label or "").replace("/", " / ")


def _render_share_controls(report: dict, *, slot_index: int) -> None:
    """User-triggered signed PNG + optional X post. Never runs on page load."""
    ticker = str(report.get("ticker") or "UNK")
    state_key = f"share_card_{slot_index}_{ticker}"
    if st.button("Share score card", key=f"share_btn_{slot_index}_{ticker}"):
        try:
            st.session_state[state_key] = share_score_card(report)
        except Exception as exc:  # noqa: BLE001 — card UI must stay up
            st.session_state[state_key] = None
            st.error(f"Could not build score card: {exc}")
            return
    if not x_credentials_ready():
        st.caption("X credentials not set — share still builds a downloadable PNG.")
    bundle = st.session_state.get(state_key)
    if bundle is None:
        return
    if bundle.png_bytes:
        try:
            # Streamlit 1.39: st.image uses use_column_width, not use_container_width.
            st.image(bundle.png_bytes, use_column_width=True)
        except TypeError:
            st.image(bundle.png_bytes)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not preview score card: {exc}")
        st.download_button(
            "Download PNG",
            data=bundle.png_bytes,
            file_name=bundle.filename,
            mime="image/png",
            key=f"share_dl_{slot_index}_{ticker}",
        )
    st.caption(f"Signature fingerprint: `{bundle.fingerprint}`")
    if bundle.x_posted:
        st.success(bundle.x_message)
    elif bundle.x_message:
        st.info(bundle.x_message)


def _render_card_details(report: dict, *, slot_index: int = 0) -> None:
    """Share + explainer + pillars — unused on Score/Compare this PR."""
    if report.get("data_source") == "fixture":
        st.caption("Demo fixture data — not a live CoinMarketCap API response.")
    else:
        st.caption("Live CoinMarketCap data.")

    st.caption(
        "Backing / reserves / redemption use **issuer-name heuristics** when live "
        "attestation/PoR is unavailable — labeled **heuristic fallback**, not audited attestations."
    )

    basis_meta = report.get("basis") or {}
    if basis_meta.get("available"):
        spread = basis_meta.get("percent_spread")
        count = basis_meta.get("wrapper_count")
        st.caption(
            f"Cross-issuer basis: **{spread:.2f}%** spread across {count} wrappers "
            "(self-reported CMC market-pairs)."
        )
    elif basis_meta.get("wrapper_count") == 1:
        st.caption(
            "Cross-issuer basis: only one wrapper on CMC market-pairs — no issuer compare."
        )

    flags = report.get("flags") or []
    if flags:
        for flag in flags:
            st.warning(flag)
    else:
        st.caption("No risk flags on this pass.")

    _render_share_controls(report, slot_index=slot_index)

    st.write(_cached_explanation(report))
    st.caption(AI_FOOTNOTE)

    with st.expander("Pillar detail", expanded=False):
        for key in WEIGHTS:
            meta = PILLARS[key]
            badge_label, evidence = _verification_badge_label(key, report)
            st.markdown(
                f"**{meta['label']}** — {report['subscores'][key]:.0f}/100 "
                f"(weight {WEIGHTS[key]:.0%})"
            )
            st.caption(badge_label)
            st.caption(meta["what"])
            st.caption(f"Evidence: {evidence}")
            st.write(report["explanations"][key])

        notes = report.get("notes") or []
        if notes:
            st.markdown("**Notes**")
            for note in notes:
                st.info(note)


def _render_compare_card(
    report: dict,
    *,
    selected: bool = False,
    company: str = "",
    slot_index: int = 0,
) -> None:
    """Ticker · company · score · LIVE/FIXTURE · pillar dots / weakest."""
    del slot_index
    ticker = str(report.get("ticker") or "")
    title = f"● {ticker}" if selected else ticker
    score = float(report.get("score") or 0)
    company = company or short_company_name(str(report.get("issuer") or ""))
    cue = mode_cue(report)
    st.metric(title, f"{score:.1f}", f"{company} · {cue}")
    dots = pillar_dots(report)
    weak = weakest_pillar_line(report)
    if dots and weak:
        st.caption(f"{dots}  {weak}")
    elif weak:
        st.caption(weak)
    elif dots:
        st.caption(dots)


def _render_slot_error(ticker: str, message: str, *, selected: bool = False) -> None:
    label = ticker or "Empty slot"
    if selected:
        label = f"● {label}"
    st.metric(label, "—", "Unavailable")
    st.error(message)


def _render_empty_slot(*, selected: bool = False) -> None:
    title = "● Empty slot" if selected else "Empty slot"
    st.metric(title, "—")
    st.caption("Pick a ticker to compare here.")


def _render_selected_slot_detail(report: dict, *, slot_index: int = 0) -> None:
    """Active-slot pillars under the row — no empty right gutter."""
    subs = report.get("subscores") or {}
    bits = [
        f"**{PILLARS[key]['label']}** {subs[key]:.0f}"
        for key in WEIGHTS
        if key in subs
    ]
    if bits:
        st.caption(" · ".join(bits))
    for line in selected_slot_verification_lines(report):
        st.caption(line)
    if report.get("data_source") == "fixture" or not report.get("live_verifiers", True):
        st.caption("Offline/fixture path — live attestation hooks skipped; leftover pillars are heuristic or self-reported.")
    leftover = [
        row
        for row in remaining_heuristic_paths(
            report.get("verification") or {},
            data_source=str(report.get("data_source") or ""),
            live_verifiers=bool(report.get("live_verifiers")),
        )
        if row["kind"] in {"heuristic_fallback", "offline_skip", "fixture"}
    ]
    for row in leftover:
        st.caption(f"Heuristic note · {row['label']}: {row['kind']}")
    with st.expander("Share score card", expanded=False):
        _render_share_controls(report, slot_index=slot_index)


st.markdown(
    f"""
    <style>
      /* Compact Search + attached dropdown. Light 1px border, not full-bleed. */
      div[data-testid="stTextInput"],
      div[data-testid="stSelectbox"] {{
        max-width: {SEARCH_FIELD_MAX};
        position: relative;
        z-index: 40;
      }}
      div[data-testid="stTextInput"] [data-baseweb="input"],
      div[data-testid="stSelectbox"] [data-baseweb="select"] > div {{
        border: 1px solid rgba(250, 250, 250, 0.2) !important;
        box-shadow: none !important;
      }}
      [data-baseweb="popover"],
      [data-baseweb="menu"] {{
        z-index: 1000 !important;
      }}
    </style>
    """,
    unsafe_allow_html=True,
)

st.header(BRAND_H1)
st.caption(BRAND_SUB)
st.caption(TAGLINE)

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

    st.markdown("### How scores are labeled")
    for line in heuristic_legend_lines():
        st.caption(line)

    st.markdown("### Disclaimer")
    st.write(DISCLAIMER)

    st.markdown("### Legal")
    st.markdown("[Privacy Policy](/privacy) · [Terms of Service](/terms)")
    st.caption("These do not replace the Disclaimer.")

mode_label = "Fixture" if use_fixtures else "Live"
st.caption(f"Mode: {mode_label}")
st.caption(
    "Remaining heuristics stay labeled: name-list fallback, self-reported CMC "
    "price/disclosure/basis, and xStocks without a published PoR proxy. "
    "Educational demo — not financial advice."
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
    "Browse a category, type a ticker or name (3+ chars), then choose a match. "
    "Click a slot to choose which one the next pick replaces."
)

catalog = _ticker_catalog(scorer)
_render_search_picker(catalog, use_fixtures)

active = int(st.session_state.active_slot)
target = next_place_index(list(st.session_state.slots), active)
target_symbol = st.session_state.slots[target]

slot_cols = st.columns(MAX_COMPARE_SLOTS, gap="small")
for index, symbol in enumerate(st.session_state.slots):
    with slot_cols[index]:
        selected = index == target
        slot_label = symbol or f"Slot {index + 1}"
        label = f"● {slot_label}" if selected else slot_label
        if st.button(
            label,
            key=f"slot_{index}",
            type="primary" if selected else "secondary",
            use_container_width=True,
        ):
            st.session_state.active_slot = index

active = int(st.session_state.active_slot)
target = next_place_index(list(st.session_state.slots), active)
target_symbol = st.session_state.slots[target]
if target_symbol:
    st.caption(
        f"Next pick replaces slot {target + 1} · **{target_symbol}**. "
        "Click a slot to change the target."
    )
else:
    st.caption(
        f"Next pick fills slot {target + 1}. "
        "When the row is full, the highlighted slot is replaced."
    )

results = _score_slots(scorer, list(st.session_state.slots))

# Hide empty compare boxes until a ticker is assigned; filled slots stay in one row.
if any(symbol for symbol, _report, _error in results):
    company_names = {opt.symbol: short_company_name(opt.name) for opt in catalog}
    compare_cols = st.columns(MAX_COMPARE_SLOTS, gap="small")
    for col, (symbol, report, error), index in zip(
        compare_cols, results, range(MAX_COMPARE_SLOTS)
    ):
        with col:
            selected = index == target
            if not symbol:
                _render_empty_slot(selected=selected)
            elif error or report is None:
                _render_slot_error(symbol, error or "Could not score this ticker.", selected=selected)
            else:
                _render_compare_card(
                    report,
                    selected=selected,
                    company=company_names.get(symbol, ""),
                    slot_index=index,
                )
    if 0 <= target < len(results):
        _sel_symbol, sel_report, sel_error = results[target]
        if sel_report is not None and not sel_error:
            _render_selected_slot_detail(sel_report, slot_index=target)

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
st.markdown("[Privacy Policy](/privacy) · [Terms of Service](/terms)")
