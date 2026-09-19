# CMC RWA coverage

Spencer requirement: every official CMC RWA class appears in the UI; every
ticker CMC lists under those classes is wired through live `map` +
`assets/list` (paginated). No stub ticker list. No keys in repo.

## Official taxonomy (docs + live endpoints)

CMC Pro RWA docs (`/v5/real-world-assets/*`) expose **six** `asset_type`
values — not the old Look industry chips (AI/Tech, Oil, …). Public docs
cite **7.9K+** tokenized RWAs. `map` is 0 credits and paginated
(`start` / `limit`, max 250, `total_size` / `has_more`). `assets/list`
is the same pagination (1 credit / 250).

| `asset_type` | UI label |
|---|---|
| `stock` | Stocks |
| `commodity` | Commodities |
| `currency` | Currencies |
| `government_security` | Treasuries |
| `etf` | ETFs |
| `real_estate` | Real Estate |

Live directory is **paginated** `map` (0 credits) plus paginated `assets/list` (1 credit / 250). Not a hard-coded ticker stub.

Wired CMC endpoints (live when `CMC_API_KEY` is set; fixture mode stays labeled):

- `/v5/real-world-assets/map`
- `/v5/real-world-assets/assets/list`
- `/v5/real-world-assets/info`
- `/v5/real-world-assets/issuers/list`
- `/v5/real-world-assets/issuers`
- `/v5/real-world-assets/quotes/latest`
- `/v5/real-world-assets/market-pairs/list`
- `/v2/cryptocurrency/quotes/latest`

BTC / ETH (and WBTC / WETH) are never an RWA class and have no RWA-bar pill.

## What was missing / fixed

- UI chips were a small Look taxonomy (AI/Tech, Oil, …) plus a subset of
  `asset_type` values, and only chips with fixture hits were shown.
- `rwa_map()` took one unpaginated page and ignored `has_more` / `total_size`,
  so the live directory dropped most of the 7.9K book.
- `assets/list` was a single page of 100.
- Fixture catalog had stocks only — Commodities / Treasuries / ETFs /
  Currencies / CMC `real_estate` were missing from Search.
- Category controls were tall Streamlit column blocks; they are now a
  horizontal wrapping pill row.
- Fix: paginate `map` + `assets/list` (and fill missing `asset_type`
  classes when the default listing is stock-scoped), always show the
  six CMC classes as the only RWA-bar pills, add labeled fixture rows
  per class, keep industry keywords typeable (not on that bar), never
  file BTC/ETH under RWA, keep Backed bTokens searchable.

This snapshot is **bundled demo fixtures** — not live CoinMarketCap.

Every official CMC RWA class has at least one directory ticker.

Catalog source: **fixture** (16 tickers, 17 class rows).

| Category | Ticker | Endpoint wired | Status |
|---|---|---|---|
| Stocks | `AAPL` | yes — /v5/real-world-assets/map (+ /v5/real-world-assets/assets/list) | `fixture` |
| Stocks | `META` | yes — /v5/real-world-assets/map (+ /v5/real-world-assets/assets/list) | `fixture` |
| Stocks | `NVDA` | yes — /v5/real-world-assets/map (+ /v5/real-world-assets/assets/list) | `fixture` |
| Stocks | `PLD` | yes — /v5/real-world-assets/map (+ /v5/real-world-assets/assets/list) | `fixture` |
| Stocks | `TSLA` | yes — /v5/real-world-assets/map (+ /v5/real-world-assets/assets/list) | `fixture` |
| Stocks | `XOM` | yes — /v5/real-world-assets/map (+ /v5/real-world-assets/assets/list) | `fixture` |
| Commodities | `GOLD` | yes — /v5/real-world-assets/map (+ /v5/real-world-assets/assets/list) | `fixture` |
| Treasuries | `USTB` | yes — /v5/real-world-assets/map (+ /v5/real-world-assets/assets/list) | `fixture` |
| ETFs | `SPY` | yes — /v5/real-world-assets/map (+ /v5/real-world-assets/assets/list) | `fixture` |
| Real Estate | `HOME` | yes — /v5/real-world-assets/map (+ /v5/real-world-assets/assets/list) | `fixture` |
| Real Estate | `PLD` | yes — /v5/real-world-assets/map (+ /v5/real-world-assets/assets/list) | `fixture` |
| Currencies | `EUR` | yes — /v5/real-world-assets/map (+ /v5/real-world-assets/assets/list) | `fixture` |
| unclassified | `bC3M` | no — Backed PoR catalog (not a CMC RWA map row) | `labeled_por_catalog` |
| unclassified | `bCSPX` | no — Backed PoR catalog (not a CMC RWA map row) | `labeled_por_catalog` |
| unclassified | `bIB01` | no — Backed PoR catalog (not a CMC RWA map row) | `labeled_por_catalog` |
| unclassified | `bIBTA` | no — Backed PoR catalog (not a CMC RWA map row) | `labeled_por_catalog` |
| unclassified | `bNVDA` | no — Backed PoR catalog (not a CMC RWA map row) | `labeled_por_catalog` |
