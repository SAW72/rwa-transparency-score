# RAT Score

RAT Score (RWA Transparency Score) — an AI-assisted risk radar for tokenized stocks. Rates issuers 0–100 on backing, proof of reserves, redemption, price integrity, disclosure, and cross-issuer basis using CoinMarketCap’s RWA API.

Judges: one command, no API key.

```bash
pip install -r requirements.txt
RWA_USE_FIXTURES=1 streamlit run app.py
```

Then open the local URL Streamlit prints. Search `NVDA`, `TSLA`, or `AAPL`. Compare two or three tickers side by side.

CLI equivalent:

```bash
python -m rwa_score --fixtures NVDA TSLA AAPL
python -m rwa_score.demo
```

## The problem

Tokenized stocks (RWAs) can fail the same way traditional wrappers do — inflated claims, thin backing, then sudden closure — except on-chain you can surface a warning **before** the doors close.

This tool rates a tokenized stock on how its public CMC/issuer signals look under our published heuristics, using CoinMarketCap's RWA API (or bundled demo fixtures when you do not have a key).

## The score (0–100)

| Pillar | What it checks | Weight | Verification |
|---|---|---|---|
| Backing model | Real shares with a regulated custodian vs. a thin debt note | 20% | on-chain PoR / attested when a verifier is registered; else **heuristic fallback** |
| Proof of reserves | Independent PoR or attestation vs. a promise | 20% | on-chain PoR (Chainlink / Backed) or attested (Dinari); else **heuristic fallback** |
| Redemption rights | Redeemable for the underlying share vs. sell-only | 15% | Robinhood verifier when registered; else heuristic (TODO live hook for Backed/Dinari) |
| Price integrity | Token 24h drift stays contained | 15% | self-reported (CMC quote) |
| Disclosure | Real, matchable SEC CIK vs. missing | 15% | self-reported (CMC RWA info) |
| Cross-issuer basis | Same underlying ticker, different wrapper prices (xStocks / Ondo / Dinari / …) | 15% | self-reported (CMC RWA market-pairs) |

Weights sum to **100%**. The sixth pillar took 5 points each from backing, reserves, and redemption (25/25/20 → 20/20/15). Price and disclosure stay at 15%. Heuristic and attestation paths for the first five pillars are unchanged.

**Basis math:** group CMC market-pairs by wrapper `crypto_id` (one product across venues), take a volume-weighted USD price per wrapper, then `spread% = (max − min) / mid × 100`. Score is `max(15, 100 − |spread%| × 10)` so a 0.5% gap → 95, 5% → 50, ≥8.5% → 15. One wrapper or a missing pairs payload is labeled unverified (defaults 55 / 50) — never treated as a measured tight market.

Bands: **GREEN** ≥ 75 · **YELLOW** ≥ 50 · **ORANGE** ≥ 25 · **RED** below 25.

### Live attestation / PoR sources (free, no new paid APIs)

| Source | Endpoint | How we use it |
|---|---|---|
| Backed / xStocks via **Chainlink Proof of Reserve** | On-chain `AggregatorV3Interface.latestRoundData()` at the public SmartData proxy ([feed addresses](https://docs.chain.link/data-feeds/smartdata/addresses)). Backed currently publishes these on **Polygon** (e.g. bNVDA `0x0fB2beD999da86Cb1Fdd97E746600A96141EeA09`, bIB01 `0xad4395fc414Fc1575A7a38C20B0Bfdbdb09ee41A`). | `eth_call` via `POLYGON_RPC_URL` (or `BASE_RPC_URL` / `ETH_RPC_URL` when a feed lives there). `collateralization_ratio = reserves / circulatingSupply` when the token `totalSupply` is readable on the same chain (≥0.999→95, ≥0.99→80, ≥0.95→60, else 30). Reserves-only (supply unread) scores **90**. Cached 1 hour. Badge: **on-chain PoR**. Tickers without a published feed (most xStocks symbols) stay on the labeled **heuristic fallback**. |
| Dinari dShares page | `https://dinari.com/dshares` | Scrape for Big-4 / audit firm, Alpaca custody, and a **1:1** claim. Score 85 when all three are present. Badge: **attested** — labeled **attestation pending** (no signed report URL yet). |
| Robinhood tokenized stocks | CMC issuer name `"Robinhood"` (no public PoR URL) | Static self-reported scores: backing **55**, reserves **40**, redemption **35**. Badge: **self-reported**. These tokens are **debt securities** — holders are creditors of Robinhood Assets Jersey, not shareholders of AAPL/NVDA/TSLA/META. Self-reported 1:1, no public proof of reserves. A low/orange score is expected; the bug this verifier fixes is missing-issuer heuristic fallback (~47) with the wrong label. |

Unknown issuers stay on the name-match path in `rwa_score/issuer_registry.py`. Matching is a known-good allowlist at word boundaries (Backed Finance, Ondo, Paxos, xStocks, Securitize, …) — never the bare substring `backed`. Negative tokens (`not backed`, `unbacked`, `anti-`) reject first. Robinhood is **not** on the fully-backed / audited / redeemable lists. The UI and CLI label those as heuristics — they are not audited attestations.

`ISSUER_NOTES` on the score payload is a short equity-vs-debt line (Robinhood = debt / creditors of Robinhood Assets Jersey; Backed, Dinari, Ondo, and others when the name matches). The AI explainer reads that field.

### AI explainer (“Why this score?”)

Each compare card shows a **Why this score?** blurb. When `XAI_API_KEY` is set, `rwa_score/explainer.py` POSTs the score, band, pillar notes, evidence, and issuer note to `https://api.x.ai/v1/chat/completions` (`grok-4.1-fast`, `max_tokens` 300, `requests` only). Missing key or any failure uses a templated fallback from the same pillar notes — the UI never crashes.

The Streamlit app caches explanations **24 hours per symbol** in a process-local dict. Footnote on every blurb: **generated by AI, not financial advice**.

Set `XAI_API_KEY` in `.env` locally or in the **Render dashboard** (`sync: false` in `render.yaml`, same pattern as `CMC_API_KEY`). Never commit the key.

### Heuristic fallback disclaimer

If a live verifier errors, times out, or is skipped (fixture/offline mode), the pillar **falls back to issuer-name heuristics** and is explicitly labeled **heuristic fallback**. Failed verifiers are never dropped silently — the error is recorded in notes/flags. Heuristics are **not** audited attestations.

## How to run

### Offline fixtures (recommended for judging)

No `CMC_API_KEY`. No live credits. Clearly labeled demo data in `rwa_score/fixtures/demo_cmc.json`.

```bash
pip install -r requirements.txt
RWA_USE_FIXTURES=1 streamlit run app.py
# or
python -m rwa_score --fixtures NVDA TSLA AAPL
```

Fixture catalog: `NVDA` (solid / Backed Finance, tight wrapper spread), `AAPL` (mid / xStocks, moderate spread), `TSLA` (thin wrapper, wide cross-issuer gap), `META` (Ondo, tight spread). Demo `market_pairs` samples live next to the other CMC-shaped fixtures so offline judging still scores the sixth pillar.

### Live CMC

```bash
cp .env.example .env   # set CMC_API_KEY, set RWA_USE_FIXTURES=0
python -m rwa_score NVDA
RWA_USE_FIXTURES=0 streamlit run app.py
```

Get a free Basic key at [coinmarketcap.com/api](https://coinmarketcap.com/api/).

**Basic plan rate limits:** CMC Basic keys allow only a few HTTP requests per minute (`HTTP 429` / `error_code 1008`, *“You've exceeded your API Key's HTTP request rate limit. Rate limits reset every minute”*). The live client retries those with exponential backoff and jitter (honors `Retry-After`, waits ~60s total), then raises a clear error. Issuer `list` + per-issuer detail are cached for the process lifetime; map/info use a short TTL so Streamlit widget reruns do not re-fetch. The demo keeps one client/scorer via `@st.cache_resource`.

If you still see 429, **wait a minute** and retry. [DoraHacks Startup](https://coinmarketcap.com/api/) unlocks a higher request rate (and more credits) than Basic.

## Architecture

```
app.py                 Streamlit demo (search, pillars, verification badges, compare, AI explainer)
rwa_score/client.py    Live CMC client + FixtureClient + create_client()
rwa_score/scorer.py    Weighted pillars, bands, verification levels, no silent fails
rwa_score/chainlink_por.py  Chainlink AggregatorV3 PoR reader (JSON-RPC eth_call, requests only)
rwa_score/verifiers.py Backed Chainlink PoR + Dinari scrapers + Robinhood debt-wrapper scores
rwa_score/explainer.py xAI Grok “Why this score?” with templated fallback
rwa_score/issuer_registry.py   Name-match heuristics + ISSUER_NOTES (equity vs debt)
rwa_score/fixtures/    Demo JSON shaped like CMC RWA responses
```

Live data flow (CMC Basic):

1. `GET /v5/real-world-assets/map` — ticker → `rwa_id` (0 credits)
2. `GET /v5/real-world-assets/info` — issuer metadata + **SEC CIK**
3. `GET /v5/real-world-assets/issuers/list` then `/issuers` — who mints the token, on-chain `crypto_id` (cached for the process lifetime)
4. `GET /v2/cryptocurrency/quotes/latest` — token 24h change for price integrity
5. `GET /v5/real-world-assets/market-pairs/list` — wrapper venues/prices for the **cross-issuer basis** pillar (short TTL cache; first page, `limit=100`)

## Tests

```bash
pytest -q
```

CI runs the same command. No real API key is required.

## Deploy

### `RWA_USE_FIXTURES`

| Value | Mode |
|---|---|
| `0` | Live path: CMC (needs `CMC_API_KEY`) plus Chainlink PoR verifiers |
| `1` | Offline demo fixtures only — no live CMC, no live attestation |

**Render must stay at `RWA_USE_FIXTURES=0`.** That default lives in `render.yaml`. Flipping the variable in the Render dashboard alone is **not** enough: the next blueprint sync / redeploy re-applies `render.yaml` and overwrites the dashboard value.

`CMC_API_KEY` and `XAI_API_KEY` stay `sync: false`. Set them in the Render dashboard for live CMC and the AI explainer — never commit the keys. The explainer falls back to a template when `XAI_API_KEY` is unset.

Optional RPC overrides (no keys): `POLYGON_RPC_URL`, `BASE_RPC_URL`, `ETH_RPC_URL`. When unset, the verifier uses public no-key endpoints (Polygon `publicnode` / `polygon-rpc.com`, Base `mainnet.base.org`, Ethereum `cloudflare-eth.com`). Paid or key-gated RPC URLs must stay in the dashboard — never commit them.

**Render (blueprint):** `render.yaml` starts via `python -m rwa_score.health` on `0.0.0.0:$PORT` so `GET /health` is registered before Streamlit's SPA catch-all.

```text
New Web Service → this repo → Build: pip install -r requirements.txt
Start: python -m rwa_score.health --server.port $PORT --server.address 0.0.0.0 --server.headless true
Env: RWA_USE_FIXTURES=0   (keep this; do not set 1 on Render)
Dashboard secrets: CMC_API_KEY, XAI_API_KEY (optional; templated fallback if unset)
Optional RPC: POLYGON_RPC_URL, BASE_RPC_URL, ETH_RPC_URL (public fallbacks if unset)
```

Confirm live mode (no fixture overwrite, verifiers enabled) with:

```bash
curl -sS https://rwa-transparency-score.onrender.com/health
```

Expected when Render is live:

```json
{"fixtures": false, "verifiers_live": true, "backed_feed": "ok", "timestamp": "2026-09-10T01:13:00Z"}
```

`backed_feed` is `"ok"` if the canonical Backed Chainlink PoR feed (bIB01 on Polygon) answers `latestRoundData` over JSON-RPC, otherwise `"down"`. The handler always returns JSON — a down feed does not crash `/health` and does not change the health JSON shape. Local demos can still run `RWA_USE_FIXTURES=1 streamlit run app.py`. For a process-start `/health` route locally, use the same launcher as Render: `python -m rwa_score.health`.

**Streamlit Community Cloud:** deploy `app.py` from the repo root. Secrets: leave `CMC_API_KEY` empty and set `RWA_USE_FIXTURES=1`, or add a key and set `RWA_USE_FIXTURES=0` for live mode.

This repo is **deploy-config ready**. A public URL appears only after you connect the repo to Render or Streamlit Cloud.

## Disclaimer

Informational and educational hackathon demo only. Not financial, investment, legal, or tax advice. Not an offer, solicitation, or recommendation to buy, sell, or hold any security, digital asset, tokenized stock, or other instrument. Scores are automated heuristics (including issuer-name matching) plus third-party CoinMarketCap data or bundled demo fixtures — not audited attestations, not legal or audit opinions, and not a substitute for issuer filings, prospectuses, offering documents, or your own independent research. Data may be incomplete, delayed, inaccurate, or outdated. Nothing here guarantees accuracy, completeness, or fitness for any purpose. Past or present scores are not indicative of future results. This demo is not provided by a broker-dealer, exchange, ATS, funding portal, or registered investment adviser, and it does not create any advisory or fiduciary relationship. Do your own research. Use at your own risk.

## License

MIT
