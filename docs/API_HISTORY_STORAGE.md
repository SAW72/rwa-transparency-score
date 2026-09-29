# No score store

There is no database, no disk, and no score, payload, or history cache. `POST /v1/attest/{ticker}` computes the score live, builds the canonical bytes once, hashes them, and posts that hash to `ScoreAttestation`. The response is the only copy of the payload. Save it. The contract stores the hash, not the bytes, so that file is what `verify` needs later:

```bash
python -m rwa_score.api.verify NVDA --payload-file nvda.json --offline
```

The digest is SHA-256 of those canonical bytes, the same `bytes32` the contract stores. The contract does not keccak the payload.

The only in-process attester state is a nonce lock, an in-flight map of `{tx_hash, nonce}` (plus the subject needed to poll `attested`) until the receipt lands, and the API rate-limit counters. That is pending-transaction bookkeeping, not a score cache. A restart drops it. A startup hold waits while the pending nonce is ahead of the latest nonce, and every send checks `attested` first. That reduces the chance of a duplicate. A restart in the middle of a broadcast can still cost one duplicate transaction that reverts or no-ops.

`GET /v1/attest/{ticker}/status` reads the chain only, by `tx_hash` (receipt plus the `ScoreAttested` log) or by `score_hash` (`attested`, `getAttestation`, `verify`). It does not read process memory.

`/health` reports process status, whether the attester is enabled, the configured chain id, RPC reachability, attester balance against the floor, and queue depth (in-flight count). It does not probe a database.

Future (out of scope): persistent DB only if we go mainnet or partner with a data provider like CoinMarketCap (API signups).
