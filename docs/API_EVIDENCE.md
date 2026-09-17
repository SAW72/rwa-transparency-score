# GET /v1/score/{ticker} evidence

**Source label: fixture-backed — not live CoinMarketCap.**

This body was captured from the paid RAT Score API running locally:

```text
RWA_USE_FIXTURES=1 python -m rwa_score.api
GET /v1/score/NVDA
```

The JSON schema is the authentic paid-API envelope (breakdown keys plus `confidence` and `attestation`). `data_source` is `"fixture"` because bundled demo fixtures were used. A live CMC run uses the same shape and sets `data_source` to `"live"`.

Machine-readable copy: [`examples/v1_score_NVDA.fixture.json`](examples/v1_score_NVDA.fixture.json).

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

```http
HTTP/1.1 200 OK
content-type: application/json
```

```json
{
  "ticker": "NVDA",
  "rwa_id": 2,
  "issuer": "Backed Finance",
  "score": 90.2,
  "band": "GREEN",
  "band_label": "GREEN — heuristic: stronger transparency signals (still verify)",
  "subscores": {
    "backing": 90.0,
    "reserves": 90.0,
    "redemption": 85.0,
    "price": 98.4,
    "disclosure": 80.0,
    "basis": 97.9
  },
  "weights": {
    "backing": 0.2,
    "reserves": 0.2,
    "redemption": 0.15,
    "price": 0.15,
    "disclosure": 0.15,
    "basis": 0.15
  },
  "pillars": {
    "backing": {
      "label": "Backing model",
      "what": "Real shares with a regulated custodian vs. a thin debt note."
    },
    "reserves": {
      "label": "Proof of reserves",
      "what": "Independent auditor publishing on-chain (e.g. Chainlink) vs. a promise."
    },
    "redemption": {
      "label": "Redemption rights",
      "what": "Redeemable for the underlying share vs. sell-only."
    },
    "price": {
      "label": "Price integrity",
      "what": "On-chain token tracks the stock without wild 24h drift."
    },
    "disclosure": {
      "label": "Disclosure",
      "what": "Matchable SEC CIK on the RWA info record vs. missing."
    },
    "basis": {
      "label": "Cross-issuer basis",
      "what": "Same underlying ticker, different wrapper prices — spread is wrapper risk."
    }
  },
  "explanations": {
    "backing": "Heuristic: issuer 'Backed Finance' matched the fully-backed name list. Verification: self-reported. Evidence: heuristic fallback: issuer 'Backed Finance' matched the backing name list. Fixture/offline mode — live attestation verifiers skipped. heuristic fallback Issuer backing / proof-of-reserves / redemption flags are name-matching heuristics, not audited attestations.",
    "reserves": "Heuristic: issuer 'Backed Finance' matched the independent-PoR name list. Verification: self-reported. Evidence: heuristic fallback: issuer 'Backed Finance' matched the reserves name list. Fixture/offline mode — live attestation verifiers skipped. heuristic fallback Issuer backing / proof-of-reserves / redemption flags are name-matching heuristics, not audited attestations.",
    "redemption": "Heuristic: issuer 'Backed Finance' matched the redeemable name list. Verification: self-reported. Evidence: heuristic fallback: issuer 'Backed Finance' redemption rights via name list (Fixture/offline mode — live redemption verifier skipped). heuristic fallback Fixture/offline mode — live redemption verifier skipped. Issuer backing / proof-of-reserves / redemption flags are name-matching heuristics, not audited attestations.",
    "price": "24h change +0.80%; score = max(20, 100 − |Δ| × 2) = 98.4. Verification: self-reported. Evidence: CMC crypto quote crypto_id=36992; 24hΔ=0.8 verification=self-reported",
    "disclosure": "SEC CIK 0001045810 present on the RWA info record. Verification: self-reported. Evidence: SEC CIK 0001045810 on CMC RWA info record. verification=self-reported",
    "basis": "3 wrappers; spread 0.21% (NVDAx 118.4500 vs NVDAxst 118.7000); score = max(15, 100 − |spread| × 10) = 97.9. Verification: self-reported. Evidence: CMC market-pairs: 3 wrappers; spread 0.21% (low 118.45, high 118.7). verification=self-reported"
  },
  "verification": {
    "backing": {
      "level": "self-reported",
      "evidence": "heuristic fallback: issuer 'Backed Finance' matched the backing name list. Fixture/offline mode — live attestation verifiers skipped.",
      "source": "heuristic_fallback",
      "notes": [
        "heuristic fallback",
        "Issuer backing / proof-of-reserves / redemption flags are name-matching heuristics, not audited attestations."
      ],
      "ok": true,
      "error": null,
      "meta": {
        "pillar": "backing",
        "matched": true,
        "issuer": "Backed Finance"
      },
      "score": 90.0
    },
    "reserves": {
      "level": "self-reported",
      "evidence": "heuristic fallback: issuer 'Backed Finance' matched the reserves name list. Fixture/offline mode — live attestation verifiers skipped.",
      "source": "heuristic_fallback",
      "notes": [
        "heuristic fallback",
        "Issuer backing / proof-of-reserves / redemption flags are name-matching heuristics, not audited attestations."
      ],
      "ok": true,
      "error": null,
      "meta": {
        "pillar": "reserves",
        "matched": true,
        "issuer": "Backed Finance"
      },
      "score": 90.0
    },
    "redemption": {
      "level": "self-reported",
      "evidence": "heuristic fallback: issuer 'Backed Finance' redemption rights via name list (Fixture/offline mode — live redemption verifier skipped).",
      "source": "heuristic_fallback",
      "notes": [
        "heuristic fallback",
        "Fixture/offline mode — live redemption verifier skipped.",
        "Issuer backing / proof-of-reserves / redemption flags are name-matching heuristics, not audited attestations."
      ],
      "ok": true,
      "error": null,
      "meta": {
        "pillar": "redemption",
        "matched": true,
        "issuer": "Backed Finance"
      },
      "score": 85.0
    },
    "price": {
      "level": "self-reported",
      "evidence": "CMC crypto quote crypto_id=36992; 24hΔ=0.8",
      "source": "cmc_quote",
      "notes": [
        "verification=self-reported"
      ],
      "ok": true,
      "error": null,
      "meta": {},
      "score": 98.4
    },
    "disclosure": {
      "level": "self-reported",
      "evidence": "SEC CIK 0001045810 on CMC RWA info record.",
      "source": "cmc_rwa_info",
      "notes": [
        "verification=self-reported"
      ],
      "ok": true,
      "error": null,
      "meta": {},
      "score": 80.0
    },
    "basis": {
      "level": "self-reported",
      "evidence": "CMC market-pairs: 3 wrappers; spread 0.21% (low 118.45, high 118.7).",
      "source": "cmc_market_pairs",
      "notes": [
        "verification=self-reported"
      ],
      "ok": true,
      "error": null,
      "meta": {},
      "score": 97.9
    }
  },
  "flags": [],
  "notes": [
    "Issuer backing / proof-of-reserves / redemption flags are name-matching heuristics, not audited attestations.",
    "Scores below use bundled DEMO FIXTURE data, not live CoinMarketCap API responses.",
    "Live attestation verifiers skipped (fixture/offline); backing/reserves use heuristic fallback.",
    "heuristic fallback",
    "Fixture/offline mode — live redemption verifier skipped."
  ],
  "heuristics": {
    "backed": true,
    "audited": true,
    "redeemable": true,
    "source": "issuer_registry",
    "labeled": true
  },
  "data_source": "fixture",
  "price": {
    "available": true,
    "crypto_id": 36992,
    "percent_change_24h": 0.8,
    "price": 118.45,
    "volume_24h": 4200000.0
  },
  "basis": {
    "available": true,
    "wrapper_count": 3,
    "percent_spread": 0.2108370229812355,
    "min_price": 118.45,
    "max_price": 118.7,
    "wrappers": [
      {
        "crypto_id": 36992,
        "symbol": "NVDAx",
        "price": 118.45,
        "volume_24h": 4200000.0,
        "venues": 1,
        "issuer": "Backed Finance"
      },
      {
        "crypto_id": 37001,
        "symbol": "NVDAon",
        "price": 118.55,
        "volume_24h": 1900000.0,
        "venues": 1,
        "issuer": ""
      },
      {
        "crypto_id": 37002,
        "symbol": "NVDAxst",
        "price": 118.7,
        "volume_24h": 1500000.0,
        "venues": 1,
        "issuer": ""
      }
    ]
  },
  "cik": "0001045810",
  "issuer_note": "Equity-backed token (Backed Finance / xStocks): claims 1:1 share custody with a public on-chain proof of reserves.",
  "summary": "Backed Finance — 0 risk flag(s).",
  "verification_mode": "offline_heuristic",
  "live_verifiers": false,
  "confidence": {
    "score": 0.4,
    "label": "low"
  },
  "attestation": {
    "score_hash": "0x0060941adfb0dc745e24dde266180cb127afbe6cdfb63513f492cc703f213953",
    "algo": "sha256",
    "fields": [
      "ticker",
      "rwa_id",
      "issuer",
      "score",
      "band",
      "subscores",
      "weights",
      "cik",
      "data_source",
      "verification",
      "basis"
    ]
  }
}
```

`confidence.label` is `low` here because fixture/offline mode skips live attestation verifiers (heuristic fallback on backing / reserves / redemption). The score band is still GREEN from the fixture pillars.

The product Disclaimer on the README still applies. This sample is demo-fixture data, not an audited attestation.
