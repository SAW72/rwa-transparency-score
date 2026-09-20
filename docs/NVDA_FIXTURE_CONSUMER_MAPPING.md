# Consuming the published NVDA fixture without dropping evidence labels

This note shows how a downstream action-policy consumer can read the published
[`v1_score_NVDA.fixture.json`](examples/v1_score_NVDA.fixture.json) response while
preserving RAT Score's scope and provenance. The fixture in the same repository
commit is the only data source used here. No hosted API, live verifier, issuer
endpoint, or market-data service is called.

The two decisions remain separate:

- RAT Score summarizes transparency signals and labels how they were obtained.
- A downstream action policy decides whether its own separately supplied evidence
  is sufficient for a particular instrument, actor, action, and time.

A different downstream outcome is therefore not a RAT scoring error. It reflects a
different question and, potentially, a stricter consumer policy.

## Preservation rules

1. Carry `data_source`, `verification_mode`, `live_verifiers`, `confidence`, and the
   complete relevant `verification.<pillar>` object alongside every mapped value.
2. Do not turn `verification.<pillar>.ok: true` into authenticated evidence. In this
   fixture, every pillar has `level: "self-reported"`; backing, reserves, and
   redemption also have `source: "heuristic_fallback"`.
3. Do not turn `band: "GREEN"` into permission to trade, redeem, transfer, or
   execute. The fixture's own `band_label` ends with `"(still verify)"`.
4. Keep cross-issuer basis observations separate from independent observations of
   one exact issuer/token/chain instrument.
5. Preserve original observation time. When a source observation timestamp is not
   represented in the fixture, a consumer should not substitute retrieval time or
   the time it reads the JSON.

## Field mapping

"Consumer use" below means a safe interpretation of the fixture as published. The
last column describes evidence an action policy may require from another source; it
states the consumer's scope rather than assessing RAT Score's completeness.

| RAT fixture field(s) | Published value or label | Consumer use | Scope boundary / separately supplied policy input |
|---|---|---|---|
| `ticker`, `rwa_id`, `issuer` | `NVDA`, `2`, `Backed Finance` | Candidate discovery and display context | Ticker plus issuer is not an exact tokenized instrument identity. A consumer may separately bind the underlying, issuer, token contract, settlement chain, venue, currency, price basis, and corporate-action version before evaluating an action. |
| `score`, `band`, `band_label`, `subscores`, `weights` | `90.4`, `GREEN`, heuristic stronger-transparency label | Preserve as RAT's scored summary | RAT scores transparency; it does not authorize an action. A consumer applies its own action, instrument, actor, freshness, and risk policy. |
| `data_source`, `verification_mode`, `live_verifiers` | `fixture`, `offline_heuristic`, `false` | Mark the whole response as fixture-backed and offline | This response does not represent a live observation or a live-verifier result. A consumer requiring current state obtains that state separately. |
| `confidence.score`, `confidence.label` | `0.4`, `low` | Preserve the published confidence label with the score | A consumer should not promote the confidence level when adapting the response. |
| `verification.backing` | `self-reported`; `heuristic_fallback`; `ok: true` | Retain the issuer-name match as labeled context | RAT does not fetch authenticated issuer backing evidence in this fixture. A policy requiring authenticated backing uses a separately authenticated, instrument-bound assertion with an observation time and validity window. |
| `verification.reserves` | `self-reported`; `heuristic_fallback`; `ok: true` | Retain the reserves name-list match as labeled context | RAT does not fetch authenticated reserve evidence in this fixture. A policy requiring reserves uses a separately authenticated, instrument-bound reserve assertion with an observation time and validity window. |
| `verification.redemption` | `self-reported`; `heuristic_fallback`; `ok: true` | Retain the redemption name-list match as labeled context | RAT does not fetch authenticated redemption status in this fixture. A redemption policy may separately require instrument-bound redemption status, pause state, observation time, and validity window. |
| `verification.price`, `price.average_tokenized_price`, `price.max_deviation_pct` | `self-reported`; CMC RWA quotes; `118.55`; about `0.1265%` | Retain the CMC average and published deviation calculation as cross-wrapper context | The fixture does not represent these values as independently authenticated quotes for one exact instrument, and it does not include their original observation timestamps. A price-dependent action policy may separately require current, instrument-bound quotes from admitted independent sources. |
| `price.tokens[]` | `NVDAx` / Backed Finance at `118.45`; `NVDAon` / Ondo at `118.55`; `NVDAxst` / xStocks at `118.70` | Preserve each wrapper's issuer, symbol, price, volume, venue count, and source as a distinct row | These are three issuer wrappers, not three observations of one exact issuer/token/chain instrument. A consumer should not count them toward a same-instrument provider or independence threshold without separate exact-instrument mappings. |
| `basis`, `verification.basis` | Three wrappers; `0.210837...%` spread; `self-reported` | Preserve as RAT's cross-issuer basis signal | Cross-issuer basis does not establish same-instrument quote consensus. A consumer may evaluate basis separately from the price evidence used to authorize an exact action. |
| `price.tradfi_markets[]`, `basis.tradfi_markets[]` | Nasdaq listing and NVDA market URL; no TradFi last price in the fixture | Preserve as venue-reference metadata | A venue listing is not a current underlying price, market-session assertion, halt status, or corporate-action status. A consumer obtains any required market state separately. |
| `cik`, `verification.disclosure` | `0001045810`; `self-reported`; CMC RWA info record | Preserve as disclosure and matching context | RAT does not authenticate an exact token contract or an issuer-to-token-to-chain mapping through this field. A consumer may verify that mapping separately before binding evidence to an action. |
| `notes`, `heuristics`, `explanations` | Fixture warnings, name-list heuristic labels, and scoring explanations | Carry the relevant text or structured labels into logs and review surfaces | A consumer should not discard these labels after extracting numeric scores or booleans. |
| `cmc_calls` | `source: fixture`, `live: false`; fixture-backed endpoint list | Record how the response was produced | This is reproducibility metadata, not evidence that those endpoints were called live for this response. |
| `attestation` | SHA-256 `score_hash` plus the list of hashed fields | Preserve the fixture's hash metadata and field coverage | The fixture does not include a signer identity or signature for a consumer trust policy to authenticate. Hash metadata alone does not authorize execution. |
| No action/caller fields in the fixture | Not represented | Leave action-specific fields unfilled | RAT scores a ticker response; it does not define the consumer's action, amount, sender eligibility, target call, nonce, or execution authorization. Those inputs belong to the downstream policy. |

## Illustrative Insight handoff

The field names below come from Insight's unreleased, opt-in
[`RWA v1` consumer contract](https://github.com/imokokok/Insight/blob/main/docs/rwa-v1.md).
They illustrate one consumer; RAT Score does not depend on Insight, and this note
does not run either system.

| Insight input | Fixture-safe handoff |
|---|---|
| `RwaInstrument` | Treat `ticker: "NVDA"` and `issuer: "Backed Finance"` as candidate context only. Do not calculate an `instrumentId` until the token contract, settlement chain, venue, currency, price basis, and corporate-action version are separately bound. |
| `RwaPrice[]` | Do not convert the three `price.tokens[]` rows into same-instrument quotes. Each row names a different issuer, and the fixture does not represent an exact instrument mapping or original observation time for the row. |
| `RwaEvidence[]` | Preserve the backing, reserve, and redemption verification objects as labeled context. Do not convert a `self-reported` / `heuristic_fallback` result into an authenticated `OK` assertion. |
| `RwaMarketState` | Leave unfilled until a separately admitted source provides current session, halt, corporate-action, observation-time, and validity data for the exact instrument. |
| `RwaRequest` | Supply the concrete action, amount, sender, target call, value, nonce, and exact `instrumentId` from the consumer workflow; none is inferred from the RAT score. |
| `RwaEvaluation` | Do not derive a verdict from this fixture alone. Insight evaluates only after a policy and its required, exact-scope inputs have been supplied. |

## Worked consumer reading

For this exact file, a consumer can truthfully record all of the following at once:

- RAT Score reports `90.4 / GREEN` for the fixture-backed NVDA example.
- The run is labeled `fixture` and `offline_heuristic`, with live verifiers disabled.
- All six verification pillars are labeled `self-reported`.
- Backing, reserves, and redemption use `heuristic_fallback` issuer-name matches.
- The price and basis sections compare three wrappers from three issuers.
- The response contains no original observation timestamps for those wrapper prices.
- The response contains no exact token contract and settlement-chain mapping, current
  market state, sender eligibility, action intent, or execution authorization.

Those statements do not change or override the RAT score. They tell an action-policy
consumer which labels to retain and which policy inputs must come from other sources
before it can make its own decision. This note intentionally does not produce an
`ALLOW`, `BLOCK`, or `UNKNOWN` action verdict because no exact action or consumer
policy is part of the fixture.

## Minimal policy handoff checklist

Before an action-policy consumer evaluates a concrete request, it can check:

- [ ] The original RAT `data_source`, `verification_mode`, confidence, and relevant
      pillar-level verification objects are still attached.
- [ ] A single exact issuer/token/chain instrument has been selected independently
      of the shared `NVDA` ticker.
- [ ] Each price quote is mapped to that exact instrument, its price basis, source,
      and original observation time.
- [ ] Provider independence is established for that instrument rather than inferred
      from the three cross-issuer wrappers.
- [ ] Any required market session, halt, and corporate-action state is separately
      sourced and current.
- [ ] Any required reserve, redemption, or sender-eligibility assertion is separately
      authenticated, scoped, and time-bounded.
- [ ] The action and its execution parameters are bound by the consumer's own policy
      and authorization mechanism.

If one of these policy inputs is unavailable, the consumer should follow its own
fail-closed or review behavior without rewriting RAT's score, band, or provenance.

## Offline reproduction

From the repository root, the exact fields used by this note can be inspected
without credentials or network access:

```bash
jq '{
  ticker, rwa_id, issuer, score, band, band_label,
  data_source, verification_mode, live_verifiers, confidence,
  verification, notes, heuristics, price, basis, cik, cmc_calls, attestation
}' docs/examples/v1_score_NVDA.fixture.json
```

No adapter is included in this first note. A later runnable adapter should be
explicitly labeled as a simulation and should preserve the same scope boundaries.
