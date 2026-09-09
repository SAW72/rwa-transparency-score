# RWA Transparency Score

An AI-assisted risk radar for tokenized stocks, built for the CoinMarketCap Build-a-thon.

## The problem

Tokenized stocks (RWAs) can collapse the same way traditional stocks do — inflated claims, insider dumps, then sudden closure — except on-chain you get a warning light *before* the doors close, not after.

This tool rates every tokenized stock on how honest its issuer actually is, using CoinMarketCap's RWA API.

## The score (0–100)

| Pillar | What it checks | Weight |
|---|---|---|
| Backing model | Real shares with a regulated custodian vs. a thin debt note | 25 |
| Proof of reserves | Independent auditor publishing on-chain (e.g. Chainlink) vs. a promise | 25 |
| Redemption rights | Redeemable for the underlying share vs. sell-only | 20 |
| Price integrity | Token tracks the real stock's last close, no weekend drift | 15 |
| Disclosure | Real, matchable SEC CIK vs. missing | 15 |

## Data flow (CMC Basic plan, free)

1. `GET /v5/real-world-assets/map` — resolve ticker → `rwa_id` (0 credits)
2. `GET /v5/real-world-assets/info` — issuer metadata, founding date, employees, exchange, **SEC CIK** (1 credit)
3. `GET /v5/real-world-assets/issuers` — who mints the token, on-chain `crypto_id`
4. `GET /v2/cryptocurrency/quotes/latest` — live token price, volume, % change

## Quick start

```bash
cp .env.example .env   # add your CMC_API_KEY
pip install -r requirements.txt
python -m rwa_score NVDA TSLA AAPL
```

## Disclaimer

This tool is for informational purposes only and does **not** constitute financial advice. Always do your own research.

## License

MIT
