# xStocks Chainlink PoR check (package A2)

Checked **2026-09-17** for [issue #23](https://github.com/SAW72/rwa-transparency-score/issues/23).

**Result:** no public **AggregatorV3 / SmartData `proxyAddress`** for the xStocks line. Live backing/reserves for `TSLAx`, `AAPLx`, `METAx`, and other x-suffix names without a Backed **bToken** Polygon feed stay on the labeled **heuristic fallback**. We did **not** invent feed addresses.

## What we looked at

| Source | What we found |
|---|---|
| [Chainlink SmartData addresses](https://docs.chain.link/data-feeds/smartdata/addresses) | xStocks names appear as **Tokenized Equities** NAV / price feeds and as **DataLink Proof of Reserve streams**. Those PoR rows set **`proxyAddress: null`**, `deliveryChannelCode: DS`, `serviceLevel: Datalink`. Example path: `tslax-por-datalink-proofofreserves-mainnet-production`. |
| [feeds-matic-mainnet.json](https://reference-data-directory.vercel.app/feeds-matic-mainnet.json) | Published PoR proxies are Backed **bTokens** only (`bNVDA`, `bIB01`, `bCSPX`, `bC3M`, `bIBTA`) plus unrelated issuers (COPW, RYT, CGT). No `TSLAx` / `AAPLx` / `METAx` aggregator. |
| [feeds-mainnet.json](https://reference-data-directory.vercel.app/feeds-mainnet.json) (Ethereum) | 0 xStocks / `*x` PoR aggregator hits. |
| Solana / Avalanche / BSC reference catalogs | 0 xStocks PoR aggregator hits. Base / Optimism / `feeds-polygon-mainnet.json` / `feeds-ethereum-mainnet.json` returned HTTP 404. |
| [xStocks docs](https://docs.xstocks.fi/llms.txt) | REST PoR (`GET https://api.xstocks.fi/api/v2/public/proof-of-reserves/{symbol}`) is documented. No SmartData proxy table. Chainlink mentions on the product side are **price** Data Streams / multipliers, not an on-chain PoR aggregator. |

## Why DataLink streams are not wired

`rwa_score/chainlink_por.py` reads **on-chain** `AggregatorV3Interface.latestRoundData()` at a public proxy. A DataLink / Data Streams product with `proxyAddress: null` has no such address. Wiring stream IDs as if they were proxies would fabricate an on-chain PoR path.

REST PoR is self-reported issuer JSON, not Chainlink on-chain PoR. It is also **not** wired as `on-chain PoR`.

## Alias note

`NVDA` / `NVDAx` already alias to the published **bNVDA** Polygon proxy (Backed bToken). That is the bToken feed, not an xStocks DataLink stream. `TSLAx` / `AAPLx` / `METAx` have no bToken proxy and stay heuristic.

## When to add feeds

If Chainlink publishes a non-null SmartData `proxyAddress` for an xStock, add a `PorFeed` to `XSTOCKS_POR_FEEDS` (or `BACKED_POR_FEEDS`) with that exact address. Do not copy DataLink stream IDs into `proxy`.
