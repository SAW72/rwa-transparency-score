# Deploy ScoreAttestation on Base Sepolia

Paste-and-sign for Spencer. Chain id **84532** only. Agents never add `--broadcast`, never send a transaction, and never read or commit a private key. Base mainnet (8453) is held.

The new contract address is **TBD after deploy**. Do not write it into git. This repo does not commit a ScoreAttestation address or a production attester address.

## Env

Set these in the shell. Do not paste secrets into this file, a PR, or chat.

```bash
export BASE_SEPOLIA_RPC_URL="https://sepolia.base.org"   # or your provider
export KEYSTORE_ACCOUNT="<keystore-name>"                 # ~/.foundry/keystores filename
export DEPLOYER="<checksummed address from the keystore>" # not a key
export ATTESTATION_FEE_WEI="1000000000000000"             # 0.001 ether, repo default
export ATTESTER_ADDRESS="0x0000000000000000000000000000000000000000" # repo default: none
export ATTESTER_ADDRESS_2="0x0000000000000000000000000000000000000000"
export ATTESTER_ADDRESS_3="0x0000000000000000000000000000000000000000"
export FINAL_OWNER="$DEPLOYER"                            # or a different checksummed owner
export FINAL_OWNER_ACCOUNT="$KEYSTORE_ACCOUNT"            # keystore of FINAL_OWNER when they differ
export BASESCAN_API_KEY="${ETHERSCAN_API_KEY:-}"          # env only, never paste
export SCORE_ATTESTATION="TBD after deploy"
export OLD_SCORE_ATTESTATION="TBD after deploy"           # not committed in this repo
export NON_ATTESTER="0x0000000000000000000000000000000000000001"
export SCORE_HASH="0x0000000000000000000000000000000000000000000000000000000000000000"
export TICKER="NVDA"
export ATTEST_TX="TBD after attest"
```

`ATTESTATION_FEE_WEI` matches `ScoreAttestation.DEFAULT_FEE` and the commented default in `contracts/README.md`. Attester defaults match `DeploySepolia` (`ATTESTER_ADDRESS` unset means the zero address, so no extra allowlist entry). The deployer is an attester because the constructor sets `isAttester[msg.sender]`.

Derive `DEPLOYER` from the keystore. This command does not take a raw key:

```bash
cast wallet address --account "$KEYSTORE_ACCOUNT"
```

## Chain-id gate

Put the gate on the send itself. Do not `exit` the shell.

```bash
if [ "$(cast chain-id --rpc-url "$BASE_SEPOLIA_RPC_URL")" = "84532" ]; then
  echo "Base Sepolia 84532"
else
  echo "skip send: chain is not Base Sepolia 84532"
fi
```

## 1. Simulate this checkout first

No `--broadcast`. From the repo root, `cd contracts` on the commit you intend to deploy (`git rev-parse HEAD`).

```bash
forge script script/DeployScoreAttestation.s.sol:DeployScoreAttestation \
  --rpc-url "$BASE_SEPOLIA_RPC_URL" \
  --sender "$DEPLOYER"
```

The script reverts unless `block.chainid` is 84532. The predicted address depends on `$DEPLOYER`'s nonce at that block. Read it before deploying:

```bash
cast nonce "$DEPLOYER" --rpc-url "$BASE_SEPOLIA_RPC_URL"
cast block-number --rpc-url "$BASE_SEPOLIA_RPC_URL"
```

## 2. Deploy

**Spencer only; agents never broadcast.**

```bash
if [ "$(cast chain-id --rpc-url "$BASE_SEPOLIA_RPC_URL")" = "84532" ]; then
  forge script script/DeployScoreAttestation.s.sol:DeployScoreAttestation \
    --rpc-url "$BASE_SEPOLIA_RPC_URL" \
    --broadcast \
    --account "$KEYSTORE_ACCOUNT" \
    --sender "$DEPLOYER"
else
  echo "skip send: chain is not Base Sepolia 84532"
fi
```

Copy the logged `ScoreAttestation` address into `SCORE_ATTESTATION` in the shell only. If `FINAL_OWNER` differs from `DEPLOYER`, this broadcast already calls `transferOwnership`. `owner()` stays the deployer until `acceptOwnership`.

## 3. Verify source on Basescan

API key from the environment only. Do not paste it.

```bash
CONSTRUCTOR_ARGS="$(cast abi-encode "constructor(uint256)" "$ATTESTATION_FEE_WEI")"
forge verify-contract \
  --rpc-url "$BASE_SEPOLIA_RPC_URL" \
  --chain 84532 \
  --verifier etherscan \
  --verifier-url "https://api-sepolia.basescan.org/api" \
  --etherscan-api-key "$BASESCAN_API_KEY" \
  --constructor-args "$CONSTRUCTOR_ARGS" \
  --watch \
  "$SCORE_ATTESTATION" \
  src/ScoreAttestation.sol:ScoreAttestation
```

## 4. Read-only checks

These blocks are `cast call` / `cast block` / `cast tx` only. No `--broadcast` and no `cast send`.

```bash
cast call "$SCORE_ATTESTATION" "owner()(address)" --rpc-url "$BASE_SEPOLIA_RPC_URL"
cast call "$SCORE_ATTESTATION" "pendingOwner()(address)" --rpc-url "$BASE_SEPOLIA_RPC_URL"
cast call "$SCORE_ATTESTATION" "isAttester(address)(bool)" "$DEPLOYER" --rpc-url "$BASE_SEPOLIA_RPC_URL"
cast call "$SCORE_ATTESTATION" "isAttester(address)(bool)" "$ATTESTER_ADDRESS" --rpc-url "$BASE_SEPOLIA_RPC_URL"
cast call "$SCORE_ATTESTATION" "isAttester(address)(bool)" "$ATTESTER_ADDRESS_2" --rpc-url "$BASE_SEPOLIA_RPC_URL"
cast call "$SCORE_ATTESTATION" "isAttester(address)(bool)" "$ATTESTER_ADDRESS_3" --rpc-url "$BASE_SEPOLIA_RPC_URL"
cast call "$SCORE_ATTESTATION" "attestationFee()(uint256)" --rpc-url "$BASE_SEPOLIA_RPC_URL"
```

After an attest transaction exists (`ATTEST_TX`), compare the stored trusted time to that block's timestamp. `verify` returns `attestedAt`, which the contract sets to `block.timestamp`.

```bash
cast call "$SCORE_ATTESTATION" "verify(bytes32,string)(bool,uint256,address)" "$SCORE_HASH" "$TICKER" --rpc-url "$BASE_SEPOLIA_RPC_URL"
ATTEST_BLOCK="$(cast tx "$ATTEST_TX" blockNumber --rpc-url "$BASE_SEPOLIA_RPC_URL")"
cast block "$ATTEST_BLOCK" timestamp --rpc-url "$BASE_SEPOLIA_RPC_URL"
cast call "$SCORE_ATTESTATION" "getAttestation(bytes32)(bytes32,string,uint256,address,uint256)" "$SCORE_HASH" --rpc-url "$BASE_SEPOLIA_RPC_URL"
```

The third `getAttestation` field (`attestedAt`) must equal the `cast block` timestamp. The fifth field is `claimedAt` and is not the trusted time.

Simulate a non-attester `attest`. `cast call --from` is an `eth_call`. It does not send.

```bash
if cast call "$SCORE_ATTESTATION" "attest(bytes32,string,uint256)" "$SCORE_HASH" "$TICKER" "1" --value 1000000000000000wei --from "$NON_ATTESTER" --rpc-url "$BASE_SEPOLIA_RPC_URL"; then
  echo "unexpected success: non-attester attest simulation"
else
  echo "non-attester attest reverted in simulation (no transaction sent)"
fi
```

## 5. Ownership handoff

Skip the `transferOwnership` send when section 2 already started it (`pendingOwner()` equals `FINAL_OWNER`). `acceptOwnership` is still required when `FINAL_OWNER` differs from the deployer.

**Spencer only; agents never broadcast.**

```bash
if [ "$(cast chain-id --rpc-url "$BASE_SEPOLIA_RPC_URL")" = "84532" ]; then
  cast send "$SCORE_ATTESTATION" "transferOwnership(address)" "$FINAL_OWNER" --rpc-url "$BASE_SEPOLIA_RPC_URL" --account "$KEYSTORE_ACCOUNT" --sender "$DEPLOYER"
else
  echo "skip send: chain is not Base Sepolia 84532"
fi
```

**Spencer only; agents never broadcast.** Run this from the final owner's keystore (`--account` / `--sender` are that owner, not the deployer).

```bash
if [ "$(cast chain-id --rpc-url "$BASE_SEPOLIA_RPC_URL")" = "84532" ]; then
  cast send "$SCORE_ATTESTATION" "acceptOwnership()" --rpc-url "$BASE_SEPOLIA_RPC_URL" --account "$FINAL_OWNER_ACCOUNT" --sender "$FINAL_OWNER"
else
  echo "skip send: chain is not Base Sepolia 84532"
fi
```

Then repeat the read-only `owner()` / `pendingOwner()` calls from section 4. After accept, `owner()` is `FINAL_OWNER` and `pendingOwner()` is the zero address. The previous owner remains an attester until `setAttester` revokes them, and is still authorized while they are `owner()`.

## 6. Repoint list

No file in this repo stores a live ScoreAttestation address or a production attester. Leave every placeholder below unchanged. The new address is **TBD after deploy**. Set it in the API host environment, not in git.

```bash
export RWA_ATTESTATION_CONTRACT="$SCORE_ATTESTATION"   # TBD after deploy — do not commit
export RWA_ATTESTATION_CHAIN="base-sepolia"
export RWA_ATTESTATION_CHAIN_ID="84532"
```

Files that mention the contract or attester slot (none of these is a deployed address):

| File | Line | What to change after deploy |
|---|---|---|
| `.env.example` | 51 | `# RWA_ATTESTATION_CONTRACT=` is empty. Keep it empty in git. Set the real value on the host. |
| `contracts/README.md` | 53 | `# ATTESTER_ADDRESS=0x...` placeholder. |
| `contracts/README.md` | 54 | `# FINAL_OWNER=0x...` placeholder. |
| `contracts/README.md` | 60 | `export RWA_ATTESTATION_CONTRACT=0x...` placeholder. |
| `contracts/README.md` | 82 | `export ATTESTATION_CONTRACT=0x...` placeholder. |
| `README.md` | 309 | Names `RWA_ATTESTATION_CONTRACT`. No address literal. |
| `rwa_score/api/settings.py` | 26 | `attestation_contract: str = ""` |
| `rwa_score/api/settings.py` | 54 | Reads `RWA_ATTESTATION_CONTRACT` from the environment. |
| `rwa_score/api/verify.py` | 96 | Same env var, default `""`. |
| `rwa_score/api/app.py` | 330 | Returns `cfg.attestation_contract or None`. |
| `scripts/verify_attestation.py` | 7 | Example uses `$RWA_ATTESTATION_CONTRACT`. |
| `contracts/script/Attest.s.sol` | 19 | `vm.envAddress("ATTESTATION_CONTRACT")`. |
| `contracts/script/DeploySepolia.s.sol` | 16 | `ATTESTER_ADDRESS` defaults to `address(0)`. |
| `render.yaml` | — | Does not set `RWA_ATTESTATION_CONTRACT`. No line to edit. |
| ABI JSON | — | No ABI JSON file in the repo. |

Test-only addresses in `contracts/test/ScoreAttestation.t.sol` are not a production allowlist: `0xA11CE` (line 11), `0xB0B` (line 12), `0xBEEF` (line 185), `0x0A1E` (lines 300, 321, 336), `0xCA11` (line 378). Foundry's default script sender `0x1804c8AB1F12E6bbf3894d4083f33e07309d1f38` appears only in test logs. Read-only `eth_getCode` on Base Sepolia (`https://sepolia.base.org`, chain 84532, block 47422561) returned `0x` and codesize 0 for each of those addresses. They are not contracts and not attesters on that chain.

## 7. Retire the previous contract

**Spencer only; agents never broadcast.**

This repo does not commit the old address, so fill `OLD_SCORE_ATTESTATION` locally after you read it from your own notes. Confirm code exists before any send:

```bash
cast codesize "$OLD_SCORE_ATTESTATION" --rpc-url "$BASE_SEPOLIA_RPC_URL"
cast call "$OLD_SCORE_ATTESTATION" "owner()(address)" --rpc-url "$BASE_SEPOLIA_RPC_URL"
cast call "$OLD_SCORE_ATTESTATION" "isAttester(address)(bool)" "$DEPLOYER" --rpc-url "$BASE_SEPOLIA_RPC_URL"
```

The contract that was deployable from `main` before two-step ownership has `setAttester(address,bool)` and no `renounceOwnership` and no `transferOwnership`. The owner stays authorized even after `setAttester(owner, false)` (`authorized` is `who == owner || isAttester[who]`). You cannot renounce that bytecode. Retirement is: revoke every extra attester, stop pointing clients at the address, and do not call `attest` from the owner key.

There is no production attester address in git. For each extra attester you actually allowlisted at deploy time:

```bash
if [ "$(cast chain-id --rpc-url "$BASE_SEPOLIA_RPC_URL")" = "84532" ]; then
  cast send "$OLD_SCORE_ATTESTATION" "setAttester(address,bool)" "$ATTESTER_ADDRESS" false --rpc-url "$BASE_SEPOLIA_RPC_URL" --account "$KEYSTORE_ACCOUNT" --sender "$DEPLOYER"
else
  echo "skip send: chain is not Base Sepolia 84532"
fi
```

Do not call `transferOwnership` on bytecode that does not have it. On the current source, `transferOwnership(address(0))` reverts `ZeroAttester`, and `address(0)` cannot call `acceptOwnership`, so that is not a renounce. The current source also has no `renounceOwnership`.
