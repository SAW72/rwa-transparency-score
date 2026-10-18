# Judge friction (CoinGecko)

Short path so judging does not stall on CoinGecko rate limits or a Free Render cold start. **No Submit.** The hosted paid API is **not** deployed (spend hold).

## External contribution credit

**YuTao Peng** ([@imokokok](https://github.com/imokokok)) contributed the offline NVDA fixture consumer-mapping note (PR [#52](https://github.com/SAW72/rwa-transparency-score/pull/52)): [`NVDA_FIXTURE_CONSUMER_MAPPING.md`](NVDA_FIXTURE_CONSUMER_MAPPING.md), linked from [`API_EVIDENCE.md`](API_EVIDENCE.md). Docs-only — how a downstream policy tool preserves RAT provenance labels. Credit / label only; not a GitHub collaborator invite.

## Fastest path: offline fixtures (no market-data key)

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

## CoinGecko rate limits

Demo keys are published at 100 calls per minute and 10,000 calls per month. This app stays under that with a 30/minute cap and a 300/day process budget. Keyless access is stricter and shared. Prefer fixtures for judging.

| Signal | Meaning |
|---|---|
| `HTTP 429` | Rate limit. `Retry-After` is used only when it is digits or an HTTP-date |
| Other `Retry-After` values | Ignored. Backoff is 1s, then 2s, then 4s |
| Wait cap | At most 10 seconds of sleep, then cached data with an `as of` time |
| HTTP 5xx or network error | Same bounded retries, then keyless CMC quotes for price, market cap, and volume, then cache |

The page stays up. No key is printed. Venue names are **not available** (there is no market-pairs feed on this source).

## Live Streamlit demo (not the paid API)

- URL: [https://rwa-transparency-score.onrender.com](https://rwa-transparency-score.onrender.com)
- Liveness: `GET /health` → [https://rwa-transparency-score.onrender.com/health](https://rwa-transparency-score.onrender.com/health)

```bash
curl -sS https://rwa-transparency-score.onrender.com/health
```

Expected when Render is live (not fixtures):

```json
{"fixtures": false, "verifiers_live": true, "backed_feed": "ok", "timestamp": "…", "git_sha": "…", "share_ux": "scorecard-preview-first"}
```

**Free Render cold starts:** the free web service can spin down after ~15 minutes of inactivity. The first request after idle may take ~30–60 seconds. Wait and retry `/health` — do not treat a slow first hit as a failed demo. Expected body is JSON (`fixtures`, `verifiers_live`, `backed_feed`, `timestamp`, `git_sha`, `share_ux`), not Streamlit's "enable JavaScript" SPA. Header `X-RWA-Health: json` is set when the Tornado JSON handler ran. If `/health` is `text/html`, the Render **Start Command** is `streamlit run app.py` and must be changed to match `render.yaml` (`python -m rwa_score.health …`). The dashboard Start Command is independent of `render.yaml` unless the service is Blueprint-synced — this repo cannot change that dashboard field. `/_stcore/health` = `ok` is Streamlit's probe, not this payload.

**After Spencer approves a deploy** (do not merge-deploy from this PR):

```bash
curl -sS -D- https://rwa-transparency-score.onrender.com/health
# Expect: content-type application/json, X-RWA-Health: json
# Body keys: fixtures, verifiers_live, backed_feed, timestamp, git_sha, share_ux
# Live host: fixtures false, verifiers_live true
```

If the body is HTML, fix **Settings → Start Command** to the `render.yaml` launcher, then redeploy. Do not treat a green CI merge as that dashboard edit.

Render hosts the **Streamlit** UI + `/health` (and `/privacy`, `/terms`). It does **not** host `GET /v1/score/{ticker}`. Do not curl `/v1/score/NVDA` on the Render hostname. Self-host the paid API locally with `RWA_USE_FIXTURES=1` to inspect that schema.

## Offline fixtures for judging

| | |
|---|---|
| Env | `RWA_USE_FIXTURES=1` |
| Data | Bundled `rwa_score/fixtures/demo_cmc.json` |
| Credits | None — fixtures only, no live CoinGecko |
| Label | UI / CLI / API set `data_source` to `fixture` |

`RWA_USE_FIXTURES=0` is live CoinGecko + Chainlink PoR verifiers. **Do not flip Render to fixtures** — `render.yaml` keeps the hosted demo on `0`.

## Do not expect

- A hosted paid API or keep-alive (spend hold)
- Every ticker to hit Chainlink PoR (Backed bTokens on Polygon only — search `bNV` / `bNVDA` for a card that can show the on-chain badge; most xStocks use **heuristic fallback**)
- Instant first load on Free Render after idle

## Ask RAT (text only)

Collapsed **Ask RAT** under the compare table. Greeting: `Ask RAT — ask anything about a tokenized stock's risk score`. Chips prefill the three demo questions. No TTS. No `XAI_API_KEY` → templated tool-backed fallback (page stays up). Fixture answers say fixture — never live CMC. Each turn lists CMC calls.

Product disclaimer stays on the README. This page does not replace it.
