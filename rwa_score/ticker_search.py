"""Prefix ticker search over the CMC / fixture RWA directory.

The Streamlit bar is exact-assign unless the typed query is long enough to
open a picker. Matching is case-insensitive prefix on the ticker symbol and
on names / aliases already present in ``rwa_map`` (and optional CMC alias
fields). No extra paid API and no invented static universe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

SEARCH_MIN_CHARS = 3
PICKER_LIMIT = 8

# CMC / fixture map rows sometimes carry these extra name-like keys.
_ALIAS_KEYS = ("aliases", "alias", "known_aliases")
_SKIP_NAME_TOKENS = {"inc", "corp", "ltd", "llc", "co", "the", "plc", "sa"}


@dataclass(frozen=True)
class TickerOption:
    """One picker row: underlying stock/RWA symbol plus display names."""

    symbol: str
    name: str = ""
    aliases: tuple[str, ...] = ()

    def match_keys(self) -> tuple[str, ...]:
        keys = [self.symbol, self.name, *self.aliases]
        return tuple(k for k in keys if k and str(k).strip())


def normalize_ticker(raw: str) -> str:
    return (raw or "").strip().upper()


def normalize_query(raw: str) -> str:
    """Strip only — search is case-insensitive, not ticker-normalized."""
    return (raw or "").strip()


def format_option(option: TickerOption) -> str:
    name = (option.name or "").strip()
    if name:
        return f"{option.symbol} — {name}"
    return option.symbol


def _iter_aliases(row: dict[str, Any]) -> list[str]:
    found: list[str] = []
    slug = (row.get("slug") or "").strip()
    if slug:
        found.append(slug.replace("-", " "))
        if "-" in slug:
            found.append(slug)
    for key in _ALIAS_KEYS:
        raw = row.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            found.append(raw)
        elif isinstance(raw, Iterable) and not isinstance(raw, (bytes, bytearray)):
            for item in raw:
                if item:
                    found.append(str(item))
    name = (row.get("name") or "").strip()
    for token in name.replace(",", " ").split():
        cleaned = token.strip(".,()").strip()
        if len(cleaned) >= SEARCH_MIN_CHARS and cleaned.lower() not in _SKIP_NAME_TOKENS:
            found.append(cleaned)
    # Preserve order, drop empties / dups (case-insensitive).
    seen: set[str] = set()
    unique: list[str] = []
    for alias in found:
        text = str(alias).strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        unique.append(text)
    return unique


def catalog_from_rwa_map(assets: Sequence[dict[str, Any]] | None) -> list[TickerOption]:
    """Build picker options from a CMC / fixture ``rwa_map`` payload."""
    options: list[TickerOption] = []
    seen: set[str] = set()
    for row in assets or []:
        if not isinstance(row, dict):
            continue
        symbol = normalize_ticker(row.get("symbol") or "")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        name = str(row.get("name") or "").strip()
        options.append(
            TickerOption(symbol=symbol, name=name, aliases=tuple(_iter_aliases(row)))
        )
    return options


def load_search_catalog(client: Any) -> list[TickerOption]:
    """Read the directory the app already uses. Failures yield an empty catalog."""
    try:
        assets = client.rwa_map()
    except Exception:  # noqa: BLE001 — search must not take down scoring
        return []
    return catalog_from_rwa_map(assets)


def _adjacent_transposition(left: str, right: str) -> bool:
    """True when ``left`` is ``right`` with one neighboring pair swapped."""
    if len(left) != len(right) or left == right:
        return False
    diffs = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
    if len(diffs) != 2:
        return False
    i, j = diffs
    return j == i + 1 and left[i] == right[j] and left[j] == right[i]


def _is_prefix_hit(query: str, value: str) -> bool:
    """Case-insensitive prefix, plus one adjacent transposition on that prefix.

    Strict prefix covers ``NVD`` → NVDA and ``NVI`` → Nvidia. Spencer-style
    ``NIV`` is a one-swap of Nvidia's opening ``NVI`` — still a prefix match
    for typeahead, not a fuzzy search over the whole catalog.
    """
    text = (value or "").strip().lower()
    if not text:
        return False
    if text.startswith(query):
        return True
    window = text[: len(query)]
    return len(window) == len(query) and _adjacent_transposition(query, window)


def prefix_matches(query: str, option: TickerOption) -> bool:
    q = normalize_query(query).lower()
    if not q:
        return False
    return any(_is_prefix_hit(q, key) for key in option.match_keys())


def _rank(option: TickerOption, query: str) -> tuple[int, int, int, int, int, str]:
    q = query.lower()
    symbol = option.symbol.lower()
    name = (option.name or "").lower()
    alias_keys = [a.lower() for a in option.aliases]
    exact_symbol = 0 if symbol == q else 1
    symbol_prefix = 0 if symbol.startswith(q) else 1
    exact_name = 0 if name == q or q in alias_keys else 1
    name_prefix = 0 if name.startswith(q) or any(a.startswith(q) for a in alias_keys) else 1
    near_prefix = 0 if prefix_matches(q, option) else 1
    return (exact_symbol, symbol_prefix, exact_name, name_prefix, near_prefix, symbol)


def search_tickers(
    query: str,
    catalog: Sequence[TickerOption],
    *,
    min_chars: int = SEARCH_MIN_CHARS,
    limit: int = PICKER_LIMIT,
) -> list[TickerOption]:
    """Prefix-match ticker / name / aliases once the query is long enough."""
    q = normalize_query(query)
    if len(q) < min_chars:
        return []
    hits = [opt for opt in catalog if prefix_matches(q, opt)]
    hits.sort(key=lambda opt: _rank(opt, q))
    return hits[: max(0, int(limit))]


def resolve_assign_symbol(
    query: str,
    *,
    matches: Sequence[TickerOption] | None = None,
    selected_symbol: str | None = None,
) -> str:
    """Symbol Assign / slot-click should place.

    Explicit picker choice wins, then the first match (selectbox default),
    then the normalized typed query (legacy exact-assign fallback).
    """
    picked = normalize_ticker(selected_symbol or "")
    if picked:
        return picked
    if matches:
        return matches[0].symbol
    return normalize_ticker(query)
