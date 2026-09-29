# Score history and attestation bytes

Payloads, scoring inputs, score history, and the attest job queue live in the API process memory. A restart or a free-plan spin-down drops them. That loss is accepted.

`RWA_STORE_MAX_ENTRIES` caps how many history rows, payloads, jobs, usage events, and deliveries the process keeps. The default is 1000. The code hard-max is 10000. In-flight attest jobs are not evicted to make room.

`GET /v1/attest/{ticker}` and `GET /v1/attest/{ticker}/status` return `canonical_payload`: the exact canonical bytes and scoring inputs as base64. Save that object and check it later with:

```bash
python -m rwa_score.api.verify NVDA --payload-file nvda.json --offline
```

The digest is SHA-256 of those canonical bytes, the same `bytes32` the contract stores.

`/health` reports process status, whether the attester is enabled, the configured chain id, RPC reachability, attester balance against the floor, and queue depth. It does not probe a database.

Future (out of scope): persistent DB only if we go mainnet or partner with a data provider like CoinMarketCap (API signups).
