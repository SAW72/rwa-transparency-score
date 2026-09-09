# RAT Score

RAT Score (RWA Transparency Score) — an AI-assisted risk radar for tokenized stocks. Rates issuers 0–100 on backing, proof of reserves, redemption, price integrity, and disclosure using CoinMarketCap’s RWA API.

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

This tool rates a tokenized stock on how honest its issuer looks, using CoinMarketCap's RWA API (or bundled demo fixtures when you do not have a key).

## The score (0–100)

| Pillar | What it checks | Weight |
|---|---|---|
| Backing model | Real shares with a regulated custodian vs. a thin debt note | 25% |
| Proof of reserves | Independent auditor publishing on-chain (e.g. Chainlink) vs. a promise | 25% |
| Redemption rights | Redeemable for the underlying share vs. sell-only | 20% |
| Price integrity | Token 24h drift stays contained | 15% |
| Disclosure | Real, matchable SEC CIK vs. missing | 15% |

Bands: **GREEN** ≥ 75 · **YELLOW** ≥ 50 · **ORANGE** ≥ 25 · **RED** below 25.

Backing / reserves / redemption use **issuer-name heuristics** (see `rwa_score/issuer_registry.py`). The UI and CLI label them as heuristics — they are not audited attestations.

## How to run

### Offline fixtures (recommended for judging)

No `CMC_API_KEY`. No live credits. Clearly labeled demo data in `rwa_score/fixtures/demo_cmc.json`.

```bash
pip install -r requirements.txt
RWA_USE_FIXTURES=1 streamlit run app.py
# or
python -m rwa_score --fixtures NVDA TSLA AAPL
```

Fixture catalog: `NVDA` (solid / Backed Finance), `AAPL` (mid / xStocks), `TSLA` (thin wrapper), `META` (Ondo).

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
app.py                 Streamlit demo (search, pillars, flags, compare)
rwa_score/client.py    Live CMC client + FixtureClient + create_client()
rwa_score/scorer.py    Weighted pillars, bands, explanations, no silent fails
rwa_score/issuer_registry.py   Name-match heuristics (labeled as such)
rwa_score/fixtures/    Demo JSON shaped like CMC RWA responses
```

Live data flow (CMC Basic):

1. `GET /v5/real-world-assets/map` — ticker → `rwa_id` (0 credits)
2. `GET /v5/real-world-assets/info` — issuer metadata + **SEC CIK**
3. `GET /v5/real-world-assets/issuers/list` then `/issuers` — who mints the token, on-chain `crypto_id` (cached for the process lifetime)
4. `GET /v2/cryptocurrency/quotes/latest` — token 24h change for price integrity

## Tests

```bash
pytest -q
```

CI runs the same command. No real API key is required.

## Deploy

**Render (blueprint):** `render.yaml` starts Streamlit on `0.0.0.0:$PORT` with `RWA_USE_FIXTURES=1`. `CMC_API_KEY` is optional and must be set in the dashboard if you want live mode — never commit it.

```text
New Web Service → this repo → Build: pip install -r requirements.txt
Start: streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true
Env: RWA_USE_FIXTURES=1
```

**Streamlit Community Cloud:** deploy `app.py` from the repo root. Secrets: leave `CMC_API_KEY` empty and set `RWA_USE_FIXTURES=1`, or add a key for live mode.

This repo is **deploy-config ready**. A public URL appears only after you connect the repo to Render or Streamlit Cloud.

## Disclaimer

This tool is for informational and hackathon demo purposes only. It does **not** constitute financial advice. Always do your own research.

## License

MIT
