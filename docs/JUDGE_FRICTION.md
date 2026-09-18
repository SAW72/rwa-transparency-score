# Judge friction (CMC DoraHacks)

Short path so judging does not stall on CoinMarketCap rate limits or a Free Render cold start. **No Submit.** The hosted paid API is **not** deployed (spend hold).

## Fastest path: offline fixtures (no CMC key)

```bash
pip install -r requirements.txt
RWA_USE_FIXTURES=1 streamlit run app.py
```

CLI: `python -m rwa_score --fixtures NVDA TSLA AAPL`

Paid REST schema (self-host only — not on Render):

```bash
python -m rwa_score.api.keys create --name local --tier paid
RWA_USE_FIXTURES=1 python -m rwa_score.api
curl -sS -H "X-API-Key: <REDACTED>" http://127.0.0.1:8000/v1/score/NVDA
```

Captured fixture-backed request + response: [`API_EVIDENCE.md`](API_EVIDENCE.md) / [`examples/v1_score_NVDA.fixture.json`](examples/v1_score_NVDA.fixture.json). That sample is **fixture-backed**, not live CMC (`data_source: "fixture"`).

## CMC Basic rate limits

CMC **Basic** keys allow only a few HTTP requests per minute.

| Signal | Meaning |
|---|---|
| `HTTP 429` | HTTP request rate limit |
| `error_code` **1008** | Same cap, sometimes on HTTP 200 |
| Message | *You've exceeded your API Key's HTTP request rate limit. Rate limits reset every minute.* |

**What to do:** wait about **one minute** and retry. The live client already retries 429 / 1008 with exponential backoff and jitter (honors `Retry-After`, waits ~60s total), then raises a clear error.

[DoraHacks Startup](https://coinmarketcap.com/api/) unlocks a higher request rate (and more credits) than Basic. Prefer fixtures for judging so you never hit the cap.

## Live Streamlit demo (not the paid API)

- URL: [https://rwa-transparency-score.onrender.com](https://rwa-transparency-score.onrender.com)
- Liveness: `GET /health` → [https://rwa-transparency-score.onrender.com/health](https://rwa-transparency-score.onrender.com/health)

```bash
curl -sS https://rwa-transparency-score.onrender.com/health
```

Expected when Render is live (not fixtures):

```json
{"fixtures": false, "verifiers_live": true, "backed_feed": "ok", "timestamp": "…"}
```

**Free Render cold starts:** the free web service can spin down after ~15 minutes of inactivity. The first request after idle may take ~30–60 seconds. Wait and retry `/health` — do not treat a slow first hit as a failed demo. Expected body is JSON (`fixtures`, `verifiers_live`, `backed_feed`), not Streamlit's "enable JavaScript" SPA. If `/health` is `text/html`, the Render **Start Command** is `streamlit run app.py` and must be changed to match `render.yaml` (`python -m rwa_score.health …`). `/_stcore/health` = `ok` is Streamlit's probe, not this payload.

Render hosts the **Streamlit** UI + `/health` (and `/privacy`, `/terms`). It does **not** host `GET /v1/score/{ticker}`. Do not curl `/v1/score/NVDA` on the Render hostname. Self-host the paid API locally with `RWA_USE_FIXTURES=1` to inspect that schema.

## Offline fixtures for judging

| | |
|---|---|
| Env | `RWA_USE_FIXTURES=1` |
| Data | Bundled `rwa_score/fixtures/demo_cmc.json` |
| Credits | None — no `CMC_API_KEY`, no live CMC |
| Label | UI / CLI / API set `data_source` to `fixture` |

`RWA_USE_FIXTURES=0` is live CMC + Chainlink PoR verifiers (needs a key). **Do not flip Render to fixtures** — `render.yaml` keeps the hosted demo on `0`.

## Do not expect

- A hosted paid API or keep-alive (spend hold)
- Every ticker to hit Chainlink PoR (Backed bTokens on Polygon only — search `bNV` / `bNVDA` for a card that can show the on-chain badge; most xStocks use **heuristic fallback**)
- Instant first load on Free Render after idle

Product disclaimer stays on the README. This page does not replace it.
