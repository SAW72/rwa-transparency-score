# RWA Transparency Score

An AI-assisted risk radar for **tokenized stocks**. Built for the [CoinMarketCap Build-a-thon](https://dorahacks.io/hackathon/coinmarketcap-api-202609/detail) on DoraHacks — **Real World Assets** track.

Score any tokenized equity **0–100** on five pillars: backing, proof of reserves, redemption rights, price integrity, and SEC disclosure. The live path is the CMC RWA API. The demo path is **fixture / offline mode**, so judges can click around with zero setup.

> Informational only. Not financial advice.

## 30-second judge path (no API key)

```bash
git clone https://github.com/SAW72/rwa-transparency-score.git
cd rwa-transparency-score
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Web demo — canned CMC responses, no CMC_API_KEY
streamlit run app.py

# Same scores in the terminal
python -m rwa_score NVDA TSLA AAPL --fixtures
pytest -q
```

You should see three contrasting names:

| Ticker | Fixture issuer | What judges should notice |
|---|---|---|
| **NVDA** | Backed Assets | Green. Custodied + audited + redeemable + CIK + calm 24h. |
| **TSLA** | xStocks | Yellow. Backed and redeemable, but no independent PoR. |
| **AAPL** | ThinWrap Labs | Orange. Sell-only wrapper, no CIK, 37% 24h drift. |

## The score (0–100)

| Pillar | What it checks | Weight | CMC field |
|---|---|---|---|
| Backing model | Real shares with a regulated custodian vs. a thin debt note | 25 | Issuer name from `/issuers` → [registry](rwa_score/issuer_registry.py) |
| Proof of reserves | Independent auditor publishing on-chain (e.g. Chainlink) vs. a promise | 25 | Same registry (`audited`) |
| Redemption rights | Redeemable for the underlying share vs. sell-only | 20 | Same registry (`redeemable`) |
| Price integrity | Token tracks the stock — wild 24h swings are penalized | 15 | `percent_change_24h` on `/v2/cryptocurrency/quotes/latest` |
| Disclosure | Real, matchable SEC CIK vs. missing | 15 | `cik` on `/v5/real-world-assets/info` |

Bands: **75+ green** · **50–74 yellow** · **25–49 orange** · **<25 red**.

CMC does **not** publish custody, audit, or redemption flags. Those three pillars are a documented allow-list on the issuer name. That is called out in the UI and below under [API feedback](#api-feedback-for-cmc).

## Data flow (CMC Basic / Startup)

```
ticker
  → GET /v5/real-world-assets/map?symbol=NVDA          # rwa_id  (0 credits)
  → GET /v5/real-world-assets/info?rwa_id=…            # CIK, exchange, about  (1 / 250)
  → GET /v5/real-world-assets/issuers/list             # issuer directory
  → GET /v5/real-world-assets/issuers?issuer_id=…      # token roster + crypto_id
  → GET /v2/cryptocurrency/quotes/latest?id=<crypto_id>&convert=USD
```

Evidence of a real call (shape, not a secret): see [`rwa_score/data/bundle.json`](rwa_score/data/bundle.json) and [`rwa_score/data/README.md`](rwa_score/data/README.md). Live mode uses the same parse path; pytest mocks `requests.Session.get`.

## Run with fixtures (default)

Fixture mode is automatic when `CMC_API_KEY` is unset, or when `USE_FIXTURES=1`.

```bash
# Web
USE_FIXTURES=1 streamlit run app.py

# CLI
python -m rwa_score NVDA TSLA AAPL --fixtures
python -m rwa_score.demo
python -m rwa_score NVDA --json --fixtures
```

No `.env` required. No outbound HTTP.

## Run against live CMC

1. Create a key at [coinmarketcap.com/api](https://coinmarketcap.com/api/). Hackathon participants get a Startup grant via the DoraHacks form — use the **same email** as your CMC account.
2. Copy the example env file. **Never commit `.env`.**

```bash
cp .env.example .env
# edit .env and set CMC_API_KEY=… and USE_FIXTURES=0
python -m rwa_score NVDA --live
streamlit run app.py
# then pick "Live CoinMarketCap API" in the sidebar
```

The key is read from the environment or Streamlit secrets. It is never rendered in the UI and is not written to logs.

## Tests

```bash
pytest -q
```

Coverage: issuer-name matching (including the `Backpack` ≠ `backed` trap), scorer weights / bands / flags, fixture demo scores, and a mocked live client that asserts `rwa_info` sends `rwa_id` (not `id`).

## Deploy

### Render

[`render.yaml`](render.yaml) is a Blueprint for one Python web service.

- **Build:** `pip install -r requirements.txt`
- **Start:** `streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true`
- **Default env:** `USE_FIXTURES=1` so the public demo works without a key
- **Optional secret:** set `CMC_API_KEY` in the Render dashboard (`sync: false` in the Blueprint — you will be prompted; the value is never stored in git)

New Blueprint: Render Dashboard → **New +** → **Blueprint** → this repo. Free web services spin down after 15 minutes of idle time; the first request after that is a cold start.

### Streamlit Community Cloud

1. [share.streamlit.io](https://share.streamlit.io) → **New app** → this repo, branch `main`, main file `app.py`.
2. Leave secrets empty to stay on fixtures.
3. To enable live CMC: **App settings → Secrets**

```toml
CMC_API_KEY = "your_key_here"
USE_FIXTURES = "0"
```

`runtime.txt` pins Python 3.12. Do not upload `.streamlit/secrets.toml` — it is gitignored.

## Project layout

```
app.py                  Streamlit UI
render.yaml             Render Blueprint
rwa_score/client.py     Live CMC client + create_client()
rwa_score/fixtures.py   Offline client
rwa_score/data/         Canned CMC envelopes
rwa_score/scorer.py     Weighted 0–100 model
rwa_score/issuer_registry.py
tests/                  pytest + mocks
```

## API feedback for CMC

What the RWA family made possible: a single `rwa_id` that is stable across issuers, company metadata (including CIK) split from market data, and a token roster that bridges to the existing quotes API.

Where it got in the way:

- Issuer objects are **name + website + tokens**. There is no custody, auditor, proof-of-reserves, or redemption field, so a transparency product has to maintain its own registry.
- `/info` `cik` is the **underlying company**, not the token issuer. A thin wrapper of AAPL still inherits Apple's CIK if CMC fills it in — the fixture set drops it on AAPL so the disclosure pillar is visible.
- `/quotes/latest` `tokens[]` often has `null` for `issuer_name` and `price`. We walk `/issuers` and then `/v2/cryptocurrency/quotes/latest` instead.
- The info query param is `rwa_id`, not `id`. Easy to miss if you copied crypto-info habits.

## Hackathon submission notes

- **Track:** Real World Assets
- **Endpoints used:** listed above, named explicitly
- **Working demo:** `streamlit run app.py` (fixtures) or the Render / Streamlit Cloud URL
- **Do not commit keys.** `.gitignore` blocks `.env` and `.streamlit/secrets.toml`. Committing a key counts against code quality per the [hackathon rules](https://dorahacks.io/hackathon/coinmarketcap-api-202609/detail).

## License

MIT
