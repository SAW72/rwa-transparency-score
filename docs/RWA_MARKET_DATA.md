# RWA market data

Live market data is CoinGecko. The client calls `GET https://api.coingecko.com/api/v3/coins/markets` with `vs_currency=usd`, `ids` from the hand-kept allowlist (at most 250 ids), and `price_change_percentage=24h`. It does **not** use CoinGecko's `real-world-assets-rwa` category. That category includes tokens that are not RWAs.

The allowlist is `rwa_score/data/rwa_allowlist.json`. Each row has a name, symbol, chain and contract when one was read from a public payload, a CoinGecko id, and `asset_class: unknown`. Ids were checked with a keyless `GET /coins/markets` on 2026-10-18. Classes are not guessed.

Optional env var (name only; set the value in Render, never commit it): `COINGECKO_DEMO_API_KEY`. The client sends it as the header `x-cg-demo-api-key`. If the variable is unset or empty, the app uses CoinGecko's public keyless endpoints and shows: "Using CoinGecko public keyless access; limits are lower."

## Rate limit and cache

| Rule | Value |
|---|---|
| Per-minute cap | 30 calls (Demo plan publishes 100) |
| Daily budget | 300 calls (under a 10,000/month Demo ceiling) |
| Quote TTL | 10 minutes |
| Directory TTL | 30 minutes (allowlist file; memory cache) |
| Refresh | At least every 24 hours (these TTLs are shorter) |
| HTTP 429 | Honor `Retry-After` only for digits or an HTTP-date. Otherwise 1s / 2s / 4s. At most 3 retries. Total sleep per request capped at 10 seconds |
| After failure | Serve the last cached payload with `as of <timestamp>`. Do not crash the page |
| Disk | Not required. Render's free disk is ephemeral |

`app.py` also keeps the search directory in `st.cache_data` (30 minutes) and one scorer in `st.cache_resource`.

## Fallback

If CoinGecko returns HTTP 429, HTTP 5xx, or a network error, price, market cap, and volume are read from `https://pro-api.coinmarketcap.com/public-api/v3/cryptocurrency/quotes/latest` by symbol. No key header is sent. The card says the row came from that keyless source. Commercial terms for this keyless path are **UNCONFIRMED**.

If both sources fail, the last cached row is shown with an `as of` time.

## Attribution and terms

Show "Data provided by CoinGecko" and "Powered by CoinGecko" with a link to https://www.coingecko.com/en/api.

CoinGecko's terms require attribution and a refresh at least every 24 hours. Cached rows in this app expire well inside that window.

**Open question:** CoinGecko's Demo plan table shows no commercial license (paid plans are marked commercial). Spencer must confirm this usage is permitted before relying on it for a commercial service. This file does not decide that.

## Fields that are not available

CoinGecko `/coins/markets` does not supply these. The UI says **not available**. The scorer does not treat them as zero and does not treat them as a pass.

| Field | Status |
|---|---|
| CIK | not available |
| Venue names (`tradfi_markets`) | not available |
| RWA rank | not available |
| Per-asset holders | not available |
| Issuer backing | not available |
| Asset class `currency` | not covered by this source |
| Asset class `real_estate` | not covered by this source |
| Asset class `government_security` | not covered by this source |
| Asset class `stock` / `etf` / `commodity` | not labeled on the allowlist (`asset_class` is unknown) |

The six pills stay on the bar. In live mode each one says "not covered by this source".

A row is **unverified** (shown, not dropped, not used as a measured price) when market cap is missing, zero, or negative; price is missing or non-positive; `price * circulating_supply` differs from market cap by more than 5%; market cap exceeds $10,000,000,000; market cap is more than 25% of the allowlist's positive market-cap total; or CoinGecko and the keyless fallback disagree by more than 20%. Figure Heloc (about $25.6B on 2026-10-18) trips the ceiling and the share rule. That is a flag, not a corrected number.

Where a token has no Chainlink or xStocks proof-of-reserve reading, the card says "no on-chain backing evidence found".

## Fixture catalog

Bundled demo fixtures are **not live**. They still include one ticker for each of the six classes (`stock`, `commodity`, `government_security`, `etf`, `real_estate`, `currency`), including `GOLD` and `USTB`. BTC / ETH are not an RWA-bar pill.

| `asset_type` | UI label |
|---|---|
| `stock` | Stocks |
| `commodity` | Commodities |
| `currency` | Currencies |
| `government_security` | Treasuries |
| `etf` | ETFs |
| `real_estate` | Real Estate |

Wired endpoints:

- `https://api.coingecko.com/api/v3/coins/markets`
- `https://pro-api.coinmarketcap.com/public-api/v3/cryptocurrency/quotes/latest`
