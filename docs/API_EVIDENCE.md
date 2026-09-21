# GET /v1/score/{ticker} evidence

**Source label: fixture-backed — not live CoinMarketCap.**

This body was captured from the paid RAT Score API running locally:

```text
RWA_USE_FIXTURES=1 python -m rwa_score.api
GET /v1/score/NVDA
```

The JSON schema is the authentic paid-API envelope (breakdown keys plus `confidence` and `attestation`). `data_source` is `"fixture"` because bundled demo fixtures were used. A live CMC run uses the same shape and sets `data_source` to `"live"`.

Machine-readable copy: [`examples/v1_score_NVDA.fixture.json`](examples/v1_score_NVDA.fixture.json).

Consumer mapping: [`NVDA_FIXTURE_CONSUMER_MAPPING.md`](NVDA_FIXTURE_CONSUMER_MAPPING.md)
shows how to preserve the fixture's provenance labels when handing fields to a
separate action-policy consumer. **Contributor:** YuTao Peng ([@imokokok](https://github.com/imokokok)) via PR [#52](https://github.com/SAW72/rwa-transparency-score/pull/52) — credit / attribution only.

Secrets, API keys, emails, and tokens are not included. Send the real key only at request time.

## Request

Exact HTTP shape (secret redacted):

```http
GET /v1/score/NVDA HTTP/1.1
Host: 127.0.0.1:8000
X-API-Key: <REDACTED>
```

Equivalent curl (self-host; paid API is not deployed on Render):

```bash
python -m rwa_score.api.keys create --name local --tier paid
# prints the raw key once — do not commit it
RWA_USE_FIXTURES=1 python -m rwa_score.api
curl -sS -H "X-API-Key: <REDACTED>" http://127.0.0.1:8000/v1/score/NVDA
```

`Authorization: Bearer <REDACTED>` is also accepted. Hosted demo URL is Streamlit-only — see [`JUDGE_FRICTION.md`](JUDGE_FRICTION.md).

## Response

Captured locally on 2026-09-15 with `RWA_USE_FIXTURES=1` and a paid key. **Fixture-backed, not live CMC.**

See the remainder of this file on `main` for the full captured JSON body (unchanged by the attribution line above).
