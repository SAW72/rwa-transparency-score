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
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st
import streamlit.components.v1 as components

from rwa_score.ask_rat import (
    ASK_RAT_CHIPS,
    ASK_RAT_GREETING,
    ask as ask_rat,
    format_ask_cmc_lines,
)
from rwa_score.client import create_client, env_flag, summarize_call_log
from rwa_score.explainer import AI_FOOTNOTE, explain_score, fallback_explanation
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
    PLAN_BLOCKED_LABEL,
    WEIGHTS,
    ScoreError,
    TransparencyScorer,
    band_code,
    basis_is_plan_blocked,
    remaining_heuristic_paths,
)
from rwa_score.ticker_search import (
    CATEGORY_BY_ID,
    CLASS_PAGE_LIMIT,
    RWA_CLASS_CATEGORIES,
    RWA_CLASS_IDS,
    SEARCH_MIN_CHARS,
    TickerOption,
    cached_class_catalog,
    catalog_from_por_feeds,
    class_browse_truncated,
    class_shard_failure_is_fresh,
    class_shard_status,
    client_shard_token,
    empty_class_page_is_cached_success,
    classes_for_query,
    format_option,
    is_class_browse_query,
    live_class_unavailable,
    load_class_catalog,
    merge_por_catalog,
    merge_search_catalog,
    normalize_ticker,
    resolve_assign_symbol,
    search_tickers,
    symbol_lookup_failed,
)
from rwa_score.verifiers import VerificationLevel
from rwa_score.source_label import source_kind
from rwa_score.health import SHARE_UX
from rwa_score.x_client import (
    MISSING_CREDS_MESSAGE,
    user_facing_x_skip_message,
    x_credentials_ready,
)

EXPLAIN_CACHE_TTL_SECONDS = 24 * 3600.0
CATALOG_CACHE_TTL_SECONDS = 1800.0
SHARE_EXPANDER_LABEL = "Scorecard"
SCORECARD_BUTTON_LABEL = "Scorecard"
DOWNLOAD_PNG_LABEL = "download PNG"
SHARE_BUTTON_LABEL = "Share"
LIVE_SCORE_PAGE_URL = "https://rwa-transparency-score.onrender.com"
X_POST_DISABLED_MESSAGE = "X post skipped (disabled)."
# Per-symbol wall-clock cache for the explainer — process-local dict.
_explain_cache: dict[str, tuple[float, str]] = {}
# Session fallback when Streamlit context is missing (unit tests).
_catalog_shard_memo: dict[str, list[TickerOption]] = {}

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
    if pillar_key == "basis" and basis_is_plan_blocked(
        report.get("basis") or {}, verification=report.get("verification") or {}
    ):
        return PLAN_BLOCKED_LABEL, evidence
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
# Prefix typeahead only. Category / class browse uses CLASS_PAGE_LIMIT so
# Matches can scroll the loaded CMC page instead of four top ranks.
CANDIDATE_STRIP_LIMIT = 4
LIVE_UNAVAILABLE_BANNER = "Live data unavailable"
CLASS_REMAINDER_CAPTION = (
    f"Showing first {CLASS_PAGE_LIMIT} live results — type a ticker for the rest."
)
SEARCH_MATCH_KEY = "search_match_pick"
# Class browse list. Separate from the prefix selectbox so a pill tap does
# not mount (or remount) a BaseWeb menu. Unselected stays None — do not
# delete this key just because nothing is picked.
SEARCH_LIST_KEY = "search_match_list"
# Hidden commit channel for the class-browse ticker overlay. The visible
# control is custom HTML (one scrollable list). This input is not a second
# Search box and is not an st.radio. A full-class st.radio inside
# st.container(height=...) Aw Snaps Chrome — do not bring that back.
CLASS_PICK_KEY = "class_ticker_pick"
CLASS_PICK_LABEL = "Class match"
CLASS_CHOSEN_KEY = "class_ticker_chosen"
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

  function menuOpen() {
    return !!(
      doc.querySelector('[data-baseweb="popover"]') ||
      doc.querySelector('[data-baseweb="menu"]') ||
      doc.querySelector('[data-testid="stSelectbox"] [aria-expanded="true"]')
    );
  }

  function restoreFocus(input) {
    if (!input || !keepFocus() || menuOpen()) return;
    var active = doc.activeElement;
    if (active && active !== input && active.closest &&
        (active.closest('[data-testid="stSelectbox"]') ||
         active.closest('[data-baseweb="popover"]'))) {
      return;
    }
    input.focus();
    try {
      var len = (input.value || "").length;
      input.setSelectionRange(len, len);
    } catch (err) {}
  }

  function removeTickerDropdown() {
    var panel = doc.getElementById("rwa-ticker-dd-panel");
    if (panel && panel.parentNode) panel.parentNode.removeChild(panel);
  }

  function classBrowse() {
    // Category Matches is the compact ticker dropdown in this container.
    // The pill rerun must not focus the search box — that rebuild Aw Snaps
    // Chrome (error 9) before the list or the compare slot paints.
    // Do not wait for a radio node: a live 401 never mounts one.
    return !!doc.querySelector(".st-key-rwa_class_browse");
  }

  function attach() {
    // Open select menu and category-pill browse both rebuild a lot of DOM.
    // Focusing or rebinding during that rebuild Aw Snaps Chrome (error 5/9).
    // Prefix search also drops a leftover class-browse overlay.
    if (!classBrowse()) removeTickerDropdown();
    if (menuOpen() || classBrowse()) return;
    var input = findSearchInput();
    if (!input) return;
    bind(input);
    // Observer bursts must not steal focus. Restore only while the user is
    // actually typing a prefix (keepFocus). A pill click clears that flag.
    if (keepFocus()) restoreFocus(input);
  }

  function attachSoon() {
    if (menuOpen() || classBrowse()) return;
    if (win.__rwaAttachTimer) win.clearTimeout(win.__rwaAttachTimer);
    win.__rwaAttachTimer = win.setTimeout(attach, 80);
  }

  if (!win.__rwaTypeaheadInstalled) {
    win.__rwaTypeaheadInstalled = true;
    doc.addEventListener("mousedown", function (ev) {
      var t = ev.target;
      if (t && t.closest && t.closest('[data-testid="stTextInput"]')) return;
      setKeepFocus(false);
    }, true);
    attach();
    new win.MutationObserver(attachSoon).observe(doc.body, { childList: true, subtree: true });
  } else {
    attach();
  }
})();
"""
# Class-browse ticker overlay. Runs inside the same 1px components.html
# iframe as typeahead, but never both in one run. No MutationObserver and
# no st.radio — open, scroll, and close stay in the parent document. A
# click commits one hidden text input so Streamlit places the ticker.
TICKER_DROPDOWN_JS = r"""
(function () {
  var win = window.parent;
  var doc = win.document;
  var PAYLOAD = __PAYLOAD_JSON__;

  function removePanel() {
    var panel = doc.getElementById("rwa-ticker-dd-panel");
    if (panel && panel.parentNode) panel.parentNode.removeChild(panel);
  }

  function findClassPickInput() {
    var blocks = doc.querySelectorAll('[data-testid="stTextInput"]');
    var i, label, text;
    var want = String(PAYLOAD.pickLabel || "Class match");
    for (i = 0; i < blocks.length; i++) {
      label = blocks[i].querySelector("label");
      text = label ? String(label.textContent || "").trim() : "";
      if (text === want) return blocks[i].querySelector("input");
    }
    return doc.querySelector(".st-key-class_ticker_pick input");
  }

  function fireEnter(input) {
    // Streamlit 1.39 text input commits on keypress Enter, and only when
    // React state is already dirty. keydown does not commit this widget.
    // Events must be constructed in the parent window — an iframe event
    // dispatched onto the app input is ignored.
    var EventCtor = win.KeyboardEvent || KeyboardEvent;
    try {
      input.dispatchEvent(new EventCtor("keypress", {
        key: "Enter", code: "Enter", keyCode: 13, which: 13,
        bubbles: true, cancelable: true
      }));
    } catch (err) {}
  }

  function commitPick(symbol) {
    var input = findClassPickInput();
    if (!input || !symbol) return;
    var proto = Object.getOwnPropertyDescriptor(win.HTMLInputElement.prototype, "value");
    if (proto && proto.set) proto.set.call(input, symbol);
    else input.value = symbol;
    var InputCtor = win.InputEvent || win.Event;
    try {
      input.dispatchEvent(new InputCtor("input", {
        bubbles: true, cancelable: true, data: symbol, inputType: "insertText"
      }));
    } catch (err) {
      try { input.dispatchEvent(new win.Event("input", { bubbles: true })); } catch (err2) {}
    }
    // React applies the input event asynchronously. One keypress on the
    // next turn commits the new value. Do not also blur — a second commit
    // would place the same ticker into the next slot.
    win.setTimeout(function () {
      var again = findClassPickInput();
      if (!again) return;
      fireEnter(again);
    }, 50);
  }

  function contentBounds() {
    var main = doc.querySelector('[data-testid="stMainBlockContainer"]')
      || doc.querySelector(".block-container")
      || doc.querySelector('[data-testid="stMain"]');
    if (!main) return { left: 16, width: Math.max(320, win.innerWidth - 32) };
    var rect = main.getBoundingClientRect();
    return { left: rect.left, width: rect.width };
  }

  function positionPanel() {
    var panel = doc.getElementById("rwa-ticker-dd-panel");
    var label = doc.getElementById("rwa-ticker-dd-label");
    if (!panel || !label || panel.hasAttribute("hidden")) return;
    var bounds = contentBounds();
    var labelRect = label.getBoundingClientRect();
    var top = Math.round(labelRect.bottom + 4);
    var maxH = Math.max(180, Math.min(win.innerHeight - top - 12, Math.round(win.innerHeight * 0.72)));
    panel.style.left = Math.round(bounds.left) + "px";
    panel.style.width = Math.round(Math.max(280, bounds.width)) + "px";
    panel.style.top = top + "px";
    panel.style.maxHeight = Math.round(maxH) + "px";
    var note = panel.querySelector(".rwa-ticker-dd-note");
    var scroll = panel.querySelector(".rwa-ticker-dd-scroll");
    if (scroll) {
      var noteH = note ? note.offsetHeight : 0;
      scroll.style.maxHeight = Math.max(120, Math.round(maxH - noteH)) + "px";
    }
  }

  function setOpen(open) {
    var panel = doc.getElementById("rwa-ticker-dd-panel");
    var label = doc.getElementById("rwa-ticker-dd-label");
    if (!panel || !label) return;
    if (open) panel.removeAttribute("hidden");
    else panel.setAttribute("hidden", "");
    label.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) positionPanel();
  }

  function mount(host) {
    var previous = doc.getElementById("rwa-ticker-dd-anchor");
    if (previous && previous.parentNode) previous.parentNode.removeChild(previous);
    removePanel();

    var anchor = doc.createElement("div");
    anchor.id = "rwa-ticker-dd-anchor";
    anchor.setAttribute("data-rwa-ticker-dropdown", "1");
    host.insertBefore(anchor, host.firstChild);

    var label = doc.createElement("button");
    label.type = "button";
    label.id = "rwa-ticker-dd-label";
    label.className = "rwa-ticker-dd-label";
    label.setAttribute("aria-haspopup", "listbox");
    label.setAttribute("aria-expanded", "false");

    var text = doc.createElement("span");
    text.className = "rwa-ticker-dd-text";
    text.textContent = PAYLOAD.label || "Choose a ticker";
    var arrow = doc.createElement("span");
    arrow.className = "rwa-ticker-dd-arrow";
    arrow.setAttribute("aria-hidden", "true");
    arrow.textContent = "\u25BE";
    label.appendChild(text);
    label.appendChild(arrow);
    anchor.appendChild(label);

    var panel = doc.createElement("div");
    panel.id = "rwa-ticker-dd-panel";
    panel.className = "rwa-ticker-dd-panel";
    panel.setAttribute("hidden", "");

    var scroll = doc.createElement("div");
    scroll.className = "rwa-ticker-dd-scroll";
    scroll.setAttribute("role", "listbox");
    scroll.setAttribute("aria-label", "Tickers");

    var rows = PAYLOAD.rows || [];
    var i, row, opt;
    for (i = 0; i < rows.length; i++) {
      row = rows[i];
      opt = doc.createElement("button");
      opt.type = "button";
      opt.className = "rwa-ticker-dd-option";
      opt.setAttribute("role", "option");
      opt.setAttribute("data-symbol", String(row.symbol || ""));
      opt.textContent = row.label || row.symbol || "";
      (function (item) {
        opt.addEventListener("click", function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          text.textContent = item.label || item.symbol;
          setOpen(false);
          commitPick(item.symbol);
        });
      })(row);
      scroll.appendChild(opt);
    }
    panel.appendChild(scroll);

    if (PAYLOAD.remainder) {
      var note = doc.createElement("div");
      note.className = "rwa-ticker-dd-note";
      note.textContent = PAYLOAD.remainder;
      panel.appendChild(note);
    }
    doc.body.appendChild(panel);

    label.addEventListener("click", function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      setOpen(label.getAttribute("aria-expanded") !== "true");
    });

    win.__rwaTickerDdPosition = positionPanel;
    if (!win.__rwaTickerDdListeners) {
      win.__rwaTickerDdListeners = true;
      win.addEventListener("resize", function () {
        if (win.__rwaTickerDdPosition) win.__rwaTickerDdPosition();
      });
      doc.addEventListener("scroll", function () {
        if (win.__rwaTickerDdPosition) win.__rwaTickerDdPosition();
      }, true);
      doc.addEventListener("mousedown", function (ev) {
        var target = ev.target;
        var panelEl = doc.getElementById("rwa-ticker-dd-panel");
        var labelEl = doc.getElementById("rwa-ticker-dd-label");
        if (!panelEl || panelEl.hasAttribute("hidden")) return;
        if (target && target.nodeType !== 1) target = target.parentElement;
        if (target && target.closest && (
          target.closest("#rwa-ticker-dd-panel") ||
          target.closest("#rwa-ticker-dd-label") ||
          target.closest("#rwa-ticker-dd-anchor")
        )) return;
        if (labelEl) labelEl.setAttribute("aria-expanded", "false");
        panelEl.setAttribute("hidden", "");
      }, true);
      doc.addEventListener("keydown", function (ev) {
        if (ev.key !== "Escape") return;
        var panelEl = doc.getElementById("rwa-ticker-dd-panel");
        var labelEl = doc.getElementById("rwa-ticker-dd-label");
        if (!panelEl || panelEl.hasAttribute("hidden")) return;
        if (labelEl) labelEl.setAttribute("aria-expanded", "false");
        panelEl.setAttribute("hidden", "");
      }, true);
    }
  }

  function mountWhenReady(tries) {
    var rows = PAYLOAD.rows || [];
    if (!rows.length) {
      removePanel();
      return;
    }
    var host = doc.querySelector(".st-key-rwa_class_browse");
    if (!host) {
      if (tries > 0) win.setTimeout(function () { mountWhenReady(tries - 1); }, 40);
      return;
    }
    mount(host);
  }

  mountWhenReady(25);
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


class _LiveDirectoryUnavailable(Exception):
    """Raised so Streamlit does not cache an empty failed class page."""


@st.cache_data(ttl=CATALOG_CACHE_TTL_SECONDS, show_spinner=False)
def _cached_class_catalog_data(
    use_fixtures: bool, asset_type: str
) -> tuple[TickerOption, ...]:
    """One CMC class page per process. Widget clicks must not re-walk.

    Live directory failures are not cached — an empty tuple would look like
    a finished class and would hide the unavailable banner until TTL.
    """
    scorer = _cached_scorer(use_fixtures)
    rows = load_class_catalog(scorer.client, asset_type, first_page_only=True)
    if class_shard_status(scorer.client, asset_type).unavailable:
        raise _LiveDirectoryUnavailable()
    return tuple(rows)


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
    source = source_kind(block.get("source"))
    if source == "fixture":
        header = "CMC calls this run — Fixture (bundled demo, **not** live CMC)"
    elif source == "live":
        header = "CMC calls this run — Live CMC (not fixtures)"
    else:
        header = "CMC calls this run — source not confirmed (not labeled live CMC)"
    lines = [header]
    endpoints = block.get("endpoints") or []
    if not endpoints:
        lines.append("No CMC/fixture directory calls recorded on this run yet.")
        return lines
    for row in endpoints:
        endpoint = row.get("endpoint") or ""
        raw_source = row.get("source")
        row_kind = source_kind(raw_source) if raw_source else source
        via = row.get("via") or ("fixture" if row_kind == "fixture" else "network")
        if row_kind == "fixture" or source == "fixture":
            tag = "fixture"
        elif row_kind == "live" or source == "live":
            tag = "cache" if row.get("cached") or via == "cache" else "live"
        else:
            tag = "unconfirmed"
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
    used_fallback = text == fallback_explanation(report)
    # Do not cache a failed xAI turn for 24h — retry when the key is present.
    if symbol and (not used_fallback or not os.environ.get("XAI_API_KEY")):
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


def clear_compare_slots() -> None:
    """Drop every compare ticker. Live directory failure must not keep the default shortlist."""
    st.session_state.slots = [""] * MAX_COMPARE_SLOTS
    st.session_state.active_slot = 0


def _ensure_slot_state() -> None:
    """Init compare slots on a true first load only.

    Category chips must not remount the page. A widget/JS rerun keeps
    ``st.session_state``, so the default NVDA/TSLA/AAPL/META row is applied
    only when slots have never been set. A live outage clears that row;
    empty slots are not refilled with the defaults.
    """
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
    """RWA bar is exactly the six CMC ``asset_type`` classes.

    Legacy Look sectors (AI/Tech, Oil/Energy, Auto/EV, Finance) and
    Crypto / Digital Assets stay off this bar. Industry keywords remain
    typeable in Search; BTC/ETH never classify as an RWA class.
    ``catalog`` is unused — the six official pills always show.
    """
    _ = catalog
    return tuple(RWA_CLASS_CATEGORIES)


def chip_query(category) -> str:
    """Search text a chip should type.

    Official CMC RWA classes use the ``asset_type`` id so Treasuries loads
    ``government_security`` (not a display alias). Industry chips still use
    the first documented keyword.
    """
    cid = str(getattr(category, "id", "") or "")
    if cid in RWA_CLASS_IDS:
        return cid
    words = getattr(category, "keywords", ()) or ()
    return str(words[0] if words else getattr(category, "label", "") or "")


def is_browse_chip_query(query: str) -> bool:
    """True when Search is exactly a category-button keyword (not typed prefix)."""
    text = (query or "").strip()
    if not text:
        return False
    return any(text == chip_query(cat) for cat in RWA_CLASS_CATEGORIES)


def matches_limit_for_query(query: str) -> int:
    """Class browse scrolls the loaded page. Prefix typeahead stays short."""
    if is_class_browse_query(query):
        return CLASS_PAGE_LIMIT
    return CANDIDATE_STRIP_LIMIT


def live_unavailable_banner(use_fixtures: bool, client, query: str) -> bool:
    """True when Live chrome must show the banner and no directory rows.

    Fixture mode never banners. A healthy live page never banners. 401 / 429 /
    1008 (and any other live directory failure) does — including a symbol
    lookup that failed after the class page itself succeeded.
    """
    if use_fixtures or client is None:
        return False
    if not (query or "").strip():
        return False
    if live_class_unavailable(client, query):
        return True
    return symbol_lookup_failed(client, query)


def search_matches(
    query: str,
    catalog: list[TickerOption],
    client=None,
    *,
    use_fixtures: bool = False,
) -> list[TickerOption]:
    """Matches for the picker. Same live map family for pills and typeahead.

    Category browse is not capped at ``CANDIDATE_STRIP_LIMIT``. A live
    directory failure returns no rows — fixture stubs are not substituted
    while the chrome still says Live.
    """
    if not use_fixtures and live_class_unavailable(client, query):
        return []
    hits = search_tickers(
        query,
        catalog,
        limit=matches_limit_for_query(query),
        client=client,
    )
    if not use_fixtures and symbol_lookup_failed(client, query):
        return []
    return hits


def stale_match_pick(stored: object, options: list[str]) -> bool:
    """True when a prior Matches pick is no longer in the strip.

    ``None`` / empty is an unselected compact selectbox. Treating that as
    stale deletes the widget key and remounts Matches mid-click (Aw Snap).
    """
    if stored is None:
        return False
    symbol = normalize_ticker(str(stored))
    if not symbol:
        return False
    return symbol not in set(options)


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
    """LIVE vs fixture badge for a compare card. Unknown is never LIVE."""
    kind = source_kind(report.get("data_source"))
    if kind == "fixture":
        return "FIXTURE"
    if kind == "live":
        return "LIVE"
    return "UNCONFIRMED"


def data_source_caption(report: dict) -> str:
    """Card-level CMC honesty. Does not call heuristic pillars LIVE CMC."""
    kind = source_kind(report.get("data_source"))
    if kind == "fixture":
        return "Demo fixture data — not a live CoinMarketCap API response."
    if kind == "live":
        return (
            "Live CoinMarketCap directory/quotes. "
            "Price, disclosure, and basis are self-reported CMC fields. "
            "Backing / reserves / redemption are live only when a verifier "
            "hits a published source — otherwise heuristic fallback."
        )
    return "Data source not confirmed — not labeled as live CoinMarketCap."


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


def basis_status_caption(basis_meta: dict | None) -> str | None:
    """Honest Cross-issuer basis caption. Plan-block is never shown as live pairs."""
    meta = basis_meta if isinstance(basis_meta, dict) else {}
    if meta.get("plan_blocked"):
        return (
            f"Cross-issuer basis: **{PLAN_BLOCKED_LABEL}** "
            "(CMC market-pairs not on this plan — not live market-pairs data)."
        )
    if meta.get("available"):
        spread = meta.get("percent_spread")
        count = meta.get("wrapper_count")
        basis_src = meta.get("source") or "cmc_market_pairs"
        try:
            spread_txt = f"{float(spread):.2f}%"
        except (TypeError, ValueError):
            spread_txt = "n/a"
        return (
            f"Cross-issuer basis: **{spread_txt}** spread across {count} wrappers "
            f"(self-reported {basis_src})."
        )
    if meta.get("wrapper_count") == 1:
        return (
            "Cross-issuer basis: only one wrapper on CMC RWA quotes/market-pairs "
            "— no issuer compare."
        )
    return None


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


def score_page_url() -> str:
    """Public HTTPS URL for this score page. Prefer Render/env, else known live host."""
    for key in ("RENDER_EXTERNAL_URL", "APP_PUBLIC_URL"):
        raw = (os.environ.get(key) or "").strip().rstrip("/")
        if raw.startswith("https://"):
            return raw
    return LIVE_SCORE_PAGE_URL


def is_https_url(url: str) -> bool:
    raw = (url or "").strip()
    if not raw.startswith("https://"):
        return False
    return not any(ch.isspace() or ch in "<>" for ch in raw)


def is_x_status_url(url: str) -> bool:
    """True only for a real https x.com/twitter.com status URL. Never invent one."""
    if not is_https_url(url):
        return False
    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower()
    if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return False
    parts = [p for p in (parsed.path or "").split("/") if p]
    return len(parts) >= 2 and parts[-2] == "status" and bool(parts[-1])


def markdown_https_link(url: str, label: str) -> str:
    """Clickable markdown link. Empty when the URL is not https."""
    cleaned = (url or "").strip()
    if not is_https_url(cleaned):
        return ""
    safe_label = (label or "Open link").replace("[", "").replace("]", "")
    return f"[{safe_label}]({cleaned})"


def share_footer_markdown(bundle: object) -> str:
    """Bottom of share view: labeled links, never a raw HTTPS dump."""
    lines: list[str] = []
    page_link = markdown_https_link(score_page_url(), "Open score page")
    if page_link:
        lines.append(page_link)
    if getattr(bundle, "x_posted", False):
        x_url = str(getattr(bundle, "x_url", "") or "").strip()
        if is_x_status_url(x_url):
            tweet_link = markdown_https_link(x_url, "View post on X")
            if tweet_link:
                lines.append(tweet_link)
    return "\n\n".join(lines)


def share_card_preview_html(png_bytes: bytes, filename: str) -> str:
    """Self-contained PNG preview + download. No Streamlit media/component URLs."""
    b64 = base64.b64encode(png_bytes).decode("ascii")
    safe_name = html.escape(filename or "rat-score.png", quote=True)
    return (
        '<div class="rat-share-card">'
        f'<img alt="RAT Score card" src="data:image/png;base64,{b64}" '
        'style="width:100%;height:auto;border-radius:8px;display:block;" />'
        '<p style="margin:0.65rem 0 0;">'
        f'<a download="{safe_name}" href="data:image/png;base64,{b64}">{DOWNLOAD_PNG_LABEL}</a>'
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
    _ensure_slot_state()
    updated, nxt = place_search_match(
        list(st.session_state.slots), int(st.session_state.active_slot), symbol
    )
    st.session_state.slots = updated
    st.session_state.active_slot = nxt
    # Drop Search + match dropdown on the next run (cannot mutate the
    # ticker_query widget after it already exists on this run).
    st.session_state["_clear_search"] = True
    _maybe_rerun()


def _clear_match_pick_state() -> None:
    """Drop Matches widgets. Safe only before they are instantiated."""
    st.session_state.pop(SEARCH_MATCH_KEY, None)
    st.session_state.pop(SEARCH_LIST_KEY, None)
    st.session_state.pop(CLASS_PICK_KEY, None)
    st.session_state.pop(CLASS_CHOSEN_KEY, None)


def _on_search_query_change() -> None:
    """Drop a stale Matches pick when Search text changes (type-ahead or Enter)."""
    _clear_match_pick_state()


def _on_class_pill(query: str) -> None:
    """Select a class before the script body builds the catalog.

    Streamlit runs this callback before the script. The pill ``if`` body runs
    only after ``_ticker_catalog(pending_search_query())``, so without this
    the click frame still has the previous query: a live 401 never marks the
    class unavailable and Matches shows "No directory matches" instead of the
    banner.
    """
    st.session_state.pop("_clear_search", None)
    st.session_state.ticker_query = query
    _clear_match_pick_state()


def catalog_for_active_query(scorer, catalog: list[TickerOption], query: str):
    """Catalog for the query the picker is about to render.

    The module-level build uses ``pending_search_query()`` at the start of
    the run. A class pill can change Search in that same run; reload so a
    live directory failure is recorded before the banner check.
    """
    if scorer is None or not (query or "").strip():
        return catalog
    return _ticker_catalog(scorer, query=query)


def search_typeahead_script(debounce_ms: int = SEARCH_TYPEAHEAD_DEBOUNCE_MS) -> str:
    """JS that commits the Search box on input so Matches update without Enter."""
    delay = max(0, int(debounce_ms))
    return SEARCH_TYPEAHEAD_JS.replace("__DEBOUNCE_MS__", str(delay))


def ticker_dropdown_items(matches: list[TickerOption]) -> list[dict[str, str]]:
    """Every loaded class row, in order. The overlay scrolls this whole shard."""
    return [
        {"symbol": opt.symbol, "label": format_option(opt)}
        for opt in matches
    ]


def class_dropdown_label(matches: list[TickerOption], chosen: str) -> str:
    """Closed ticker control. Unselected stays the placeholder."""
    symbol = normalize_ticker(str(chosen or ""))
    if not symbol:
        return "Choose a ticker"
    for opt in matches:
        if opt.symbol == symbol:
            return format_option(opt)
    return symbol


def ticker_dropdown_script(
    items: list[dict[str, str]],
    *,
    label: str = "Choose a ticker",
    truncated: bool = False,
) -> str:
    """Parent-page overlay script. One continuous list; no page controls."""
    payload = {
        "rows": items,
        "label": label or "Choose a ticker",
        "remainder": CLASS_REMAINDER_CAPTION if truncated else "",
        "pickLabel": CLASS_PICK_LABEL,
    }
    blob = json.dumps(payload, ensure_ascii=True).replace("<", "\\u003c")
    return TICKER_DROPDOWN_JS.replace("__PAYLOAD_JSON__", blob)


def _on_class_ticker_pick() -> None:
    """Place the overlay selection into the next compare slot.

    Runs before the script body, so the closed label and the slot row both
    paint the chosen ticker on this run. Search stays on the class query —
    the ticker label lives on the dropdown, not by clearing the box.
    """
    symbol = normalize_ticker(str(st.session_state.get(CLASS_PICK_KEY) or ""))
    st.session_state[CLASS_PICK_KEY] = ""
    if not symbol:
        return
    st.session_state[CLASS_CHOSEN_KEY] = symbol
    _ensure_slot_state()
    updated, nxt = place_search_match(
        list(st.session_state.slots), int(st.session_state.active_slot), symbol
    )
    st.session_state.slots = updated
    st.session_state.active_slot = nxt


def _install_search_typeahead(
    dropdown_items: list[dict[str, str]] | None = None,
    *,
    label: str = "Choose a ticker",
    truncated: bool = False,
) -> None:
    """One 1px iframe: prefix typeahead, or the class-browse ticker overlay.

    Share preview must not use ``components.html`` — mounting a ``/component``
    iframe on the Share click rerun is the residual MPA **Page not found**
    after #36. Class browse passes ``dropdown_items`` (possibly empty, to
    tear the overlay down). Prefix search leaves it ``None``.
    """
    if dropdown_items is not None:
        script = ticker_dropdown_script(
            dropdown_items, label=label, truncated=truncated
        )
    else:
        script = search_typeahead_script()
    components.html(
        f"<script>{script}</script>",
        height=1,
        scrolling=False,
    )


def _render_class_browse(
    matches: list[TickerOption],
    query: str,
    *,
    directory_down: bool,
    use_fixtures: bool,
    client,
) -> None:
    """Compact ticker label. Click opens one full-width scrollable overlay.

    The list is the loaded class shard (up to ``CLASS_PAGE_LIMIT``), not a
    paged button strip. Open, scroll, and close do not rerun Streamlit.
    Choosing a row places that ticker and closes the panel. No radio and no
    fixed-height ``st.container`` — those crashed Chrome on Stocks, ETFs,
    and on activating GOLD.
    """
    with st.container(key="rwa_class_browse"):
        if directory_down:
            st.error(LIVE_UNAVAILABLE_BANNER)
            _clear_match_pick_state()
            _install_search_typeahead(dropdown_items=[])
            return
        if not matches:
            _clear_match_pick_state()
            _install_search_typeahead(dropdown_items=[])
            if len((query or "").strip()) >= SEARCH_MIN_CHARS:
                st.caption("No directory matches — type a ticker or tap a category.")
            return
        if CLASS_PICK_KEY not in st.session_state:
            st.session_state[CLASS_PICK_KEY] = ""
        st.text_input(
            CLASS_PICK_LABEL,
            key=CLASS_PICK_KEY,
            label_visibility="collapsed",
            on_change=_on_class_ticker_pick,
        )
        truncated = bool(not use_fixtures and class_browse_truncated(client, query))
        chosen = str(st.session_state.get(CLASS_CHOSEN_KEY) or "")
        _install_search_typeahead(
            ticker_dropdown_items(matches),
            label=class_dropdown_label(matches, chosen),
            truncated=truncated,
        )
        if truncated:
            st.caption(CLASS_REMAINDER_CAPTION)


def _render_search_picker(
    catalog: list[TickerOption],
    use_fixtures: bool,
    client=None,
    scorer=None,
) -> None:
    """Categories → compact Search + attached match dropdown.

    Category chips are Streamlit buttons (``rwa_class_*``). A click writes
    ``ticker_query`` before Search on this rerun — same session, so compare
    slots stay put. Chip-keyword queries skip the typeahead iframe so the
    compact Matches selectbox is not remount-thrashed. Do not use
    ``<a href="?…">``. ``?rwa_cat=`` deep-links still work.
    Typed queries commit on each keystroke (debounced) so Matches appear at
    3+ characters without Enter. After a successful place, ``_clear_search``
    empties the box first so the match dropdown is not created.
    """
    if st.session_state.get("_clear_search"):
        st.session_state["_clear_search"] = False
        st.session_state.ticker_query = ""
        _clear_match_pick_state()
        try:
            del st.query_params["rwa_cat"]
        except (KeyError, TypeError):
            pass

    chip_cats = browse_categories(catalog)
    raw_cat = st.query_params.get("rwa_cat")
    if raw_cat:
        cid = raw_cat if isinstance(raw_cat, str) else (
            raw_cat[0] if isinstance(raw_cat, (list, tuple)) and raw_cat else str(raw_cat)
        )
        cat = CATEGORY_BY_ID.get(str(cid)) or next(
            (row for row in chip_cats if row.id == str(cid)), None
        )
        if cat is not None:
            st.session_state.ticker_query = chip_query(cat)
        try:
            del st.query_params["rwa_cat"]
        except (KeyError, TypeError):
            pass

    # Widget buttons, not <a href="?…"> — a full navigation remounts Streamlit
    # and Chrome Aw Snaps after a slot change (query/session fight). Same-run
    # ticker_query write keeps compare slots in session_state.
    st.markdown(
        '<div class="rat-cat-row" role="list" data-rat-cat-pill="1"></div>',
        unsafe_allow_html=True,
    )
    chip_cols = st.columns(len(chip_cats), gap="small")
    for col, cat in zip(chip_cols, chip_cats):
        with col:
            if st.button(
                chip_display_label(cat.label),
                key=f"rwa_class_{cat.id}",
                use_container_width=True,
                on_click=_on_class_pill,
                args=(chip_query(cat),),
            ):
                st.session_state.ticker_query = chip_query(cat)
                _clear_match_pick_state()

    query = st.text_input(
        "Search",
        placeholder="Ticker, name, or category",
        label_visibility="visible",
        key="ticker_query",
        on_change=_on_search_query_change,
    )
    # Category-button and class-keyword queries already committed ticker_query.
    # Skip the 1px typeahead iframe so the Matches selectbox is not fighting a
    # MutationObserver remount on the same run (Aw Snap when opening the list).
    # Keep SEARCH_MATCH_KEY stable — do not delete it while the menu can be open.
    if not is_browse_chip_query(query) and not is_class_browse_query(query):
        _install_search_typeahead()
    # Pill click updates Search after the outer catalog build. Reload for
    # this query so a 401/429 sets class_shard_status before the banner.
    catalog = catalog_for_active_query(scorer, catalog, query)
    matches = search_matches(
        query, catalog, client, use_fixtures=use_fixtures
    )
    directory_down = live_unavailable_banner(use_fixtures, client, query)
    if directory_down:
        # Before the compare chips render. Defaults NVDA/TSLA/AAPL/META are
        # a stub shortlist under a Live outage — clear them, do not swap in
        # FixtureClient. Slot button keys stay so the row does not remount.
        clear_compare_slots()
    if is_class_browse_query(query):
        # Compact ticker overlay, not a radio in a fixed-height container and
        # not a paged button strip. Those Aw Snapped Chrome on Stocks / ETFs
        # and when a row was activated. A 250-option selectbox is also wrong
        # here: it mounts a BaseWeb menu on this same pill rerun.
        _render_class_browse(
            matches,
            query,
            directory_down=directory_down,
            use_fixtures=use_fixtures,
            client=client,
        )
    elif directory_down:
        # Empty Matches. Do not paint fixture stubs under a Live label, and
        # do not swap the client to FixtureClient.
        st.error(LIVE_UNAVAILABLE_BANNER)
        _clear_match_pick_state()
    elif matches:
        options = [opt.symbol for opt in matches]
        labels = {opt.symbol: format_option(opt) for opt in matches}
        stored = st.session_state.get(SEARCH_MATCH_KEY)
        # None / empty is unselected. Deleting the key here remounts the
        # selectbox mid-click (Aw Snap). Only drop a pick that left the strip.
        if stale_match_pick(stored, options) and SEARCH_MATCH_KEY in st.session_state:
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
        _clear_match_pick_state()
        if len((query or "").strip()) >= SEARCH_MIN_CHARS:
            st.caption("No directory matches — type a ticker or tap a category.")
    if directory_down:
        st.caption("Choose a ticker")

    if use_fixtures:
        st.caption(
            "Fixture catalog: "
            + ", ".join(FIXTURE_TICKERS)
            + " + XOM, PLD, GOLD, USTB, SPY, EUR, HOME + Backed bTokens "
            "(bNVDA, bIB01, bCSPX, bC3M, bIBTA). "
            "Prefix (NIV → NVDA / Nvidia, bNV → bNVDA) or a CMC RWA class "
            "(Stocks, Commodities, Treasuries, ETFs, Real Estate, Currencies — "
            "the six CMC asset_type pills), then choose a match. "
            "Fixture mode is not live CMC."
        )
    else:
        st.caption(
            "Live mode: the same CMC RWA map page for a category tap and for "
            "typeahead (one class page, scroll Matches, up to 250 names) plus "
            "published Backed bToken PoR symbols (bNVDA, …). "
            "Prefix-match a ticker or tap a CMC RWA class, then choose a "
            "match. BTC/ETH are not RWA. Crypto and Look sector chips are "
            "not on this bar."
        )


def _in_streamlit_script() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
    except Exception:  # noqa: BLE001
        return False
    return get_script_run_ctx() is not None


def _shard_store() -> dict:
    """Session class shards. Falls back to the process memo in unit tests."""
    try:
        store = st.session_state.get("catalog_shards")
        if not isinstance(store, dict):
            store = {}
            st.session_state["catalog_shards"] = store
        return store
    except Exception:  # noqa: BLE001 — pytest / no ScriptRunContext
        return _catalog_shard_memo


def pending_search_query() -> str:
    """Search text the next catalog build should honor (chip or typed)."""
    try:
        if st.session_state.get("_clear_search"):
            return ""
        raw_cat = st.query_params.get("rwa_cat")
        if raw_cat:
            cid = raw_cat if isinstance(raw_cat, str) else (
                raw_cat[0] if isinstance(raw_cat, (list, tuple)) and raw_cat else str(raw_cat)
            )
            cat = CATEGORY_BY_ID.get(str(cid))
            if cat is not None:
                return chip_query(cat)
        return str(st.session_state.get("ticker_query") or "")
    except Exception:  # noqa: BLE001 — pytest / no ScriptRunContext
        return ""


def _class_shard(scorer: TransparencyScorer, asset_type: str) -> list[TickerOption]:
    """One cached CMC class page. Warm hits do not walk the directory.

    A non-empty success stays cached. ``[]`` is reused only for a recorded
    successful empty page. Failed empties are not written, and a cached
    ``[]`` is not a warm hit while the class is unavailable or the retry
    window has elapsed.
    """
    source = str(getattr(scorer.client, "source", "") or "")
    store = _shard_store()
    key = f"{source}:{client_shard_token(scorer.client)}:{asset_type}"
    # A recent 401/429 stays empty without another directory call.
    if class_shard_failure_is_fresh(scorer.client, asset_type):
        store.pop(key, None)
        return []
    if key in store:
        hit = store.get(key) or []
        if hit:
            return list(hit)
        if empty_class_page_is_cached_success(scorer.client, asset_type):
            return []
        store.pop(key, None)
    try:
        if _in_streamlit_script():
            rows = list(_cached_class_catalog_data(source == "fixture", asset_type))
        else:
            rows = cached_class_catalog(
                scorer.client, asset_type, first_page_only=True
            )
    except _LiveDirectoryUnavailable:
        store.pop(key, None)
        return []
    except Exception:
        if class_shard_status(scorer.client, asset_type).unavailable:
            store.pop(key, None)
            return []
        raise
    if class_shard_status(scorer.client, asset_type).unavailable:
        store.pop(key, None)
        return []
    if not rows and not empty_class_page_is_cached_success(scorer.client, asset_type):
        store.pop(key, None)
        return []
    store[key] = rows
    return list(rows)


def _ticker_catalog(
    scorer: TransparencyScorer, query: str = ""
) -> list[TickerOption]:
    """POR rows always; CMC classes only when Search needs them.

    Live initial paint is Backed bTokens only — no full-book map /
    ``assets_list_all`` walk. Tapping Treasuries (or typing a class keyword)
    loads that class's first page. Fixture mode still hydrates every class
    on an empty query so the tiny local JSON catalog stays complete.
    Warm shards stay in session / ``@st.cache_data`` so widget reruns do
    not rebuild the directory.
    """
    source = str(getattr(scorer.client, "source", "") or "")
    wanted = classes_for_query(query)
    if source == "fixture" and not wanted:
        wanted = tuple(RWA_CLASS_IDS)
    store = _shard_store()
    prefix = f"{source}:{client_shard_token(scorer.client)}:"
    for kind in wanted:
        # ``_class_shard`` writes successes only. Assigning its return here
        # would pin a failed ``[]`` and let the next browse skip ``rwa_map``.
        _class_shard(scorer, kind)
        key = f"{prefix}{kind}"
        if class_shard_status(scorer.client, kind).unavailable or (
            key in store and not store[key]
            and not empty_class_page_is_cached_success(scorer.client, kind)
        ):
            store.pop(key, None)
    merged = list(catalog_from_por_feeds())
    for key, rows in list(store.items()):
        if key.startswith(prefix):
            merged = merge_search_catalog(merged, rows)
    return merge_por_catalog(merged, catalog_from_por_feeds())


def chip_display_label(label: str) -> str:
    """Chip text: spaces around slashes so wrap cannot split a word."""
    return (label or "").replace("/", " / ")


def _render_share_controls(report: dict, *, slot_index: int) -> None:
    """Scorecard preview first; only the bottom Share button may post to X.

    The ``Scorecard`` control builds and shows the PNG only. That click must
    not call ``attach_x_share`` / ``post_image`` / ``XClient``. After the
    preview is visible, ``download PNG`` and the bottom ``Share`` button are
    independent. Preview/download use ``st.markdown`` data URIs (not
    ``st.image`` / ``st.download_button`` / ``components.html``). Do not
    ``st.rerun()`` after a click. PNG bytes / caption are unchanged.
    """
    ticker = str(report.get("ticker") or "UNK")
    state_key, pending_key = share_session_keys(slot_index, ticker)
    # pending_key is kept for session-key compat; it is not an auto-post latch.
    st.session_state.pop(pending_key, None)
    if st.button(SCORECARD_BUTTON_LABEL, key=f"share_btn_{slot_index}_{ticker}"):
        try:
            bundle = share_score_card(report, post_to_x=False)
        except Exception as exc:  # noqa: BLE001 — card UI must stay up
            st.session_state[state_key] = None
            st.error(f"Could not build score card: {exc}")
            return
        # Preview only — no X network on this click.
        if bundle.png_bytes:
            bundle.x_posted = False
            bundle.x_url = None
            bundle.x_message = ""
        st.session_state[state_key] = bundle

    bundle = st.session_state.get(state_key)
    if bundle is None:
        st.caption("Click Scorecard to preview the signed PNG. Nothing is posted to X.")
        return
    if not bundle.png_bytes:
        st.error(
            "Could not build the score card image. Download is unavailable — "
            "try Scorecard again."
        )
        raw = str(getattr(bundle, "x_message", "") or "")
        if raw.startswith(PNG_BUILD_FAILED_PREFIX) and "{" not in raw:
            st.caption(raw)
        return

    # Preview renders here — still no X client / network.
    _show_share_png(bundle.png_bytes, bundle.filename)
    st.caption(f"Signature fingerprint: `{bundle.fingerprint}`")
    footer = share_footer_markdown(bundle)
    if footer:
        st.markdown(footer)

    # Bottom of the scorecard view. Only this button may post to X.
    if st.button(SHARE_BUTTON_LABEL, key=f"share_x_btn_{slot_index}_{ticker}"):
        if x_credentials_ready():
            bundle = attach_x_share(bundle)
        else:
            bundle.x_posted = False
            bundle.x_url = None
            bundle.x_message = MISSING_CREDS_MESSAGE
        st.session_state[state_key] = bundle

    if not x_credentials_ready():
        st.caption("X credentials not set — you can still download the PNG.")

    status = _user_facing_share_status(bundle)
    if bundle.x_posted:
        st.success(status)
        st.caption("Download PNG and Share stay independent. The Share button stays Share.")
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
    """Posted caption, or a polished skip — never raw X API errors or URLs."""
    posted = bool(getattr(bundle, "x_posted", False))
    message = str(getattr(bundle, "x_message", "") or "")
    if posted:
        return "Posted to X."
    if message in {"", X_POST_DISABLED_MESSAGE}:
        return ""
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
    is the free-form path. Voice output is out of MVP. Failures use the templated fallback.
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
    return use_fixtures


def _render_sidebar_tail(reports: list[dict], client: object | None) -> None:
    """CMC call journal under weights, above disclaimer. Collapsed by default.

    One source of truth for this-run CMC/fixture calls. Fixture journals
    never claim LIVE endpoints.
    """
    block = collect_cmc_calls(reports, client) if client is not None else {
        "source": "unknown",
        "live": False,
        "endpoints": [],
    }
    with st.sidebar:
        with st.expander("CMC calls this run", expanded=False):
            for line in format_cmc_calls_lines(block):
                st.caption(line)
        with st.expander("Disclaimer", expanded=False):
            st.write(DISCLAIMER)
        st.markdown("[Privacy Policy](/privacy) · [Terms of Service](/terms)")
        st.caption("These do not replace the Disclaimer.")


def _render_card_details(report: dict, *, slot_index: int = 0) -> None:
    """Collapsed pillar evidence on the compare card. xAI / share stay gated elsewhere."""
    ticker = str(report.get("ticker") or "UNK")
    with open_expander(
        "Pillar evidence",
        expanded=False,
        key=f"pillar_ev_{slot_index}_{ticker}",
    ):
        st.caption(data_source_caption(report))

        st.caption(
            "Backing / reserves / redemption use **issuer-name heuristics** when live "
            "attestation/PoR is unavailable — labeled **heuristic fallback**, not audited attestations."
        )

        caption = basis_status_caption(report.get("basis") or {})
        if caption:
            st.caption(caption)

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
        SHARE_EXPANDER_LABEL,
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
      /* Open prefix Matches scroll inside the menu.
         The closed selectbox stays one line. Do not remount it while open. */
      div[data-baseweb="popover"] [role="listbox"],
      div[data-baseweb="menu"] [role="listbox"] {{
        max-height: 18rem;
        overflow-y: auto;
      }}
      /* Class-browse ticker dropdown. Label stays compact in the Matches
         slot; the panel is fixed to the content column and scrolls as one
         list over the compare tiles. Not a nested st.container height. */
      #rwa-ticker-dd-anchor {{
        position: relative;
        z-index: 40;
        min-height: 2.25rem;
      }}
      .rwa-ticker-dd-label {{
        display: inline-flex;
        align-items: center;
        justify-content: space-between;
        gap: 0.55rem;
        max-width: min(22rem, 100%);
        min-width: 11rem;
        padding: 0.32rem 0.65rem;
        border: 1px solid rgba(250, 250, 250, 0.2);
        border-radius: 0.45rem;
        background: rgba(38, 39, 48, 0.96);
        color: inherit;
        font: inherit;
        font-size: 0.95rem;
        line-height: 1.3;
        cursor: pointer;
        text-align: left;
      }}
      .rwa-ticker-dd-text {{
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }}
      .rwa-ticker-dd-arrow {{
        flex: 0 0 auto;
        font-size: 0.75rem;
        opacity: 0.85;
      }}
      .rwa-ticker-dd-label[aria-expanded="true"] .rwa-ticker-dd-arrow {{
        transform: rotate(180deg);
      }}
      .rwa-ticker-dd-panel {{
        position: fixed;
        z-index: 1000;
        display: flex;
        flex-direction: column;
        box-sizing: border-box;
        background: rgb(14, 17, 23);
        border: 1px solid rgba(250, 250, 250, 0.2);
        border-radius: 0.5rem;
        box-shadow: 0 16px 48px rgba(0, 0, 0, 0.45);
        overflow: hidden;
      }}
      .rwa-ticker-dd-panel[hidden] {{
        display: none !important;
      }}
      .rwa-ticker-dd-scroll {{
        overflow-y: auto;
        overflow-x: hidden;
        min-height: 0;
        flex: 1 1 auto;
      }}
      .rwa-ticker-dd-option {{
        display: block;
        width: 100%;
        text-align: left;
        padding: 0.38rem 0.8rem;
        border: 0;
        border-bottom: 1px solid rgba(250, 250, 250, 0.06);
        background: transparent;
        color: inherit;
        font: inherit;
        font-size: 0.92rem;
        cursor: pointer;
      }}
      .rwa-ticker-dd-option:hover,
      .rwa-ticker-dd-option:focus {{
        background: rgba(250, 250, 250, 0.08);
        outline: none;
      }}
      .rwa-ticker-dd-note {{
        flex: 0 0 auto;
        padding: 0.4rem 0.8rem 0.55rem;
        font-size: 0.8rem;
        opacity: 0.85;
        border-top: 1px solid rgba(250, 250, 250, 0.12);
      }}
      .st-key-class_ticker_pick {{
        position: absolute !important;
        width: 1px !important;
        height: 1px !important;
        margin: 0 !important;
        padding: 0 !important;
        overflow: hidden !important;
        opacity: 0 !important;
        pointer-events: none !important;
        clip: rect(0, 0, 0, 0) !important;
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
      /* Expander chevron sits beside the label, not stranded at the far right
         of a full-width row (Scorecard / why / ask / CMC calls). */
      div[data-testid="stExpander"] summary,
      div[data-testid="stExpander"] [class*="expanderHeader"] {{
        display: inline-flex !important;
        justify-content: flex-start !important;
        align-items: center !important;
        gap: 0.4rem !important;
        width: max-content !important;
        max-width: 100%;
      }}
      div[data-testid="stExpander"] summary svg,
      div[data-testid="stExpander"] [data-testid="stExpanderToggleIcon"] {{
        margin-left: 0.15rem !important;
        flex: 0 0 auto !important;
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
      .rat-cat-row[data-rat-cat-pill] {{
        height: 0;
        margin: 0;
        overflow: hidden;
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
        cursor: pointer;
        font-family: inherit;
        appearance: none;
        -webkit-appearance: none;
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
    _render_sidebar_tail([], None)
    st.error(str(exc))
    st.stop()

_ensure_slot_state()

st.subheader("Score / Compare")
st.caption(
    "Browse a category, type a ticker or name (3+ chars), then choose a match. "
    "Click a slot to choose which one the next pick replaces."
)

catalog = _ticker_catalog(scorer, query=pending_search_query())
_render_search_picker(catalog, use_fixtures, client=scorer.client, scorer=scorer)

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
_render_sidebar_tail(ok_reports, scorer.client)

st.divider()
st.caption(DISCLAIMER)
st.markdown("[Privacy Policy](/privacy) · [Terms of Service](/terms)")
