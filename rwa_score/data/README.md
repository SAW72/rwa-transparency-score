# Offline CMC fixtures

`bundle.json` is a canned subset of CoinMarketCap Pro API envelopes:

| Key | Live endpoint |
|---|---|
| `rwa_map` | `GET /v5/real-world-assets/map` |
| `rwa_info` | `GET /v5/real-world-assets/info` |
| `issuers_list` | `GET /v5/real-world-assets/issuers/list` |
| `issuers` | `GET /v5/real-world-assets/issuers` |
| `quotes` | `GET /v2/cryptocurrency/quotes/latest` |

NVDA / TSLA / AAPL are chosen so the demo shows **green / yellow / orange** without an API key. AAPL's CIK is intentionally omitted and its issuer is a fictional thin wrapper so the disclosure and backing pillars fail in a visible way.
