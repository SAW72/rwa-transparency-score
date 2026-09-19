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
import html
import os
import time
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from rwa_score.ask_rat import (
    ASK_RAT_CHIPS,
    ASK_RAT_GREETING,
    ask as ask_rat,
    format_ask_cmc_lines,
)
from rwa_score.client import create_client, env_flag, summarize_call_log
from rwa_score.explainer import AI_FOOTNOTE, explain_score
from rwa_score.health import install_health_route, serve_health_if_requested
from rwa_score.score_card import (
    PNG_BUILD_FAILED_PREFIX,
    attach_x_share,
    share_score_card,
)
from rwa_score.scorer import (
    ALWAYS_SELF_REPORTED,
    LIVE_OR_HEURISTIC,
    PILLARS,
    WEIGHTS,
    ScoreError,
    TransparencyScorer,
    band_code,
    remaining_heuristic_paths,
)
from rwa_score.ticker_search import (
    CATEGORIES,
    CATEGORY_BY_ID,
    RWA_CLASS_CATEGORIES,
    SEARCH_MIN_CHARS,
    TickerOption,
    format_option,
    load_search_catalog,
    normalize_ticker,
    resolve_assign_symbol,
    search_tickers,
)
from rwa_score.verifiers import VerificationLevel
from rwa_score.x_client import (
    MISSING_CREDS_MESSAGE,
    user_facing_x_skip_message,
    x_credentials_ready,
)

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
# Dark ink on bright chips; white on RED so the label stays readable.
_BAND_CHIP_INK = {
    "GREEN": "#0D1117",
    "YELLOW": "#0D1117",
    "ORANGE": "#0D1117",
    "RED": "#FFFFFF",
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
    meta = block.get("meta") or {}
    published = meta.get("published_por_feed")
    if pillar_key in ALWAYS_SELF_REPORTED:
        # LIVE/FIXTURE is the data source, not an oracle. Do not label CMC-only
        # pillars as the card's overall "Verification:" line.
        badge = "CMC field (self-reported)"
        return badge, evidence
    if source == "heuristic_fallback" or "heuristic fallback" in (evidence or "").lower():
        if published and meta.get("por_path") == "fixture_labeled_skip":
            badge = (
                f"Verification: heuristic fallback · published Chainlink PoR "
                f"({published}, fixture/offline skip)"
            )
        else:
            badge = f"Verification: heuristic fallback · {level}"
    else:
        badge = f"Verification: {level}"
    return badge, evidence


def heuristic_legend_lines(report: dict | None = None) -> list[str]:
    """Educational leftover-heuristic lines. Never invents a live path."""
    base = [
        "GREEN/YELLOW/ORANGE/RED bands are automated heuristics — not audited attestations.",
        "Price, disclosure, and cross-issuer basis are always self-reported CMC (or fixture) fields "
        "(RWA quotes/latest plus market-pairs; crypto 24hΔ is a labeled fallback).",
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
    """Compact per-pillar verification labels for the active compare slot.

    First line distinguishes LIVE/FIXTURE data from the strongest
    backing/reserves/redemption check so a card with on-chain PoR is not
    summarized as ``Verification: self-reported``.
    """
    mode = mode_cue(report)
    cue = verification_cue(report)
    lines = [f"{mode} data · strongest check: {cue}"]
    for key in WEIGHTS:
        badge, _evidence = _verification_badge_label(key, report)
        lines.append(f"{PILLARS[key]['label']} — {badge}")
    return lines


def sidebar_legend_markdown(lines: list[str] | None = None) -> str:
    """One markdown block for the labeling expander — not N caption widgets."""
    rows = lines if lines is not None else heuristic_legend_lines()
    return "\n".join(f"- {line}" for line in rows)


def sidebar_weights_markdown() -> str:
    """Compact pillar list: label, weight, and what — one line each."""
    return "\n".join(
        f"- **{PILLARS[key]['label']}** — {weight:.0%} — {PILLARS[key]['what']}"
        for key, weight in WEIGHTS.items()
    )


FIXTURE_TICKERS = ["NVDA", "TSLA", "AAPL", "META"]
DEFAULT_SLOTS = ["NVDA", "TSLA", "AAPL", "META"]
MAX_COMPARE_SLOTS = 4
# Pillar / score gaps below these stay unlabeled — do not invent a "driver".
COMPARE_MIN_SCORE_DELTA = 5.0
COMPARE_MIN_PILLAR_DELTA = 8.0
COMPARE_MIN_WEIGHTED_DELTA = 1.2
_VERIFICATION_RANK = {
    VerificationLevel.ON_CHAIN_POR.value: 4,
    VerificationLevel.ATTESTED.value: 3,
    VerificationLevel.EXAMINED.value: 2,
    "heuristic fallback": 1,
    VerificationLevel.SELF_REPORTED.value: 0,
}
CANDIDATE_STRIP_LIMIT = 4
SEARCH_MATCH_KEY = "search_match_pick"
SEARCH_FIELD_MAX = "17rem"  # ~272px — ticker-sized, not full-bleed
# Streamlit 1.39 text_input commits on Enter/blur only. Debounced input
# events commit the same widget so Matches update as the user types.
SEARCH_TYPEAHEAD_DEBOUNCE_MS = 150
SEARCH_TYPEAHEAD_JS = r"""
(function () {
  var win = window.parent;
  var doc = win.document;
  var DELAY = __DEBOUNCE_MS__;
  var BOUND = "data-rwa-typeahead";

  function setKeepFocus(on) {
    win.__rwaSearchKeepFocus = !!on;
    try {
      if (on) win.sessionStorage.setItem("rwaSearchKeepFocus", "1");
      else win.sessionStorage.removeItem("rwaSearchKeepFocus");
    } catch (err) {}
  }

  function keepFocus() {
    if (win.__rwaSearchKeepFocus) return true;
    try { return win.sessionStorage.getItem("rwaSearchKeepFocus") === "1"; }
    catch (err) { return false; }
  }

  function findSearchInput() {
    var blocks = doc.querySelectorAll('[data-testid="stTextInput"]');
    var i;
    for (i = 0; i < blocks.length; i++) {
      var label = blocks[i].querySelector("label");
      if (label && String(label.textContent || "").trim() === "Search") {
        return blocks[i].querySelector("input");
      }
    }
    return doc.querySelector('[data-testid="stTextInput"] input');
  }

  function fireEnter(input) {
    var specs = [
      { type: "keydown", key: "Enter", code: "Enter", keyCode: 13, which: 13 },
      { type: "keypress", key: "Enter", code: "Enter", keyCode: 13, which: 13 },
      { type: "keyup", key: "Enter", code: "Enter", keyCode: 13, which: 13 }
    ];
    var i;
    for (i = 0; i < specs.length; i++) {
      try { input.dispatchEvent(new KeyboardEvent(specs[i].type, Object.assign({
        bubbles: true, cancelable: true
      }, specs[i]))); } catch (err) {}
    }
  }

  function commitTypedValue(input) {
    if (!input || doc.activeElement !== input) return;
    fireEnter(input);
    input.blur();
  }

  function scheduleCommit(input) {
    setKeepFocus(true);
    if (input._rwaTimer) win.clearTimeout(input._rwaTimer);
    input._rwaTimer = win.setTimeout(function () { commitTypedValue(input); }, DELAY);
  }

  function bind(input) {
    if (!input || input.getAttribute(BOUND) === "1") return;
    input.setAttribute(BOUND, "1");
    input.addEventListener("input", function () { scheduleCommit(input); });
    input.addEventListener("keyup", function (ev) {
      if (ev.key === "Enter" || ev.keyCode === 13) return;
      scheduleCommit(input);
    });
  }

  function restoreFocus(input) {
    if (!input || !keepFocus()) return;
    input.focus();
    try {
      var len = (input.value || "").length;
      input.setSelectionRange(len, len);
    } catch (err) {}
  }

  function attach() {
    var input = findSearchInput();
    if (!input) return;
    bind(input);
    restoreFocus(input);
  }

  if (!win.__rwaTypeaheadInstalled) {
    win.__rwaTypeaheadInstalled = true;
    doc.addEventListener("mousedown", function (ev) {
      var t = ev.target;
      if (t && t.closest && t.closest('[data-testid="stTextInput"]')) return;
      setKeepFocus(false);
    }, true);
    attach();
    new win.MutationObserver(attach).observe(doc.body, { childList: true, subtree: true });
  } else {
    attach();
  }
})();
"""
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


def health_launcher_reminder(*, launcher_set: bool | None = None) -> str | None:
    """Warn when this process was started with ``streamlit run`` instead of the health launcher.

    Does not claim the Render dashboard was changed.
    """
    if launcher_set is None:
        launcher_set = os.getenv("RWA_HEALTH_LAUNCHER") == "1"
    if launcher_set:
        return None
    return (
        "This process was started with `streamlit run app.py`, not "
        "`python -m rwa_score.health`. Cold-start GET /health can be Streamlit "
        "SPA HTML (`text/html`). Render **Start Command** must match "
        "`render.yaml` (`python -m rwa_score.health --server.port $PORT …`). "
        "This app cannot change the Render dashboard."
    )


def collect_cmc_calls(reports: list[dict], client: object) -> dict:
    """Union of per-score journals plus leftover client log for this Streamlit run."""
    source = getattr(client, "source", "unknown")
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for report in reports:
        block = report.get("cmc_calls") or {}
        for row in block.get("endpoints") or []:
            key = (row.get("endpoint"), row.get("source"), row.get("via"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    fetch = getattr(client, "call_log", None)
    extra = list(fetch()) if callable(fetch) else []
    merged = summarize_call_log([*rows, *extra], client_source=str(source))
    return merged


def format_cmc_calls_lines(block: dict) -> list[str]:
    """Human lines for the CMC-calls evidence strip. Fixture never says live."""
    source = "fixture" if block.get("source") == "fixture" else "live"
    if source == "fixture":
        header = "CMC calls this run — Fixture (bundled demo, **not** live CMC)"
    else:
        header = "CMC calls this run — Live CMC (not fixtures)"
    lines = [header]
    endpoints = block.get("endpoints") or []
    if not endpoints:
        lines.append("No CMC/fixture directory calls recorded on this run yet.")
        return lines
    for row in endpoints:
        endpoint = row.get("endpoint") or ""
        via = row.get("via") or ("fixture" if source == "fixture" else "network")
        tag = "fixture" if source == "fixture" else ("cache" if row.get("cached") or via == "cache" else "live")
        lines.append(f"`GET {endpoint}` · {tag}")
    return lines


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
    """CMC RWA classes always; industry / crypto chips only when the catalog has rows."""
    present = {cid for opt in catalog for cid in opt.categories}
    rwa = tuple(RWA_CLASS_CATEGORIES)
    extra = tuple(
        cat
        for cat in CATEGORIES
        if cat.id not in {c.id for c in rwa} and cat.id in present
    )
    return rwa + extra


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


def normalize_band(band: str | None, *, score: float | None = None) -> str:
    """Canonical GREEN/YELLOW/ORANGE/RED. Never invents a new band name."""
    code = str(band or "").strip().upper()
    if code in BAND_COLORS:
        return code
    if score is not None:
        return band_code(float(score))
    return ""


def band_chip_spec(band: str | None, *, score: float | None = None) -> dict[str, str]:
    """Color + label for the band pill. Empty when the band is unknown."""
    code = normalize_band(band, score=score)
    if code not in BAND_COLORS:
        return {"band": "", "label": "", "color": "", "text_color": ""}
    return {
        "band": code,
        "label": code,
        "color": BAND_COLORS[code],
        "text_color": _BAND_CHIP_INK[code],
    }


def band_chip_html(
    band: str | None,
    *,
    score: float | None = None,
    selected: bool = False,
) -> str:
    """Colored GREEN/YELLOW/ORANGE/RED pill. Empty string if band is unknown."""
    spec = band_chip_spec(band, score=score)
    if not spec["band"]:
        return ""
    extra = " rat-band-chip-selected" if selected else ""
    label = html.escape(spec["band"])
    return (
        f'<span class="rat-band-chip rat-band-{spec["band"].lower()}{extra}" '
        f'style="background:{spec["color"]};color:{spec["text_color"]}">'
        f"{label}</span>"
    )


def pillar_evidence_rows(report: dict) -> list[dict]:
    """Existing per-pillar badge + evidence. No new feeds or invented metrics."""
    subs = report.get("subscores") or {}
    explanations = report.get("explanations") or {}
    rows: list[dict] = []
    for key in WEIGHTS:
        badge, evidence = _verification_badge_label(key, report)
        meta = PILLARS[key]
        rows.append(
            {
                "key": key,
                "label": meta["label"],
                "what": meta["what"],
                "score": float(subs.get(key) or 0),
                "weight": float(WEIGHTS[key]),
                "badge": badge,
                "evidence": evidence,
                "explanation": str(explanations.get(key) or ""),
            }
        )
    return rows


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


def _join_tickers(tickers: list[str]) -> str:
    """Compact ticker list for captions (max three names)."""
    names = [str(t).strip() for t in tickers if str(t).strip()]
    if not names:
        return ""
    if len(names) <= 3:
        return "/".join(names)
    return f"{names[0]}/{names[1]}…"


def pillar_verification_kind(pillar_key: str, report: dict) -> str:
    """Normalize one pillar to a compact badge kind. Never invents a live path."""
    block = (report.get("verification") or {}).get(pillar_key) or {}
    level = str(block.get("level") or VerificationLevel.SELF_REPORTED.value)
    source = str(block.get("source") or "")
    evidence = str(block.get("evidence") or "")
    if source == "heuristic_fallback" or "heuristic fallback" in evidence.lower():
        return "heuristic fallback"
    return level


def verification_cue(report: dict) -> str:
    """Strongest backing/reserves/redemption badge for a compare card."""
    kinds = [pillar_verification_kind(key, report) for key in LIVE_OR_HEURISTIC]
    if not kinds:
        return VerificationLevel.SELF_REPORTED.value
    return max(kinds, key=lambda kind: _VERIFICATION_RANK.get(kind, 0))


def _empty_compare_diff() -> dict:
    return {
        "available": False,
        "headline": "",
        "lines": [],
        "score_spread": 0.0,
        "bands": {},
        "largest_delta": None,
        "verification_groups": {},
        "verification_differs": False,
        "cards": {},
    }


def _band_summary(reports: list[dict]) -> str:
    grouped: dict[str, list[str]] = {}
    for report in reports:
        band = str(report.get("band") or band_code(float(report.get("score") or 0)))
        ticker = str(report.get("ticker") or "")
        if ticker:
            grouped.setdefault(band, []).append(ticker)
    parts = []
    for band in ("GREEN", "YELLOW", "ORANGE", "RED"):
        names = grouped.get(band) or []
        if names:
            parts.append(f"{band} {_join_tickers(names)}")
    return " · ".join(parts)


def _largest_pillar_delta(reports: list[dict]) -> dict | None:
    """Pillar that contributes most to the score gap (delta × published weight)."""
    best: dict | None = None
    best_weighted = -1.0
    for key in WEIGHTS:
        values: list[tuple[str, float]] = []
        for report in reports:
            ticker = str(report.get("ticker") or "")
            if not ticker:
                continue
            subs = report.get("subscores") or {}
            if key not in subs:
                continue
            values.append((ticker, float(subs.get(key) or 0)))
        if len(values) < 2:
            continue
        high_val = max(item[1] for item in values)
        low_val = min(item[1] for item in values)
        spread = high_val - low_val
        weighted = spread * float(WEIGHTS[key])
        if weighted < best_weighted:
            continue
        if weighted == best_weighted and best is not None:
            continue
        best_weighted = weighted
        best = {
            "pillar": key,
            "label": PILLARS[key]["label"],
            "high": high_val,
            "low": low_val,
            "spread": spread,
            "weighted": weighted,
            "high_tickers": [t for t, val in values if val == high_val],
            "low_tickers": [t for t, val in values if val == low_val],
        }
    if best is None:
        return None
    if (
        float(best["spread"]) < COMPARE_MIN_PILLAR_DELTA
        or float(best["weighted"]) < COMPARE_MIN_WEIGHTED_DELTA
    ):
        return None
    return best


def compare_differentiation(reports: list[dict] | None) -> dict:
    """Why 2–4 scored slots differ — headline, lines, per-card contrast.

    Uses only published scores, bands, weights, and verification badges.
    """
    scored = [
        report
        for report in (reports or [])
        if report and str(report.get("ticker") or "").strip()
    ]
    if len(scored) < 2:
        return _empty_compare_diff()

    ranked = sorted(scored, key=lambda row: float(row.get("score") or 0), reverse=True)
    high, low = ranked[0], ranked[-1]
    high_ticker = str(high.get("ticker") or "")
    low_ticker = str(low.get("ticker") or "")
    score_spread = float(high.get("score") or 0) - float(low.get("score") or 0)
    bands = {
        str(report.get("ticker") or ""): str(
            report.get("band") or band_code(float(report.get("score") or 0))
        )
        for report in scored
        if report.get("ticker")
    }
    largest = _largest_pillar_delta(scored)

    groups: dict[str, list[str]] = {}
    for report in scored:
        ticker = str(report.get("ticker") or "")
        groups.setdefault(verification_cue(report), []).append(ticker)
    verification_differs = len(groups) > 1

    issuers: dict[str, list[str]] = {}
    for report in scored:
        ticker = str(report.get("ticker") or "")
        issuer = short_company_name(str(report.get("issuer") or "")) or "unknown"
        issuers.setdefault(issuer, []).append(ticker)
    issuers_differ = len(issuers) > 1

    headline_bits: list[str] = []
    if score_spread >= COMPARE_MIN_SCORE_DELTA and high_ticker and low_ticker:
        headline_bits.append(
            f"{low_ticker} trails {high_ticker} by {score_spread:.1f}"
        )
    elif len(set(bands.values())) > 1:
        headline_bits.append(f"Bands split {_band_summary(scored)}")
    else:
        headline_bits.append("Scores are close")
    if largest is not None:
        headline_bits.append(
            f"{largest['label']} drives the gap "
            f"({largest['high']:.0f} vs {largest['low']:.0f})"
        )
    headline = " — ".join(headline_bits) + "."

    lines: list[str] = []
    if largest is not None:
        lines.append(
            f"Largest pillar gap: {largest['label']} "
            f"{largest['high']:.0f} ({_join_tickers(largest['high_tickers'])}) vs "
            f"{largest['low']:.0f} ({_join_tickers(largest['low_tickers'])})."
        )
    band_line = _band_summary(scored)
    if band_line:
        lines.append(f"Bands: {band_line}.")
    if verification_differs:
        ordered = sorted(
            groups.items(),
            key=lambda item: _VERIFICATION_RANK.get(item[0], 0),
            reverse=True,
        )
        bits = [f"{kind} ({_join_tickers(names)})" for kind, names in ordered]
        lines.append(f"Verification: {' · '.join(bits)}.")
    elif groups:
        kind = next(iter(groups))
        lines.append(
            f"Verification: all {kind} — labeled, not an audited attestation."
        )
    if issuers_differ and len(lines) < 4:
        issuer_bits = [
            f"{_join_tickers(names)} {issuer}" for issuer, names in issuers.items()
        ]
        lines.append(f"Issuers: {' · '.join(issuer_bits)}.")

    cards: dict[str, dict] = {}
    for report in scored:
        ticker = str(report.get("ticker") or "")
        score = float(report.get("score") or 0)
        band = bands.get(ticker) or band_code(score)
        cue = verification_cue(report)
        contrast = ""
        role = ""
        if largest is not None:
            label = str(largest["label"])
            if ticker in largest["high_tickers"]:
                peer = _join_tickers(largest["low_tickers"])
                contrast = (
                    f"▲ {label} {largest['high']:.0f} vs {peer} {largest['low']:.0f}"
                )
                role = "high"
            elif ticker in largest["low_tickers"]:
                peer = _join_tickers(largest["high_tickers"])
                contrast = (
                    f"▼ {label} {largest['low']:.0f} vs {peer} {largest['high']:.0f}"
                )
                role = "low"
        if ticker == high_ticker:
            row_role = f"Highest in this row ({score:.1f} {band})."
        elif ticker == low_ticker:
            row_role = f"Lowest in this row ({score:.1f} {band})."
        else:
            gap = float(high.get("score") or 0) - score
            row_role = f"{score:.1f} {band} · {gap:.1f} below {high_ticker}."
        cards[ticker] = {
            "band": band,
            "verification": cue,
            "contrast": contrast,
            "delta_role": role,
            "row_role": row_role,
        }

    return {
        "available": True,
        "headline": headline,
        "lines": lines[:4],
        "score_spread": round(score_spread, 1),
        "bands": bands,
        "largest_delta": largest,
        "verification_groups": groups,
        "verification_differs": verification_differs,
        "cards": cards,
    }


def card_contrast_line(ticker: str, diff: dict | None) -> str:
    """Per-card high/low pillar cue from ``compare_differentiation``."""
    row = ((diff or {}).get("cards") or {}).get(str(ticker or "")) or {}
    return str(row.get("contrast") or "")


def selected_slot_contrast_line(ticker: str, diff: dict | None) -> str:
    """One-line rank of the active slot against the rest of the row."""
    row = ((diff or {}).get("cards") or {}).get(str(ticker or "")) or {}
    return str(row.get("row_role") or "")


def _maybe_rerun() -> None:
    """Rerun only inside a live Streamlit script (no-op in unit tests).

    Search place-into-slot only. Share click / preview / download must never
    call this — ``st.rerun()`` after Share is an MPA Page-not-found trigger.
    """
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
    except Exception:  # noqa: BLE001
        return
    if get_script_run_ctx() is None:
        return
    st.rerun()


_EXPANDER_SUPPORTS_KEY: bool | None = None


def open_expander(label: str, *, expanded: bool = False, key: str | None = None):
    """Expander with a unique key when Streamlit accepts ``key=``.

    Streamlit 1.39's expander has no ``key`` — identical labels across the
    four compare columns can collide. Prefer a per-slot key; fall back to
    the label-only API so the demo never crashes on the pinned version.
    """
    global _EXPANDER_SUPPORTS_KEY
    if key and _EXPANDER_SUPPORTS_KEY is not False:
        try:
            widget = st.expander(label, expanded=expanded, key=key)
            _EXPANDER_SUPPORTS_KEY = True
            return widget
        except TypeError:
            _EXPANDER_SUPPORTS_KEY = False
    return st.expander(label, expanded=expanded)


def share_session_keys(slot_index: int, ticker: str) -> tuple[str, str]:
    """Session keys for the PNG bundle and a pending X post."""
    symbol = str(ticker or "UNK")
    return (
        f"share_card_{slot_index}_{symbol}",
        f"share_x_pending_{slot_index}_{symbol}",
    )


def share_card_preview_html(png_bytes: bytes, filename: str) -> str:
    """Self-contained PNG preview + download. No Streamlit media/component URLs."""
    b64 = base64.b64encode(png_bytes).decode("ascii")
    safe_name = html.escape(filename or "rat-score.png", quote=True)
    return (
        '<div class="rat-share-card">'
        f'<img alt="RAT Score card" src="data:image/png;base64,{b64}" '
        'style="width:100%;height:auto;border-radius:8px;display:block;" />'
        '<p style="margin:0.65rem 0 0;">'
        f'<a download="{safe_name}" href="data:image/png;base64,{b64}">Download PNG</a>'
        "</p></div>"
    )


def _show_share_png(png_bytes: bytes, filename: str = "rat-score.png") -> None:
    """Preview + download as markdown data URIs — never a Streamlit media route.

    ``st.image`` / ``st.download_button`` register ``/media`` and
    ``/_stcore/download``. ``components.html`` is an iframe proto (srcdoc /
    ``stIFrame``). Streamlit 1.39 MPA v1 (any ``pages/`` tree) treats those
    mounts as unknown pages and shows **Page not found** on the Share click
    rerun — the residual live fail after #36. Markdown HTML stays inline
    (no iframe, no media endpoint, no ``st.rerun()``). ``pages/`` is also
    gone so the MPA router is off.
    """
    try:
        st.markdown(
            share_card_preview_html(png_bytes, filename),
            unsafe_allow_html=True,
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not preview score card: {exc}")


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


def _on_search_query_change() -> None:
    """Drop a stale Matches pick when Search text changes (type-ahead or Enter)."""
    st.session_state.pop(SEARCH_MATCH_KEY, None)


def search_typeahead_script(debounce_ms: int = SEARCH_TYPEAHEAD_DEBOUNCE_MS) -> str:
    """JS that commits the Search box on input so Matches update without Enter."""
    delay = max(0, int(debounce_ms))
    return SEARCH_TYPEAHEAD_JS.replace("__DEBOUNCE_MS__", str(delay))


def _install_search_typeahead() -> None:
    """Bridge Streamlit's Enter/blur-only text_input to live-as-you-type search.

    This 1px iframe is the only ``components.html`` in the app. Share preview
    must not use it — mounting a ``/component`` iframe on the Share click
    rerun is the residual MPA **Page not found** after #36.
    """
    components.html(
        f"<script>{search_typeahead_script()}</script>",
        height=1,
        scrolling=False,
    )


def _render_search_picker(catalog: list[TickerOption], use_fixtures: bool) -> None:
    """Categories → compact Search + attached match dropdown.

    Chip click writes ``ticker_query`` before the Search box is created so
    matches appear on this run — no Enter, no extra rerun.
    Typed queries commit on each keystroke (debounced) so Matches appear at
    3+ characters without Enter. After a successful place, ``_clear_search``
    empties the box first so the match dropdown is not created.
    """
    if st.session_state.get("_clear_search"):
        st.session_state["_clear_search"] = False
        st.session_state.ticker_query = ""
        st.session_state.pop(SEARCH_MATCH_KEY, None)
        try:
            del st.query_params["rwa_cat"]
        except (KeyError, TypeError):
            pass

    chip_cats = browse_categories(catalog)
    raw_cat = st.query_params.get("rwa_cat")
    if raw_cat:
        cid = raw_cat if isinstance(raw_cat, str) else str(raw_cat)
        cat = CATEGORY_BY_ID.get(cid) or next(
            (row for row in chip_cats if row.id == cid), None
        )
        if cat is not None:
            st.session_state.ticker_query = chip_query(cat)
        try:
            del st.query_params["rwa_cat"]
        except (KeyError, TypeError):
            pass

    pills = []
    for cat in chip_cats:
        label = html.escape(chip_display_label(cat.label))
        cid = html.escape(cat.id)
        pills.append(
            f'<a class="rat-cat-pill" href="?rwa_cat={cid}" target="_self">{label}</a>'
        )
    st.markdown(
        f'<div class="rat-cat-row" role="list">{"".join(pills)}</div>',
        unsafe_allow_html=True,
    )

    query = st.text_input(
        "Search",
        placeholder="Ticker, name, or category",
        label_visibility="visible",
        key="ticker_query",
        on_change=_on_search_query_change,
    )
    _install_search_typeahead()
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
            + " + XOM, PLD, GOLD, USTB, SPY, EUR, HOME + Backed bTokens "
            "(bNVDA, bIB01, bCSPX, bC3M, bIBTA). "
            "Prefix (NIV → NVDA / Nvidia, bNV → bNVDA) or a CMC RWA class "
            "(Stocks, Commodities, Treasuries, ETFs, Real Estate, Currencies), "
            "then choose a match. Fixture mode is not live CMC."
        )
    else:
        st.caption(
            "Live mode: paginated CMC RWA map + ``assets/list`` "
            "(every official asset_type) plus published Backed bToken PoR symbols "
            "(bNVDA, …). Prefix-match ticker/name or tap a CMC RWA class, "
            "then choose a match. BTC/ETH are Crypto / Digital Assets, not RWA."
        )


def _ticker_catalog(scorer: TransparencyScorer) -> list[TickerOption]:
    """CMC/fixture map plus published Backed bToken PoR symbols.

    Always goes through the client so ``assets/list`` / map cache hits appear
    on the CMC-calls evidence strip. HTTP is still TTL-cached on the client.
    """
    return load_search_catalog(scorer.client)


def chip_display_label(label: str) -> str:
    """Chip text: spaces around slashes so wrap cannot split a word."""
    return (label or "").replace("/", " / ")


def _render_share_controls(report: dict, *, slot_index: int) -> None:
    """User-triggered signed PNG + optional X post. Never runs on page load.

    PNG is stored first. Preview/download use ``st.markdown`` data URIs
    (not ``st.image`` / ``st.download_button`` / ``components.html``) so no
    ``/media``, ``/_stcore/download``, or ``/component`` URL is registered.
    Do not ``st.rerun()`` after the click — that also trips Page not found.
    """
    ticker = str(report.get("ticker") or "UNK")
    state_key, pending_key = share_session_keys(slot_index, ticker)
    st.session_state.pop(pending_key, None)
    if st.button("Share score card", key=f"share_btn_{slot_index}_{ticker}"):
        try:
            bundle = share_score_card(report, post_to_x=False)
        except Exception as exc:  # noqa: BLE001 — card UI must stay up
            st.session_state[state_key] = None
            st.error(f"Could not build score card: {exc}")
            return
        if bundle.png_bytes and x_credentials_ready():
            st.session_state[state_key] = bundle
            bundle = attach_x_share(bundle)
        elif bundle.png_bytes:
            bundle.x_message = MISSING_CREDS_MESSAGE
        st.session_state[state_key] = bundle
    if not x_credentials_ready():
        st.caption("X credentials not set — share still builds a downloadable PNG.")
    bundle = st.session_state.get(state_key)
    if bundle is None:
        st.caption("Click Share score card to build a signed PNG preview.")
        return
    if not bundle.png_bytes:
        st.error(
            "Could not build the score card image. Download is unavailable — "
            "try Share score card again."
        )
        raw = str(getattr(bundle, "x_message", "") or "")
        if raw.startswith(PNG_BUILD_FAILED_PREFIX) and "{" not in raw:
            st.caption(raw)
        return
    _show_share_png(bundle.png_bytes, bundle.filename)
    st.caption(f"Signature fingerprint: `{bundle.fingerprint}`")
    status = _user_facing_share_status(bundle)
    if bundle.x_posted:
        st.success(status)
    elif status:
        st.info(status)


def _render_why_this_score(report: dict, *, slot_index: int = 0) -> None:
    """Collapsed 'Why this score?' control. xAI runs only after the user asks.

    Streamlit still executes expander bodies when collapsed, so the explanation
    stays behind Show explanation. Missing ``XAI_API_KEY`` uses the templated
    fallback — this helper must never raise.
    """
    ticker = str(report.get("ticker") or "UNK")
    why_key = f"explain_{slot_index}_{ticker}"
    with open_expander(
        "Why this score?",
        expanded=False,
        key=f"why_exp_{slot_index}_{ticker}",
    ):
        if st.session_state.get(why_key):
            st.write(_cached_explanation(report))
            st.caption(AI_FOOTNOTE)
        elif st.button(
            "Show explanation",
            key=f"explain_btn_{slot_index}_{ticker}",
        ):
            st.session_state[why_key] = True
            st.write(_cached_explanation(report))
            st.caption(AI_FOOTNOTE)


def _user_facing_share_status(bundle: object) -> str:
    """Posted caption, or a polished skip — never raw X API errors."""
    posted = bool(getattr(bundle, "x_posted", False))
    message = str(getattr(bundle, "x_message", "") or "")
    if posted:
        return message
    return user_facing_x_skip_message(message)


def _ask_rat_messages() -> list[dict]:
    """Session chat history. Empty list when session_state is unavailable."""
    try:
        rows = st.session_state.get("ask_rat_messages")
        if not isinstance(rows, list):
            rows = []
            st.session_state["ask_rat_messages"] = rows
        return rows
    except Exception:  # noqa: BLE001 — pytest / no ScriptRunContext
        return []


def _render_ask_rat(
    scorer: TransparencyScorer,
    *,
    catalog: list[TickerOption] | None = None,
) -> None:
    """Collapsed Ask RAT text chat. Does not run the model on page load.

    Chips write the exact demo question into session and submit it. ``st.chat_input``
    is the free-form path. No TTS. Failures use the templated fallback.
    """
    messages = _ask_rat_messages()
    try:
        engaged = bool(messages) or bool(st.session_state.get("ask_rat_engaged"))
        pending = st.session_state.pop("ask_rat_prefill", None)
    except Exception:  # noqa: BLE001 — pytest / no ScriptRunContext
        engaged = bool(messages)
        pending = None
    if isinstance(pending, str):
        pending = pending.strip() or None
    else:
        pending = None

    with open_expander("Ask RAT", expanded=engaged, key="ask_rat_exp"):
        st.write(ASK_RAT_GREETING)
        st.caption(
            "Text chat grounded in scores, CMC, and PoR. No voice. "
            "Chips prefill a question; type your own below."
        )
        chip_cols = st.columns(len(ASK_RAT_CHIPS), gap="small")
        for index, chip in enumerate(ASK_RAT_CHIPS):
            with chip_cols[index]:
                if st.button(chip, key=f"ask_rat_chip_{index}", use_container_width=True):
                    st.session_state["ask_rat_prefill"] = chip
                    st.session_state["ask_rat_engaged"] = True
                    st.session_state["ask_rat_draft"] = chip
                    pending = chip
        draft = st.session_state.get("ask_rat_draft") or pending or ""
        if draft:
            st.caption(f"Question: {draft}")
        typed = st.chat_input("Ask about a tokenized stock's risk score")
        if typed:
            pending = typed.strip()
            st.session_state["ask_rat_draft"] = pending
            st.session_state["ask_rat_engaged"] = True
        if pending:
            messages.append({"role": "user", "content": pending})
            try:
                symbols = [opt.symbol for opt in (catalog or []) if getattr(opt, "symbol", "")]
                result = ask_rat(
                    pending,
                    scorer,
                    score_fn=lambda symbol: _score_one(scorer, symbol),
                    catalog_symbols=symbols,
                )
                cmc_lines = format_ask_cmc_lines(result.cmc_calls)
                body = result.answer
                messages.append(
                    {
                        "role": "assistant",
                        "content": body,
                        "cmc_lines": cmc_lines,
                        "footnote": result.footnote,
                        "skipped": result.skipped_reason,
                        "polished": result.polished,
                    }
                )
            except Exception as exc:  # noqa: BLE001 — chat must stay up
                messages.append(
                    {
                        "role": "assistant",
                        "content": (
                            f"Ask RAT hit an error ({exc}). "
                            "This is an automated summary, not financial advice."
                        ),
                        "cmc_lines": [],
                        "footnote": AI_FOOTNOTE,
                        "skipped": "internal error",
                        "polished": False,
                    }
                )
            st.session_state["ask_rat_prefill"] = ""
            _maybe_rerun()
        for msg in messages:
            role = "assistant" if msg.get("role") == "assistant" else "user"
            with st.chat_message(role):
                st.write(msg.get("content") or "")
                if msg.get("skipped"):
                    st.caption(str(msg["skipped"]))
                if msg.get("footnote"):
                    st.caption(str(msg["footnote"]))
                for line in msg.get("cmc_lines") or []:
                    st.caption(line)


def _render_sidebar_controls(default_fixtures: bool) -> bool:
    """Dense sidebar: Live/fixture + labeling stay visible; help collapses.

    Exact Disclaimer text is preserved inside the expander. Privacy / Terms
    links stay on the sidebar (and footer) — layout is compressed, not meaning.
    """
    with st.sidebar:
        use_fixtures = st.toggle(
            "Use demo fixtures",
            value=default_fixtures,
            help="Bypass the live CMC API. Required if you do not have CMC_API_KEY.",
        )
        if use_fixtures:
            st.caption("Fixture mode — bundled **demo data**, not live CMC.")
        elif not os.getenv("CMC_API_KEY"):
            st.error(
                "CMC_API_KEY is not set. Switch fixtures on, or add the key in the host env."
            )
        else:
            st.caption(
                "Live mode — CMC_API_KEY set. Issuer directory cached; Basic 429s retried."
            )

        with st.expander("How scores are labeled", expanded=False):
            st.markdown(sidebar_legend_markdown())

        with st.expander("Pillar weights", expanded=False):
            st.markdown(sidebar_weights_markdown())

        with st.expander("Disclaimer", expanded=False):
            st.write(DISCLAIMER)

        st.markdown("[Privacy Policy](/privacy) · [Terms of Service](/terms)")
        st.caption("These do not replace the Disclaimer.")
    return use_fixtures


def _render_card_details(report: dict, *, slot_index: int = 0) -> None:
    """Collapsed pillar evidence on the compare card. xAI / share stay gated elsewhere."""
    ticker = str(report.get("ticker") or "UNK")
    with open_expander(
        "Pillar evidence",
        expanded=False,
        key=f"pillar_ev_{slot_index}_{ticker}",
    ):
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
            basis_src = basis_meta.get("source") or "cmc_market_pairs"
            st.caption(
                f"Cross-issuer basis: **{spread:.2f}%** spread across {count} wrappers "
                f"(self-reported {basis_src})."
            )
        elif basis_meta.get("wrapper_count") == 1:
            st.caption(
                "Cross-issuer basis: only one wrapper on CMC RWA quotes/market-pairs "
                "— no issuer compare."
            )

        flags = report.get("flags") or []
        if flags:
            for flag in flags:
                st.warning(flag)
        else:
            st.caption("No risk flags on this pass.")

        for row in pillar_evidence_rows(report):
            st.markdown(
                f"**{row['label']}** — {row['score']:.0f}/100 "
                f"(weight {row['weight']:.0%})"
            )
            st.caption(row["badge"])
            st.caption(row["what"])
            st.caption(f"Evidence: {row['evidence']}")
            if row["explanation"]:
                st.write(row["explanation"])

        notes = report.get("notes") or []
        if notes:
            st.markdown("**Notes**")
            for note in notes:
                st.info(note)
        st.caption("Educational demo — existing verification evidence, not a new metric.")


def _render_compare_card(
    report: dict,
    *,
    selected: bool = False,
    company: str = "",
    slot_index: int = 0,
    diff: dict | None = None,
) -> None:
    """Ticker · band chip · issuer · score · LIVE/FIXTURE · pillar evidence / Why this score?"""
    ticker = str(report.get("ticker") or "")
    title = f"● {ticker}" if selected else ticker
    score = float(report.get("score") or 0)
    band = normalize_band(report.get("band"), score=score) or str(
        report.get("band") or band_code(score)
    )
    issuer = short_company_name(str(report.get("issuer") or "")) or company
    cue = mode_cue(report)
    st.metric(
        title,
        f"{score:.1f}",
        f"{issuer} · {cue}",
        delta_color="off",
    )
    chip = band_chip_html(band, score=score, selected=selected)
    if chip:
        st.markdown(chip, unsafe_allow_html=True)
    dots = pillar_dots(report)
    weak = weakest_pillar_line(report)
    if dots and weak:
        st.caption(f"{dots}  {weak}")
    elif weak:
        st.caption(weak)
    elif dots:
        st.caption(dots)
    card = ((diff or {}).get("cards") or {}).get(ticker) or {}
    if (diff or {}).get("verification_differs") and card.get("verification"):
        st.caption(f"Verify: {card['verification']}")
    contrast = card_contrast_line(ticker, diff)
    if contrast:
        st.caption(contrast)
    _render_card_details(report, slot_index=slot_index)
    _render_why_this_score(report, slot_index=slot_index)


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


def _render_compare_callout(diff: dict) -> None:
    """Short 'why these differ' strip — only when 2+ slots scored."""
    if not diff.get("available") or not diff.get("headline"):
        return
    st.markdown("#### Why these differ")
    st.info(diff["headline"])
    for line in diff.get("lines") or []:
        st.caption(line)
    st.caption("Educational compare — heuristic scores, not financial advice.")


def _render_selected_slot_detail(
    report: dict, *, slot_index: int = 0, diff: dict | None = None
) -> None:
    """Active-slot pillars under the row — no empty right gutter."""
    rank = selected_slot_contrast_line(str(report.get("ticker") or ""), diff)
    if rank:
        st.caption(rank)
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
    ticker = str(report.get("ticker") or "UNK")
    state_key, _pending = share_session_keys(slot_index, ticker)
    has_share = False
    try:
        has_share = st.session_state.get(state_key) is not None
    except Exception:  # noqa: BLE001 — pytest / no ScriptRunContext
        has_share = False
    with open_expander(
        "Share score card",
        expanded=has_share,
        key=f"share_exp_{slot_index}_{ticker}",
    ):
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
      /* Typeahead bridge is a 1px iframe — keep JS alive, no layout gap.
         Search-only; Share preview is markdown (no iframe) so a Share click
         cannot mount /component and trip MPA Page not found. */
      div[data-testid="stIFrame"]:has(iframe[height="1"]),
      iframe[height="1"] {{
        height: 1px !important;
        min-height: 1px !important;
        margin: 0 !important;
        padding: 0 !important;
        overflow: hidden !important;
        border: 0 !important;
        position: absolute !important;
        width: 1px !important;
        opacity: 0 !important;
        pointer-events: none !important;
      }}
      /* Share PNG preview (markdown data URI — not st.image / components.html). */
      .rat-share-card img {{
        width: 100%;
        height: auto;
        border-radius: 8px;
        display: block;
      }}
      .rat-share-card p {{
        margin: 0.65rem 0 0;
      }}
      /* CMC RWA classes — horizontal wrapping pills, not tall column blocks. */
      .rat-cat-row {{
        display: flex;
        flex-direction: row;
        flex-wrap: wrap;
        align-items: center;
        gap: 0.4rem;
        margin: 0 0 0.55rem 0;
        max-width: 100%;
      }}
      .rat-cat-pill {{
        display: inline-flex;
        align-items: center;
        padding: 0.22rem 0.8rem;
        border-radius: 999px;
        border: 1px solid rgba(250, 250, 250, 0.22);
        background: rgba(250, 250, 250, 0.06);
        color: inherit;
        text-decoration: none;
        font-size: 0.82rem;
        font-weight: 650;
        line-height: 1.25;
        white-space: nowrap;
      }}
      .rat-cat-pill:hover {{
        border-color: rgba(250, 250, 250, 0.45);
        background: rgba(250, 250, 250, 0.12);
      }}
      /* Score-band pill — stronger than metric-delta text. */
      .rat-band-chip {{
        display: inline-block;
        padding: 0.22rem 0.7rem;
        border-radius: 999px;
        font-weight: 800;
        font-size: 0.78rem;
        letter-spacing: 0.08em;
        line-height: 1.25;
        text-transform: uppercase;
        vertical-align: middle;
      }}
      .rat-band-chip-selected {{
        box-shadow: 0 0 0 2px rgba(250, 250, 250, 0.95), 0 0 0 4px currentColor;
      }}
      /* Sidebar density: drop empty vertical gap; keep expanders scannable. */
      section[data-testid="stSidebar"] > div:first-child,
      section[data-testid="stSidebar"] [data-testid="stSidebarContent"],
      section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {{
        padding-top: 0.55rem !important;
        padding-bottom: 0.75rem !important;
      }}
      section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{
        gap: 0.35rem !important;
      }}
      section[data-testid="stSidebar"] [data-testid="stExpander"] details {{
        border: 1px solid rgba(250, 250, 250, 0.14);
        border-radius: 0.4rem;
      }}
      section[data-testid="stSidebar"] [data-testid="stExpander"] summary {{
        padding: 0.28rem 0.55rem !important;
      }}
      section[data-testid="stSidebar"] [data-testid="stExpander"] [data-testid="stExpanderDetails"],
      section[data-testid="stSidebar"] [data-testid="stExpander"] .streamlit-expanderContent {{
        padding: 0.35rem 0.55rem 0.5rem !important;
      }}
      section[data-testid="stSidebar"] .stAlert {{
        padding: 0.4rem 0.65rem !important;
      }}
      section[data-testid="stSidebarNav"],
      [data-testid="stSidebarNav"] {{
        display: none !important;
      }}
    </style>
    """,
    unsafe_allow_html=True,
)

st.header(BRAND_H1)
st.caption(BRAND_SUB)
st.caption(TAGLINE)

default_fixtures = env_flag("RWA_USE_FIXTURES") or not os.getenv("CMC_API_KEY")
use_fixtures = _render_sidebar_controls(default_fixtures)

mode_label = "Fixture" if use_fixtures else "Live"
st.caption(f"Mode: {mode_label}")
reminder = health_launcher_reminder()
if reminder:
    st.caption(reminder)
st.caption(
    "Remaining heuristics stay labeled: name-list fallback, self-reported CMC "
    "price/disclosure/basis (RWA quotes + market-pairs), and xStocks without "
    "a published PoR proxy. Educational demo — not financial advice."
)

try:
    scorer = _cached_scorer(use_fixtures)
    begin = getattr(scorer.client, "begin_run", None)
    if callable(begin):
        begin()
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
ok_reports = [
    report for _symbol, report, error in results if report is not None and not error
]
diff = compare_differentiation(ok_reports)

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
                    diff=diff,
                )
    _render_compare_callout(diff)
    if 0 <= target < len(results):
        _sel_symbol, sel_report, sel_error = results[target]
        if sel_report is not None and not sel_error:
            _render_selected_slot_detail(
                sel_report, slot_index=target, diff=diff
            )

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

_render_ask_rat(scorer, catalog=catalog)

st.markdown("#### CMC calls this run")
for line in format_cmc_calls_lines(collect_cmc_calls(ok_reports, scorer.client)):
    st.caption(line)

st.divider()
st.caption(DISCLAIMER)
st.markdown("[Privacy Policy](/privacy) · [Terms of Service](/terms)")
