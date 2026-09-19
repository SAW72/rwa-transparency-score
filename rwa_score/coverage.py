"""CMC RWA coverage matrix — category → ticker → endpoint → status.

Live catalogs come from paginated ``map`` + ``assets/list``. Fixture catalogs
are labeled ``fixture`` and never claim live. Native crypto is a separate
class, not an RWA row.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .client import ASSET_TYPE_LABELS, ASSET_TYPES, ENDPOINT_ASSETS_LIST, ENDPOINT_MAP
from .ticker_search import (
    BACKED_SEARCH_SOURCE,
    CRYPTO_ID,
    RWA_CLASS_IDS,
    TickerOption,
    load_search_catalog,
)

WIRED_ENDPOINTS = (
    "/v5/real-world-assets/map",
    "/v5/real-world-assets/assets/list",
    "/v5/real-world-assets/info",
    "/v5/real-world-assets/issuers/list",
    "/v5/real-world-assets/issuers",
    "/v5/real-world-assets/quotes/latest",
    "/v5/real-world-assets/market-pairs/list",
    "/v2/cryptocurrency/quotes/latest",
)


def _status_for(option: TickerOption, *, client_source: str) -> str:
    if option.source == BACKED_SEARCH_SOURCE:
        return "labeled_por_catalog"
    if client_source == "fixture" or option.source == "fixture":
        return "fixture"
    if option.source in {"rwa_map", "rwa_assets_list"}:
        return "live"
    return "missing"


def _endpoint_wired(option: TickerOption) -> str:
    if option.source == BACKED_SEARCH_SOURCE:
        return "no — Backed PoR catalog (not a CMC RWA map row)"
    if option.source == "rwa_assets_list":
        return f"yes — {ENDPOINT_ASSETS_LIST} (+ {ENDPOINT_MAP})"
    return f"yes — {ENDPOINT_MAP} (+ {ENDPOINT_ASSETS_LIST})"


def coverage_rows(
    catalog: Sequence[TickerOption],
    *,
    client_source: str,
) -> list[dict[str, Any]]:
    """One row per (RWA class or crypto) × ticker in the directory."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for opt in catalog:
        classes = [cid for cid in opt.categories if cid in RWA_CLASS_IDS or cid == CRYPTO_ID]
        if not classes and opt.asset_type in ASSET_TYPES:
            classes = [opt.asset_type]
        if not classes:
            classes = ["unclassified"]
        for cid in classes:
            key = (cid, opt.symbol)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "category": ASSET_TYPE_LABELS.get(cid, cid),
                    "category_id": cid,
                    "ticker": opt.symbol,
                    "name": opt.name,
                    "asset_type": opt.asset_type,
                    "endpoint_wired": _endpoint_wired(opt),
                    "status": _status_for(opt, client_source=client_source),
                    "source": opt.source,
                }
            )
    rows.sort(key=lambda row: (RWA_CLASS_IDS.index(row["category_id"]) if row["category_id"] in RWA_CLASS_IDS else 99, row["ticker"]))
    return rows


def missing_rwa_classes(catalog: Sequence[TickerOption]) -> tuple[str, ...]:
    present = {cid for opt in catalog for cid in opt.categories if cid in RWA_CLASS_IDS}
    present.update(opt.asset_type for opt in catalog if opt.asset_type in ASSET_TYPES)
    return tuple(cid for cid in RWA_CLASS_IDS if cid not in present)


def render_coverage_markdown(
    catalog: Sequence[TickerOption],
    *,
    client_source: str,
    note: str = "",
) -> str:
    rows = coverage_rows(catalog, client_source=client_source)
    missing = missing_rwa_classes(catalog)
    lines = [
        "# CMC RWA coverage",
        "",
        "Spencer requirement: every official CMC RWA class appears in the UI; every",
        "ticker CMC lists under those classes is wired through live `map` +",
        "`assets/list` (paginated). No stub ticker list. No keys in repo.",
        "",
        "## Official taxonomy (docs + live endpoints)",
        "",
        "CMC Pro RWA docs (`/v5/real-world-assets/*`) expose **six** `asset_type`",
        "values — not the old Look industry chips (AI/Tech, Oil, …). Public docs",
        "cite **7.9K+** tokenized RWAs. `map` is 0 credits and paginated",
        "(`start` / `limit`, max 250, `total_size` / `has_more`). `assets/list`",
        "is the same pagination (1 credit / 250).",
        "",
        "| `asset_type` | UI label |",
        "|---|---|",
    ]
    for cid, label in ASSET_TYPE_LABELS.items():
        lines.append(f"| `{cid}` | {label} |")
    lines.extend(
        [
            "",
            "Live directory is **paginated** `map` (0 credits) plus paginated "
            "`assets/list` (1 credit / 250). Not a hard-coded ticker stub.",
            "",
            "Wired CMC endpoints (live when `CMC_API_KEY` is set; fixture mode stays labeled):",
            "",
        ]
    )
    for endpoint in WIRED_ENDPOINTS:
        lines.append(f"- `{endpoint}`")
    lines.extend(
        [
            "",
            "BTC / ETH (and WBTC / WETH) are **Crypto / Digital Assets**, never an RWA class.",
            "",
            "## What was missing / fixed",
            "",
            "- UI chips were a small Look taxonomy (AI/Tech, Oil, …) plus a subset of",
            "  `asset_type` values, and only chips with fixture hits were shown.",
            "- `rwa_map()` took one unpaginated page and ignored `has_more` / `total_size`,",
            "  so the live directory dropped most of the 7.9K book.",
            "- `assets/list` was a single page of 100.",
            "- Fixture catalog had stocks only — Commodities / Treasuries / ETFs /",
            "  Currencies / CMC `real_estate` were missing from Search.",
            "- Category controls were tall Streamlit column blocks; they are now a",
            "  horizontal wrapping pill row.",
            "- Fix: paginate `map` + `assets/list`, always show the six CMC classes,",
            "  add labeled fixture rows per class, keep industry chips as extras,",
            "  never file BTC/ETH under RWA.",
            "",
        ]
    )
    if note:
        lines.extend([note, ""])
    if missing:
        lines.append(
            "Missing CMC RWA classes in this catalog: "
            + ", ".join(f"`{cid}`" for cid in missing)
            + "."
        )
        lines.append("")
    else:
        lines.append("Every official CMC RWA class has at least one directory ticker.")
        lines.append("")
    lines.extend(
        [
            f"Catalog source: **{client_source}** ({len(catalog)} tickers, {len(rows)} class rows).",
            "",
            "| Category | Ticker | Endpoint wired | Status |",
            "|---|---|---|---|",
        ]
    )
    for row in rows:
        lines.append(
            f"| {row['category']} | `{row['ticker']}` | {row['endpoint_wired']} | `{row['status']}` |"
        )
    lines.append("")
    return "\n".join(lines)


def coverage_from_client(client: Any) -> str:
    catalog = load_search_catalog(client)
    source = getattr(client, "source", "") or "unknown"
    note = (
        "This snapshot is **bundled demo fixtures** — not live CoinMarketCap."
        if source == "fixture"
        else "This snapshot is from live CMC `map` + `assets/list` (paginated)."
    )
    return render_coverage_markdown(catalog, client_source=source, note=note)


def iter_tickers_for_class(
    catalog: Sequence[TickerOption], category_id: str
) -> Iterable[TickerOption]:
    for opt in catalog:
        if category_id in opt.categories or opt.asset_type == category_id:
            yield opt
