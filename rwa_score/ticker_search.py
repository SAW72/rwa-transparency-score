"""Prefix + category ticker search over the CMC / fixture RWA directory.

The Streamlit bar is exact-assign unless the query opens a picker:
  - ticker / name / alias prefix after ``SEARCH_MIN_CHARS``
  - CMC RWA ``asset_type`` classes (``stock``, ``commodity``, ``etf``, …)
  - optional industry keywords (``AI``, ``oil``, …) even when short

CMC RWA classes come from the official ``asset_type`` enum on ``map`` and
``assets/list`` (paginated — not a hard-coded ticker stub). Industry chips
are extra Look-style filters over ``industry`` / name hints. Native crypto
(BTC / ETH and wraps) is never filed under an RWA class.

Published Backed **bToken** symbols from ``BACKED_POR_FEEDS`` are merged in as
first-class picker rows so ``bNV`` / ``bNVDA`` (and feed aliases) can land a
card that is eligible for the on-chain PoR badge. xStocks DataLink names with
no proxy are not injected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Sequence

from .chainlink_por import (
    BACKED_POR_FEEDS,
    XSTOCKS_POR_FEEDS,
    PorFeed,
    canonical_backed_por_feed,
)
from .client import ASSET_TYPE_LABELS, ASSET_TYPES

SEARCH_MIN_CHARS = 3
CATEGORY_MIN_CHARS = 2
PICKER_LIMIT = 8

# CMC / fixture map rows sometimes carry these extra name-like keys.
_ALIAS_KEYS = ("aliases", "alias", "known_aliases")
_INDUSTRY_KEYS = ("industry", "sector", "industry_name")
_SKIP_NAME_TOKENS = {"inc", "corp", "ltd", "llc", "co", "the", "plc", "sa"}
_SKIP_INDUSTRY = {"", "spot", "stock", "crypto", "cryptocurrency"}


@dataclass(frozen=True)
class Category:
    """One browse bucket. ``keywords`` are what the search bar accepts."""

    id: str
    label: str
    keywords: tuple[str, ...]
    name_hints: tuple[str, ...] = ()


# Official CMC RWA ``asset_type`` enum — always shown in the UI.
# Keywords are case-insensitive; spaces are OK.
RWA_CLASS_CATEGORIES: tuple[Category, ...] = (
    Category(
        id="stock",
        label="Stocks",
        keywords=("stock", "stocks", "equity", "equities"),
    ),
    Category(
        id="commodity",
        label="Commodities",
        keywords=("commodity", "commodities"),
    ),
    Category(
        id="government_security",
        label="Treasuries",
        keywords=(
            "government_security",
            "treasury",
            "treasuries",
            "fixed income",
            "t-bill",
            "tbill",
        ),
    ),
    Category(
        id="etf",
        label="ETFs",
        keywords=("etf", "etfs"),
    ),
    Category(
        id="real_estate",
        label="Real Estate",
        keywords=("real_estate", "real estate", "reit", "realty", "property", "estate"),
        name_hints=("prologis", "pld", "simon", "american tower"),
    ),
    Category(
        id="currency",
        label="Currencies",
        keywords=("currency", "currencies", "fx"),
    ),
)

# Optional industry browse over rows already in the CMC/fixture directory.
INDUSTRY_CATEGORIES: tuple[Category, ...] = (
    Category(
        id="ai_tech",
        label="AI/Tech",
        keywords=("ai", "tech", "technology", "semiconductor", "software", "computer"),
        name_hints=("nvidia", "apple", "meta", "microsoft", "alphabet", "amd", "intel"),
    ),
    Category(
        id="oil_energy",
        label="Oil/Energy",
        keywords=("oil", "energy", "petroleum", "crude", "refining", "gas"),
        name_hints=("exxon", "chevron", "conocophillips", "xom", "cvx"),
    ),
    Category(
        id="auto_ev",
        label="Auto/EV",
        keywords=("auto", "ev", "vehicle", "automotive", "motor"),
        name_hints=("tesla", "ford", "rivian", "lucid"),
    ),
    Category(
        id="finance",
        label="Finance",
        keywords=("finance", "bank", "financial", "insurance"),
        name_hints=("jpmorgan", "goldman", "visa", "berkshire"),
    ),
)

# Native crypto — never an RWA class. Shown only if a row leaks into the catalog.
CRYPTO_CATEGORY = Category(
    id="crypto_digital",
    label="Crypto / Digital Assets",
    keywords=("crypto", "cryptocurrency", "digital asset", "digital assets"),
)
NATIVE_CRYPTO_SYMBOLS = frozenset({"BTC", "ETH", "WBTC", "WETH"})

CATEGORIES: tuple[Category, ...] = (
    *RWA_CLASS_CATEGORIES,
    *INDUSTRY_CATEGORIES,
    CRYPTO_CATEGORY,
)
RWA_CLASS_IDS = tuple(cat.id for cat in RWA_CLASS_CATEGORIES)
INDUSTRY_IDS = tuple(cat.id for cat in INDUSTRY_CATEGORIES)
CRYPTO_ID = CRYPTO_CATEGORY.id

CATEGORY_BY_ID = {cat.id: cat for cat in CATEGORIES}
CATEGORY_LABELS = {cat.id: cat.label for cat in CATEGORIES}

CATEGORY_DOC = (
    "Search categories (type a keyword, case-insensitive; spaces OK):\n"
    "CMC RWA asset_type (always in the UI; directory from paginated map + assets/list):\n"
    "- Stocks — stock, stocks, equity, equities\n"
    "- Commodities — commodity, commodities\n"
    "- Treasuries — government_security, treasury, treasuries, fixed income\n"
    "- ETFs — etf, etfs\n"
    "- Real Estate — real_estate, real estate, reit, realty, property\n"
    "- Currencies — currency, currencies, fx\n"
    "Industry chips (optional, over CMC/fixture industry or name hints):\n"
    "- AI/Tech — ai, tech, technology, semiconductor, software, computer\n"
    "- Oil/Energy — oil, energy, petroleum, crude, refining, gas\n"
    "- Auto/EV — auto, ev, vehicle, automotive, motor\n"
    "- Finance — finance, bank, financial, insurance\n"
    "BTC / ETH (and WBTC / WETH) are Crypto / Digital Assets, never an RWA class.\n"
    "Directory also lists published Backed bTokens (bNVDA, bIB01, bCSPX, bC3M, "
    "bIBTA) from BACKED_POR_FEEDS so on-chain PoR cards are searchable."
)

BACKED_SEARCH_SOURCE = "backed_por_feeds"
ASSETS_LIST_SOURCE = "rwa_assets_list"


@dataclass(frozen=True)
class TickerOption:
    """One picker row: underlying stock/RWA symbol plus display names."""

    symbol: str
    name: str = ""
    aliases: tuple[str, ...] = ()
    industry: str = ""
    categories: tuple[str, ...] = ()
    source: str = "rwa_map"
    asset_type: str = ""
    rwa_rank: int | None = None

    def match_keys(self) -> tuple[str, ...]:
        keys = [self.symbol, self.name, *self.aliases]
        return tuple(k for k in keys if k and str(k).strip())

    def category_labels(self) -> tuple[str, ...]:
        return tuple(CATEGORY_LABELS[cid] for cid in self.categories if cid in CATEGORY_LABELS)


def normalize_ticker(raw: str) -> str:
    """Uppercase CMC tickers; keep published bToken casing (``bNVDA``)."""
    text = (raw or "").strip()
    if not text:
        return ""
    feed = canonical_backed_por_feed(text)
    if feed is not None:
        return feed.symbol
    return text.upper()


def normalize_query(raw: str) -> str:
    """Strip and collapse whitespace — search is case-insensitive."""
    return re.sub(r"\s+", " ", (raw or "").strip())


def format_option(option: TickerOption) -> str:
    name = (option.name or "").strip()
    label = f"{option.symbol} — {name}" if name else option.symbol
    sector = [
        CATEGORY_LABELS[cid]
        for cid in option.categories
        if cid in INDUSTRY_IDS
    ]
    if sector:
        label = f"{label} · {sector[0]}"
    elif option.asset_type:
        label = f"{label} · {ASSET_TYPE_LABELS.get(option.asset_type, option.asset_type)}"
    elif CRYPTO_ID in option.categories:
        label = f"{label} · {CATEGORY_LABELS[CRYPTO_ID]}"
    return label


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


def _row_industry(row: dict[str, Any], info: dict[str, Any] | None = None) -> str:
    for src in (row, info or {}):
        if not isinstance(src, dict):
            continue
        for key in _INDUSTRY_KEYS:
            raw = src.get(key)
            if raw is None:
                continue
            text = str(raw).strip()
            if text and text.lower() not in _SKIP_INDUSTRY:
                return text
    return ""


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _exact_hint_hits(hint: str, haystack: str) -> bool:
    """Name hints are exact tokens (or a phrase). ``meta`` must not match metals."""
    text = hint.lower().strip()
    if not text or not haystack:
        return False
    if " " in text:
        return text in haystack
    return text in _tokens(haystack)


def _keyword_hits_haystack(keyword: str, haystack: str) -> bool:
    kw = keyword.lower().strip()
    if not kw or not haystack:
        return False
    if " " in kw:
        return kw in haystack
    for token in _tokens(haystack):
        if token == kw or (len(kw) >= 3 and token.startswith(kw)):
            return True
    return False


def is_native_crypto(
    *,
    symbol: str = "",
    name: str = "",
    asset_type: str = "",
) -> bool:
    """True for BTC/ETH (and wraps). CMC RWA ``asset_type`` rows stay RWA."""
    sym = (symbol or "").strip().upper()
    if sym in NATIVE_CRYPTO_SYMBOLS:
        return True
    kind = (asset_type or "").strip().lower()
    if kind in ASSET_TYPES:
        return False
    hay = f"{name} {symbol}".lower()
    return any(token in hay.split() for token in ("bitcoin", "ethereum"))


def classify_categories(
    *,
    symbol: str = "",
    name: str = "",
    industry: str = "",
    aliases: Sequence[str] = (),
    asset_type: str = "",
) -> tuple[str, ...]:
    """Return category ids for one directory row.

    CMC ``asset_type`` is the RWA class. Industry chips use industry / name
    hints only — they do not invent tickers. BTC/ETH never join an RWA class.
    """
    if is_native_crypto(symbol=symbol, name=name, asset_type=asset_type):
        return (CRYPTO_ID,)
    hits: list[str] = []
    kind = (asset_type or "").strip().lower()
    if kind in ASSET_TYPES:
        hits.append(kind)
    industry_hay = " ".join(
        part for part in (industry, name, symbol, *aliases) if part
    ).lower()
    for cat in INDUSTRY_CATEGORIES:
        if any(_keyword_hits_haystack(kw, industry_hay) for kw in cat.keywords):
            hits.append(cat.id)
            continue
        if any(_exact_hint_hits(hint, industry_hay) for hint in cat.name_hints):
            hits.append(cat.id)
    # Tokenized REITs are stocks on CMC; still browse under Real Estate.
    if "real_estate" not in hits:
        re_cat = CATEGORY_BY_ID["real_estate"]
        if any(_keyword_hits_haystack(kw, industry_hay) for kw in re_cat.keywords):
            hits.append("real_estate")
        elif any(_exact_hint_hits(hint, industry_hay) for hint in re_cat.name_hints):
            hits.append("real_estate")
    return tuple(hits)


def resolve_categories(query: str) -> tuple[str, ...]:
    """Category ids whose keywords/labels match the typed query."""
    q = normalize_query(query).lower()
    if len(q) < CATEGORY_MIN_CHARS:
        return ()
    hits: list[str] = []
    for cat in CATEGORIES:
        label = cat.label.lower()
        aliases = (
            label,
            cat.id.replace("_", " "),
            cat.id.replace("_", "/"),
            *cat.keywords,
        )
        matched = False
        for alias in aliases:
            if q == alias:
                matched = True
                break
            if len(q) >= SEARCH_MIN_CHARS and alias.startswith(q):
                matched = True
                break
        # "AI" is a 2-char exact keyword; prefix-of-keyword only from 3 chars.
        if not matched and len(q) >= CATEGORY_MIN_CHARS:
            for kw in cat.keywords:
                if q == kw:
                    matched = True
                    break
        if matched:
            hits.append(cat.id)
    return tuple(hits)


def catalog_from_rwa_map(
    assets: Sequence[dict[str, Any]] | None,
    *,
    info_by_id: dict[int, dict[str, Any]] | None = None,
) -> list[TickerOption]:
    """Build picker options from a CMC / fixture ``rwa_map`` payload."""
    options: list[TickerOption] = []
    seen: set[str] = set()
    extra_info = info_by_id or {}
    for row in assets or []:
        if not isinstance(row, dict):
            continue
        symbol = normalize_ticker(row.get("symbol") or "")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        name = str(row.get("name") or "").strip()
        aliases = tuple(_iter_aliases(row))
        rwa_id = row.get("rwa_id")
        try:
            info = extra_info.get(int(rwa_id)) if rwa_id is not None else None
        except (TypeError, ValueError):
            info = None
        industry = _row_industry(row, info)
        asset_type = str(row.get("asset_type") or (info or {}).get("asset_type") or "").strip()
        raw_rank = row.get("rwa_rank")
        if raw_rank is None and info:
            raw_rank = info.get("rwa_rank")
        try:
            rwa_rank = int(raw_rank) if raw_rank is not None else None
        except (TypeError, ValueError):
            rwa_rank = None
        categories = classify_categories(
            symbol=symbol,
            name=name,
            industry=industry,
            aliases=aliases,
            asset_type=asset_type,
        )
        options.append(
            TickerOption(
                symbol=symbol,
                name=name,
                aliases=aliases,
                industry=industry,
                categories=categories,
                source="rwa_map",
                asset_type=asset_type,
                rwa_rank=rwa_rank,
            )
        )
    return options


def catalog_from_assets_list(payload: Any) -> list[TickerOption]:
    """Picker rows from CMC / fixture ``assets/list`` (ranked, asset_type)."""
    if isinstance(payload, dict):
        rows = payload.get("rwa_assets") or []
    else:
        rows = payload or []
    return [replace(opt, source=ASSETS_LIST_SOURCE) for opt in catalog_from_rwa_map(rows)]


def catalog_from_por_feeds(feeds: Sequence[PorFeed] | None = None) -> list[TickerOption]:
    """First-class picker rows for published Chainlink PoR bTokens.

    Only feeds with a real proxy are included. Names and aliases come from the
    existing ``PorFeed`` catalog — no invented tickers or proxy addresses.
    """
    options: list[TickerOption] = []
    seen: set[str] = set()
    for feed in feeds if feeds is not None else BACKED_POR_FEEDS + XSTOCKS_POR_FEEDS:
        if not feed.proxy:
            continue
        symbol = str(feed.symbol or "").strip()
        key = symbol.upper()
        if not symbol or key in seen:
            continue
        seen.add(key)
        # Do not put "Finance" in the display name — that would dump every
        # bToken into the Finance category via the documented keyword list.
        aliases = tuple(a for a in (*feed.aliases, feed.unit, "Backed", "bToken") if a)
        name = f"Backed {feed.symbol} (Chainlink PoR)"
        categories = classify_categories(
            symbol=symbol, name=name, industry="", aliases=aliases
        )
        options.append(
            TickerOption(
                symbol=symbol,
                name=name,
                aliases=aliases,
                industry="",
                categories=categories,
                source=BACKED_SEARCH_SOURCE,
            )
        )
    return options


def merge_search_catalog(
    base: Sequence[TickerOption],
    extra: Sequence[TickerOption],
) -> list[TickerOption]:
    """Append extra rows whose symbols are not already in ``base`` (case-insensitive)."""
    merged = list(base)
    seen = {opt.symbol.upper() for opt in merged if opt.symbol}
    for opt in extra:
        key = (opt.symbol or "").upper()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(opt)
    return merged


def enrich_catalog_from_assets_list(
    base: Sequence[TickerOption],
    listed: Sequence[TickerOption],
) -> list[TickerOption]:
    """Overlay ``assets/list`` rank / asset_type onto map rows; append new symbols."""
    by_listed = {opt.symbol.upper(): opt for opt in listed if opt.symbol}
    out: list[TickerOption] = []
    seen: set[str] = set()
    for opt in base:
        key = opt.symbol.upper()
        extra = by_listed.get(key)
        if extra is None:
            out.append(opt)
        else:
            categories = tuple(dict.fromkeys((*opt.categories, *extra.categories)))
            out.append(
                replace(
                    opt,
                    name=opt.name or extra.name,
                    industry=opt.industry or extra.industry,
                    asset_type=opt.asset_type or extra.asset_type,
                    rwa_rank=opt.rwa_rank if opt.rwa_rank is not None else extra.rwa_rank,
                    categories=categories,
                )
            )
        if key:
            seen.add(key)
    for extra in listed:
        key = (extra.symbol or "").upper()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(extra)
    return out


def load_search_catalog(client: Any) -> list[TickerOption]:
    """Read the CMC/fixture map, enrich from ``assets/list``, then Backed bTokens.

    Map failures still return the Backed PoR catalog so ``bNVDA`` remains
    searchable. Fixture info is joined for ``industry`` (local JSON). Live
    mode does **not** call ``rwa_info`` per ticker — that would burn
    Basic-plan credits. ``map`` and ``assets/list`` are paginated and cached
    so every CMC RWA ticker is in the directory (not a stub list).
    """
    assets: Sequence[dict[str, Any]] | None = None
    try:
        assets = client.rwa_map()
    except Exception:  # noqa: BLE001 — search must not take down scoring
        assets = None
    info_by_id: dict[int, dict[str, Any]] = {}
    if assets is not None and getattr(client, "source", "") == "fixture":
        for row in assets or []:
            if not isinstance(row, dict) or row.get("rwa_id") is None:
                continue
            try:
                rid = int(row["rwa_id"])
                info = client.rwa_info(rid)
            except Exception:  # noqa: BLE001
                continue
            if info:
                info_by_id[rid] = info
    base = catalog_from_rwa_map(assets, info_by_id=info_by_id) if assets is not None else []
    listed: list[TickerOption] = []
    fetch_all = getattr(client, "assets_list_all", None)
    fetch_list = getattr(client, "assets_list", None)
    try:
        if callable(fetch_all):
            listed = catalog_from_assets_list(fetch_all())
        elif callable(fetch_list):
            listed = catalog_from_assets_list(fetch_list())
    except Exception:  # noqa: BLE001 — search stays up without the ranked book
        listed = []
    if listed:
        base = enrich_catalog_from_assets_list(base, listed)
    extra = catalog_from_por_feeds()
    return merge_search_catalog(base, extra)


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


def category_matches(query: str, option: TickerOption) -> bool:
    wanted = set(resolve_categories(query))
    return bool(wanted) and bool(wanted.intersection(option.categories))


def _rank(option: TickerOption, query: str) -> tuple[int, int, int, int, int, int, int, str]:
    q = query.lower()
    symbol = option.symbol.lower()
    name = (option.name or "").lower()
    alias_keys = [a.lower() for a in option.aliases]
    exact_symbol = 0 if symbol == q else 1
    symbol_prefix = 0 if symbol.startswith(q) else 1
    exact_name = 0 if name == q or q in alias_keys else 1
    name_prefix = 0 if name.startswith(q) or any(a.startswith(q) for a in alias_keys) else 1
    is_prefix = 0 if prefix_matches(q, option) else 1
    is_category = 0 if category_matches(q, option) else 1
    rank = option.rwa_rank if option.rwa_rank is not None else 10**9
    return (is_prefix, exact_symbol, symbol_prefix, exact_name, name_prefix, is_category, rank, symbol)


def search_tickers(
    query: str,
    catalog: Sequence[TickerOption],
    *,
    min_chars: int = SEARCH_MIN_CHARS,
    limit: int = PICKER_LIMIT,
) -> list[TickerOption]:
    """Prefix-match ticker / name, or list a category bucket."""
    q = normalize_query(query)
    if not q:
        return []
    cats = resolve_categories(q)
    allow_prefix = len(q) >= min_chars
    if not allow_prefix and not cats:
        return []
    hits = [
        opt
        for opt in catalog
        if (allow_prefix and prefix_matches(q, opt)) or (cats and category_matches(q, opt))
    ]
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
