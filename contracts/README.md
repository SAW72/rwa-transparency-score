# RAT Score attestation (Base)

Solidity contract that stores a **hash of a score payload**, a ticker, a timestamp, and an attester. It never stores the raw score, band, or pillar breakdown.

Anyone who cited a RAT Score can re-hash the payload and call `verify(scoreHash, ticker)`. If the hash is missing or the ticker does not match, the cited breakdown was edited or was never attested.

## Networks

| Network | Chain ID | This repo |
|---|---|---|
| Base Sepolia | 84532 | **Allowed** — scripts + tests target this |
| Base mainnet | 8453 | **Held** — `DeploySepolia` / `Attest` revert |

Spencer standing rule: testnets only until an explicit mainnet go. Do not add a mainnet deploy script in this PR.

## Layout

```
contracts/
  src/ScoreAttestation.sol
  test/ScoreAttestation.t.sol
  script/DeploySepolia.s.sol
  script/Attest.s.sol
```

## Install + test

Needs [Foundry](https://book.getfoundry.sh/getting-started/installation).

```bash
cd contracts
forge install foundry-rs/forge-std --no-commit
forge test -vv
```

## Deploy (Base Sepolia only)

Spencer enters keys locally. **Never commit `PRIVATE_KEY`, put it in a PR, or paste it in chat.**

```bash
cd contracts
export BASE_SEPOLIA_RPC_URL=https://sepolia.base.org   # or your provider
export PRIVATE_KEY=          # funded Sepolia key — env only
# optional: ATTESTATION_FEE_WEI=1000000000000000  (0.001 ETH)

forge script script/DeploySepolia.s.sol:DeploySepolia \
  --rpc-url "$BASE_SEPOLIA_RPC_URL" \
  --broadcast \
  --private-key "$PRIVATE_KEY"
```

The script `require`s `block.chainid == 84532`. Pointing it at Base mainnet (or any other chain) reverts with `mainnet held: deploy Base Sepolia only`.

After deploy, set the address in the API host environment (not in git):

```bash
export RWA_ATTESTATION_CONTRACT=0x...
export RWA_ATTESTATION_CHAIN=base-sepolia
export RWA_ATTESTATION_CHAIN_ID=84532
```

## Attest a live score

1. Hash a score (fixtures or live API):

```bash
RWA_USE_FIXTURES=1 python scripts/verify_attestation.py NVDA --fixtures --json
# → score_hash 0x…
```

2. Submit **only that hash** (plus ticker / timestamp / attester):

```bash
cd contracts
export ATTESTATION_CONTRACT=0x...
export SCORE_HASH=0x...          # 32-byte hex from the client
export TICKER=NVDA
# optional: ATTEST_TIMESTAMP, ATTESTER

forge script script/Attest.s.sol:Attest \
  --rpc-url "$BASE_SEPOLIA_RPC_URL" \
  --broadcast \
  --private-key "$PRIVATE_KEY"
```

Default fee is **0.001 ETH** per attestation (covers gas + a small revenue line). Owner can `setFee` / `withdraw`.

3. Re-verify:

```bash
python scripts/verify_attestation.py NVDA --fixtures \
  --contract "$ATTESTATION_CONTRACT" \
  --rpc-url "$BASE_SEPOLIA_RPC_URL"
```

`cast` must be on `PATH` for the on-chain read. The script never sends a transaction and never reads a private key.

## Hash algorithm

`sha256` of canonical JSON (sorted keys, no whitespace) over ticker, rwa_id, issuer, score, band, subscores, weights, cik, data_source, and verification `{score, level, source}` per pillar. See `rwa_score/api/attest.py`.
