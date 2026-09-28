# RAT Score attestation (Base)

Solidity contract that stores a **hash of a score payload**, a ticker, a trusted attestation time, and an attester. It never stores the raw score, band, or pillar breakdown.

`attest(scoreHash, ticker, timestamp)` still takes the third argument so existing callers keep working. That argument is stored and emitted only as `claimedAt`. The trusted time (`attestedAt`, and the `uint256` returned by `verify`) is `block.timestamp`.

Ownership is two-step. `transferOwnership` sets `pendingOwner` and leaves the current owner in control. The pending address must call `acceptOwnership`. There is no OpenZeppelin dependency; the handoff is implemented in the contract.

Anyone who cited a RAT Score can re-hash the payload and call `verify(scoreHash, ticker)`. If the hash is missing or the ticker does not match, the cited breakdown was edited or was never attested.

`attest` is **not permissionless**. Only the contract **owner** or an **allowlisted attester** (a relayer or API-held key added via `setAttester`) can lock a hash. A stranger who pays `attestationFee` cannot occupy a digest or front-run an official payload. `AlreadyAttested` still prevents a second official lock of the same hash; it does not let random payers brick official hashes.

`withdraw` pays with `call`, not the 2300-gas `transfer` stipend. A redeploy is required before Base Sepolia runs this bytecode.

## Networks

| Network | Chain ID | This repo |
|---|---|---|
| Base Sepolia | 84532 | **Allowed** — scripts + tests target this |
| Base mainnet | 8453 | **Held** — `DeployScoreAttestation` / `Attest` revert |

Spencer standing rule: testnets only until an explicit mainnet go. Do not add a mainnet deploy script in this PR.

## Layout

```
contracts/
  src/ScoreAttestation.sol
  test/ScoreAttestation.t.sol
  script/DeployScoreAttestation.s.sol
  script/Attest.s.sol
```

## Install + test

Needs [Foundry](https://book.getfoundry.sh/getting-started/installation).

```bash
cd contracts
forge install foundry-rs/forge-std@v1.16.2 --no-commit
forge test -vv
```

## Deploy (Base Sepolia only)

Paste-and-sign steps are in [`DEPLOY_BASE_SEPOLIA.md`](DEPLOY_BASE_SEPOLIA.md). Spencer broadcasts from a Foundry keystore (`--account`). Agents never pass `--broadcast`. **Never commit a key, put it in a PR, or paste it in chat.**

`script/DeployScoreAttestation.s.sol` reverts unless `block.chainid == 84532` (`mainnet held: deploy Base Sepolia only`). On `forge script` it also requires `vm.rpc("eth_chainId") == 0x14a34` and reverts if `FOUNDRY_CHAIN_ID` is set, so a spoofed `--chain-id` cannot pass. Constructor fee defaults to `0.001 ether` and cannot exceed `MAX_FEE` (0.1 ether). Extra attesters default to the zero address (none are committed in this repo). If `FINAL_OWNER` differs from the deployer, the script calls `transferOwnership` and that owner must `acceptOwnership`.

```bash
# optional defaults — leave the address placeholders; do not invent a contract address
# ATTESTATION_FEE_WEI=1000000000000000
# ATTESTER_ADDRESS=0x...
# FINAL_OWNER=0x...
```

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

2. Submit **only that hash** (plus ticker / claimed timestamp) from an **authorized**
   key (deployer/owner or an address the owner passed to `setAttester`). The
   attester is always the broadcasting `msg.sender` — there is no attester
   argument to spoof. A stranger paying the fee cannot lock the hash.
   `ATTEST_TIMESTAMP` is stored as `claimedAt`. The trusted time is the block time.

```bash
cd contracts
export ATTESTATION_CONTRACT=0x...
export SCORE_HASH=0x...          # 32-byte hex from the client
export TICKER=NVDA
# optional: ATTEST_TIMESTAMP   # claimedAt only; attestedAt is block.timestamp

# KEYSTORE_ACCOUNT is the ~/.foundry/keystores filename, not a raw key.
# DEPLOYER is that account's address (`cast wallet address --account "$KEYSTORE_ACCOUNT"`).
forge script script/Attest.s.sol:Attest \
  --rpc-url "$BASE_SEPOLIA_RPC_URL" \
  --broadcast \
  --account "$KEYSTORE_ACCOUNT" \
  --sender "$DEPLOYER"
```

Default fee is **0.001 ETH** per attestation (covers gas + a small revenue line). Owner can `setFee` / `withdraw`.

3. Re-verify:

```bash
python scripts/verify_attestation.py NVDA \
  --rpc-url "$BASE_SEPOLIA_RPC_URL" \
  --attester "$ATTESTER_ADDRESS"
```

`--contract` defaults to `0x2F073a3628D498d92956e7eFE2b26633eDa75b00`. `--fixtures` is obsolete and only warns. `cast` must be on `PATH` for the on-chain read. The script never sends a transaction and never reads a private key. Exit codes are `0` match (or `--offline` local check), `1` database, `2` nothing stored, `3` bytes or inputs mismatch, `4` ticker / chain / attester, `5` RPC, `6` `cast` missing, `7` no RPC URL and no `--offline`, `8` `inputs_json` missing. The table is in the root README.

## Hash algorithm

`sha256` of canonical JSON (sorted keys, no whitespace, UTF-8, no NaN/Infinity — RFC 8785 key order, Python number formatting) over ticker, rwa_id, issuer, score, band, **full** subscores and weights (all six live pillars, including **basis**), cik, data_source, verification `{score, level, source}` per pillar (including basis), the basis meta block, `as_of` (attest time, Unix seconds; `0` for fixtures), `data_as_of` (latest provider observation time already on the report, or null), `scorer_version`, and `inputs_digest`. `inputs_digest` hashes every scoring input kept on the report (CMC price and basis, identity, heuristic flags, and every pillar's verifier meta). It does not hash raw provider HTTP bodies or explanation prose. Headers, API keys, tokens, and URLs are not on the scoring-input allowlist. The input JSON is stored beside the payload. See `rwa_score/api/attest.py`. A cited breakdown that silently drops basis will not match. The published NVDA fixture hash assumes `scorer_version` is `unknown`.
