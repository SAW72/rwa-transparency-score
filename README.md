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
| Backed **bTokens** via **Chainlink Proof of Reserve** | On-chain `AggregatorV3Interface.latestRoundData()` at the public SmartData proxy ([feed addresses](https://docs.chain.link/data-feeds/smartdata/addresses)). **Polygon only today** — see coverage note below. | `eth_call` via `POLYGON_RPC_URL` (or `BASE_RPC_URL` / `ETH_RPC_URL` when a feed lives there). **Reserves-only score 90** when the feed publishes a positive reserve balance. Same-chain ERC-20 `totalSupply` is **not** used for a ratio (multi-chain issuance / units unverified — live bIB01 ≈ 80 vs 4442 looks like 0.018 and must not be scored as undercollateralized PoR). An implausible ratio is ignored, not labeled a clean on-chain miss. Cached 1 hour. Badge: **on-chain PoR**. |
| Dinari dShares page | `https://dinari.com/dshares` | Scrape for Big-4 / audit firm, Alpaca custody, and a **1:1** claim. Score 85 when all three are present. Badge: **attested** — labeled **attestation pending** (no signed report URL yet). |
| Robinhood tokenized stocks | CMC issuer name `"Robinhood"` (no public PoR URL) | Static self-reported scores: backing **55**, reserves **40**, redemption **35**. Badge: **self-reported**. These tokens are **debt securities** — holders are creditors of Robinhood Assets Jersey, not shareholders of AAPL/NVDA/TSLA/META. Self-reported 1:1, no public proof of reserves. A low/orange score is expected; the bug this verifier fixes is missing-issuer heuristic fallback (~47) with the wrong label. |

### Chainlink PoR coverage (judges)

**Do not expect every ticker to hit the oracle.**

Chainlink Proof of Reserve currently covers Backed **bTokens on Polygon only**. The wired feeds in `rwa_score/chainlink_por.py` are **bNVDA**, **bIB01**, **bCSPX**, **bC3M**, and **bIBTA** (aliases such as `NVDA` / `NVDAx` map to bNVDA).

Most **xStocks** symbols (`TSLAx`, `AAPLx`, `METAx`, …) have **no published Chainlink PoR / SmartData aggregator yet**. Live backing/reserves for those names use the labeled **heuristic fallback**. That is expected, not a bug.

**Next upgrade:** no on-chain Chainlink PoR / SmartData proxy addresses for the xStocks line were found in the [SmartData directory](https://docs.chain.link/data-feeds/smartdata/addresses), the public reference-data catalogs (Ethereum / Polygon / other listed networks), or xStocks public oracle docs. xStocks today exposes REST PoR (`GET /public/proof-of-reserves/{symbol}`) and Chainlink **price** Data Streams (pull-based; no aggregator address). Alliance copy says they are adopting Chainlink PoR — when a SmartData proxy ships, add it to `BACKED_POR_FEEDS`.

Unknown issuers stay on the name-match path in `rwa_score/issuer_registry.py`. Matching is a known-good allowlist at word boundaries (Backed Finance, Ondo, Paxos, xStocks, Securitize, …) — never the bare substring `backed`. Negative tokens (`not backed`, `unbacked`, `anti-`) reject first. Robinhood is **not** on the fully-backed / audited / redeemable lists. The UI and CLI label those as heuristics — they are not audited attestations.

`ISSUER_NOTES` on the score payload is a short equity-vs-debt line (Robinhood = debt / creditors of Robinhood Assets Jersey; Backed, Dinari, Ondo, and others when the name matches). The AI explainer reads that field.

### AI explainer (“Why this score?”)

Each compare card shows a **Why this score?** blurb. When `XAI_API_KEY` is set, `rwa_score/explainer.py` POSTs the score, band, pillar notes, evidence, and issuer note to `https://api.x.ai/v1/chat/completions` (`grok-4.1-fast`, `max_tokens` 300, `requests` only). Missing key or any failure uses a templated fallback from the same pillar notes — the UI never crashes.

The Streamlit app caches explanations **24 hours per symbol** in a process-local dict. Footnote on every blurb: **generated by AI, not financial advice**.

Set `XAI_API_KEY` in `.env` locally or in the **Render dashboard** (`sync: false` in `render.yaml`, same pattern as `CMC_API_KEY`). Never commit the key.

### Shareable score card (X)

Each compare card has a **Share score card** button. It does **not** run on page load — only on an explicit click. The click builds a signed, timestamped PNG (ticker, score, band, all six pillar bars including **basis**, RAT branding) and offers a download. If X credentials are configured it also posts that image to X via API v2. If they are missing, the PNG still downloads and the UI says the X post was skipped. Generation or X failures never crash the demo.

**Signing.** `SCORE_CARD_SIGNING_SECRET` HMAC-SHA256-signs canonical JSON of `{ticker, score, band, subscores, timestamp}`. The first 16 hex characters (the fingerprint) are printed on the image and in the caption so others can verify. No secret → card is labeled `UNSIGNED`. Never commit the secret. Store it in the **Render dashboard** (`sync: false`) or **Bitwarden**.

**X credentials** (OAuth 1.0a user context; all four required to post):

| Variable | Also accepted |
|---|---|
| `X_API_KEY` | `TWITTER_API_KEY`, `TWITTER_CONSUMER_KEY` |
| `X_API_SECRET` | `TWITTER_API_SECRET`, `TWITTER_CONSUMER_SECRET` |
| `X_ACCESS_TOKEN` | `TWITTER_ACCESS_TOKEN` |
| `X_ACCESS_TOKEN_SECRET` | `TWITTER_ACCESS_TOKEN_SECRET` |

Set them in the Render dashboard (`sync: false`) or Bitwarden. Never commit them. Posting uses the app account — click Share only when you intend to publish. This is a user-triggered share, not a hunter or auto-poster.

Caption includes the fingerprint and **Not financial advice**. MIT core stays open.

### Heuristic fallback disclaimer

If a live verifier errors, times out, or is skipped (fixture/offline mode), the pillar **falls back to issuer-name heuristics** and is explicitly labeled **heuristic fallback**. The same label applies when a Backed / xStocks ticker has **no published Chainlink PoR feed** (most xStocks symbols) — that is expected coverage, not a failed probe. Failed verifiers are never dropped silently — the error is recorded in notes/flags. Heuristics are **not** audited attestations.

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
app.py                 Streamlit demo (search, pillars, verification badges, compare, AI explainer, share card)
rwa_score/client.py    Live CMC client + FixtureClient + create_client()
rwa_score/scorer.py    Weighted pillars, bands, verification levels, no silent fails
rwa_score/chainlink_por.py  Chainlink AggregatorV3 PoR reader (JSON-RPC eth_call, requests only)
rwa_score/verifiers.py Backed Chainlink PoR + Dinari scrapers + Robinhood debt-wrapper scores
rwa_score/explainer.py xAI Grok “Why this score?” with templated fallback
rwa_score/score_card.py Signed timestamped PNG (Pillow) + HMAC-SHA256 fingerprint
rwa_score/x_client.py  X API v2 media + tweet (OAuth 1.0a); skip if credentials missing
rwa_score/issuer_registry.py   Name-match heuristics + ISSUER_NOTES (equity vs debt)
rwa_score/fixtures/    Demo JSON shaped like CMC RWA responses
rwa_score/api/         Paid REST output layer (keys, quotas, history, webhooks, score hash)
contracts/             ScoreAttestation.sol — Base Sepolia hash attestation (Foundry)
scripts/verify_attestation.py   Re-hash a live score and optionally read the chain
```

Live data flow (CMC Basic):

1. `GET /v5/real-world-assets/map` — ticker → `rwa_id` (0 credits)
2. `GET /v5/real-world-assets/info` — issuer metadata + **SEC CIK**
3. `GET /v5/real-world-assets/issuers/list` then `/issuers` — who mints the token, on-chain `crypto_id` (cached for the process lifetime)
4. `GET /v2/cryptocurrency/quotes/latest` — token 24h change for price integrity
5. `GET /v5/real-world-assets/market-pairs/list` — wrapper venues/prices for the **cross-issuer basis** pillar (short TTL cache; first page, `limit=100`)

## Paid API (verdict + history + attestation)

The scoring engine, verifiers, and fixtures stay **MIT-open**. What you pay for is authenticated access, score history, webhooks, and an optional on-chain **hash** of the breakdown (never the raw score on-chain).

| Tier | Price | Quota | Includes |
|---|---|---|---|
| Free | $0 | 50 calls / rolling 24h (sliding window) | Current score, compare, watchlist |
| Paid | $20–$50 / mo | Unlimited | History, webhooks, attestation hash |

### Get an API key

Self-host (prints the secret once; the DB stores only a SHA-256 hash):

```bash
python -m rwa_score.api.keys create --name "my-app" --tier free
python -m rwa_score.api.keys create --name "desk" --tier paid
```

Hosted keys: open a GitHub issue on this repo or contact [@SAW72](https://github.com/SAW72). Send the key as `X-API-Key` or `Authorization: Bearer`. Never commit it.

Optional bootstrap on process start (env only): `RWA_API_BOOTSTRAP_KEY` + `RWA_API_BOOTSTRAP_TIER=paid`.

### Run the API

Binds `0.0.0.0:$PORT` (default `8000`). Reuses `TransparencyScorer` / `create_client` so numbers match the Streamlit UI byte-for-byte on the breakdown.

```bash
pip install -r requirements.txt
python -m rwa_score.api.keys create --name local --tier paid
RWA_USE_FIXTURES=1 python -m rwa_score.api
curl -sS -H "X-API-Key: $KEY" http://127.0.0.1:8000/v1/score/NVDA
```

| Method | Path | Who |
|---|---|---|
| GET | `/v1/score/{ticker}` | free + paid |
| GET | `/v1/compare?tickers=a,b,c` | free + paid |
| GET / PUT / POST / DELETE | `/v1/watchlist` | free + paid |
| GET | `/v1/history/{ticker}` | paid |
| POST / GET / DELETE | `/v1/webhooks` | paid |
| GET | `/v1/attest/{ticker}` | paid |
| GET | `/v1/me` | free + paid |
| GET | `/health` | open |

### Webhooks

`POST /v1/webhooks` with `{"url": "https://…", "trigger": "band_cross"}` or `"below_orange"` (new band is RED). URLs must be **https** to a public host — localhost, RFC1918, link-local, and `169.254.169.254` are rejected.

**v1 delivery:** synchronous HTTP POST in the **same scoring cycle** as the request that observed the crossing (`GET /v1/score`, compare, watchlist). First observation of a ticker is stored and does not fire. The same check runs for every saved watchlist ticker when you run `python -m rwa_score.api.poll` (cron / background worker). Body is HMAC-SHA256 signed (`X-RAT-Signature: sha256=…`) with the webhook secret.

### On-chain attestation (Base Sepolia)

`GET /v1/attest/{ticker}` returns `score_hash` (SHA-256 of the canonical six-pillar breakdown, including **basis**). Submit with `attest(scoreHash, ticker, timestamp)` — attester is `msg.sender`, not calldata. See [`contracts/README.md`](contracts/README.md). Deploy scripts **revert on any chain except Base Sepolia (84532)**. Mainnet is held.

```bash
RWA_USE_FIXTURES=1 python scripts/verify_attestation.py NVDA --fixtures
```

Pass `--contract` and `--rpc-url` (or `RWA_ATTESTATION_CONTRACT` / `BASE_SEPOLIA_RPC_URL`) to read the chain. The client never needs a private key.

SQLite (`RWA_API_DB_PATH`, default `data/rat_api.sqlite`) is v1. Render’s filesystem is ephemeral — use a disk or Postgres before relying on keys in production.

## Tests

```bash
pytest -q
cd contracts && forge install foundry-rs/forge-std --no-commit && forge test -vv
```

CI runs both. No real API key or wallet is required.

## Deploy

### `RWA_USE_FIXTURES`

| Value | Mode |
|---|---|
| `0` | Live path: CMC (needs `CMC_API_KEY`) plus Chainlink PoR verifiers |
| `1` | Offline demo fixtures only — no live CMC, no live attestation |

**Render must stay at `RWA_USE_FIXTURES=0`.** That default lives in `render.yaml`. Flipping the variable in the Render dashboard alone is **not** enough: the next blueprint sync / redeploy re-applies `render.yaml` and overwrites the dashboard value.

`CMC_API_KEY`, `XAI_API_KEY`, `SCORE_CARD_SIGNING_SECRET`, and the four `X_*` keys stay `sync: false`. Set them in the Render dashboard (or Bitwarden) — never commit the keys. The explainer falls back to a template when `XAI_API_KEY` is unset. Share still downloads a PNG when the signing secret or X tokens are unset.

Optional RPC overrides (no keys): `POLYGON_RPC_URL`, `BASE_RPC_URL`, `ETH_RPC_URL`. When unset, the verifier uses public no-key endpoints (Polygon `publicnode` / `polygon-rpc.com`, Base `mainnet.base.org`, Ethereum `cloudflare-eth.com`). Paid or key-gated RPC URLs must stay in the dashboard — never commit them.

**Render (blueprint):** `render.yaml` starts via `python -m rwa_score.health` on `0.0.0.0:$PORT` so `GET /health` is registered before Streamlit's SPA catch-all.

```text
New Web Service → this repo → Build: pip install -r requirements.txt
Start: python -m rwa_score.health --server.port $PORT --server.address 0.0.0.0 --server.headless true
Env: RWA_USE_FIXTURES=0   (keep this; do not set 1 on Render)
Dashboard secrets: CMC_API_KEY, XAI_API_KEY (optional; templated fallback if unset)
Optional RPC: POLYGON_RPC_URL, BASE_RPC_URL, ETH_RPC_URL (public fallbacks if unset)
Optional share: SCORE_CARD_SIGNING_SECRET, X_API_KEY, X_API_SECRET,
X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET (PNG still downloads if unset)
```

Confirm live mode (no fixture overwrite, verifiers enabled) with:

```bash
curl -sS https://rwa-transparency-score.onrender.com/health
```

Expected when Render is live:

```json
{"fixtures": false, "verifiers_live": true, "backed_feed": "ok", "timestamp": "2026-09-10T01:13:00Z"}
```

`backed_feed` is `"ok"` if the canonical Backed Chainlink PoR feed (bIB01 **bToken** on Polygon) answers `latestRoundData` over JSON-RPC, otherwise `"down"`. This probe does **not** mean every scored ticker has an oracle feed — xStocks symbols without a published proxy still score via **heuristic fallback**. The handler always returns JSON — a down feed does not crash `/health` and does not change the health JSON shape. Local demos can still run `RWA_USE_FIXTURES=1 streamlit run app.py`. For a process-start `/health` route locally, use the same launcher as Render: `python -m rwa_score.health`.

**Streamlit Community Cloud:** deploy `app.py` from the repo root. Secrets: leave `CMC_API_KEY` empty and set `RWA_USE_FIXTURES=1`, or add a key and set `RWA_USE_FIXTURES=0` for live mode.

This repo is **deploy-config ready**. A public URL appears only after you connect the repo to Render or Streamlit Cloud.

## Disclaimer

Informational and educational hackathon demo only. Not financial, investment, legal, or tax advice. Not an offer, solicitation, or recommendation to buy, sell, or hold any security, digital asset, tokenized stock, or other instrument. Scores are automated heuristics (including issuer-name matching) plus third-party CoinMarketCap data or bundled demo fixtures — not audited attestations, not legal or audit opinions, and not a substitute for issuer filings, prospectuses, offering documents, or your own independent research. Data may be incomplete, delayed, inaccurate, or outdated. Nothing here guarantees accuracy, completeness, or fitness for any purpose. Past or present scores are not indicative of future results. This demo is not provided by a broker-dealer, exchange, ATS, funding portal, or registered investment adviser, and it does not create any advisory or fiduciary relationship. Do your own research. Use at your own risk.

## License

MIT
