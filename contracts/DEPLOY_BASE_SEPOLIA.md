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
export ETHERSCAN_API_KEY="${ETHERSCAN_API_KEY:-}"         # Etherscan API V2 key, env only, never paste
export SCORE_ATTESTATION="TBD after deploy"
export OLD_SCORE_ATTESTATION="TBD after deploy"           # not committed in this repo
export OLD_ATTESTER="0x0000000000000000000000000000000000000000" # extra attester on the old contract
export OLD_OWNER="0x0000000000000000000000000000000000000000"    # current owner() of the old contract
export OLD_OWNER_ACCOUNT="<keystore-name of OLD_OWNER>"          # not a key
export NON_ATTESTER="0x0000000000000000000000000000000000000001"
export SCORE_HASH="0x0000000000000000000000000000000000000000000000000000000000000000"
export TICKER="NVDA"
export ATTEST_TX="TBD after attest"
```

`ATTESTATION_FEE_WEI` matches `ScoreAttestation.DEFAULT_FEE` and the commented default in `contracts/README.md`. Attester defaults match `DeployScoreAttestation` (`ATTESTER_ADDRESS` unset means the zero address, so no extra allowlist entry). The deployer is an attester because the constructor sets `isAttester[msg.sender]`.

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

The script reverts unless `block.chainid` is 84532. On `forge script` it also requires `eth_chainId` from the RPC to be `0x14a34` (84532) and reverts if `FOUNDRY_CHAIN_ID` is set. Do not export `FOUNDRY_CHAIN_ID` and do not pass `--chain-id`. The predicted address depends on `$DEPLOYER`'s nonce at that block. Read it before deploying:

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

## 3. Verify source (Etherscan API V2)

API key from the environment only. Do not paste it. Foundry sends this to Etherscan API V2 for chain id 84532. Do not pass `--verifier-url`.

```bash
CONSTRUCTOR_ARGS="$(cast abi-encode "constructor(uint256)" "$ATTESTATION_FEE_WEI")"
forge verify-contract \
  --rpc-url "$BASE_SEPOLIA_RPC_URL" \
  --chain 84532 \
  --etherscan-api-key "$ETHERSCAN_API_KEY" \
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
cast block "$ATTEST_BLOCK" --field timestamp --rpc-url "$BASE_SEPOLIA_RPC_URL"
cast call "$SCORE_ATTESTATION" "getAttestation(bytes32)((bytes32,string,uint256,address,uint256))" "$SCORE_HASH" --rpc-url "$BASE_SEPOLIA_RPC_URL"
```

`getAttestation` returns the `Record` struct, so the return type is one tuple. The inner types follow `ScoreAttestation.sol`: `scoreHash`, `ticker`, `attestedAt`, `attester`, `claimedAt`. The third value (`attestedAt`) must equal the `cast block` timestamp. The fifth value is `claimedAt` and is not the trusted time.

Simulate a non-attester `attest`. `cast call --from` is an `eth_call`. It does not send.

```bash
NOT_ATTESTER_SELECTOR="$(cast sig "NotAttester()")"
OUT="$(cast call "$SCORE_ATTESTATION" "attest(bytes32,string,uint256)" "$SCORE_HASH" "$TICKER" "1" --value 1000000000000000wei --from "$NON_ATTESTER" --rpc-url "$BASE_SEPOLIA_RPC_URL" 2>&1 || true)"
case "$OUT" in
  *"$NOT_ATTESTER_SELECTOR"*)
    echo "non-attester attest reverted NotAttester ($NOT_ATTESTER_SELECTOR); no transaction sent"
    ;;
  *)
    echo "FAIL: expected NotAttester selector $NOT_ATTESTER_SELECTOR"
    printf '%s\n' "$OUT"
    ;;
esac
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

Then repeat the read-only `owner()` / `pendingOwner()` calls from section 4. After accept, `owner()` is `FINAL_OWNER` and `pendingOwner()` is the zero address.

**Spencer only; agents never broadcast.** After `acceptOwnership`, `FINAL_OWNER` removes the deployer from the attester allowlist. If `FINAL_OWNER` is the deployer, skip the accept send above and still run this: the constructor allowlist entry is cleared, and that address stays authorized only because it is `owner()`.

```bash
if [ "$(cast chain-id --rpc-url "$BASE_SEPOLIA_RPC_URL")" = "84532" ]; then
  cast send "$SCORE_ATTESTATION" "setAttester(address,bool)" "$DEPLOYER" false --rpc-url "$BASE_SEPOLIA_RPC_URL" --account "$FINAL_OWNER_ACCOUNT" --sender "$FINAL_OWNER"
else
  echo "skip send: chain is not Base Sepolia 84532"
fi
```

Read-only. This must print false. It does not send.

```bash
DEPLOYER_STILL_ATTESTER="$(cast call "$SCORE_ATTESTATION" "isAttester(address)(bool)" "$DEPLOYER" --rpc-url "$BASE_SEPOLIA_RPC_URL")"
case "$DEPLOYER_STILL_ATTESTER" in
  false|0)
    echo "isAttester(DEPLOYER) is false"
    ;;
  *)
    echo "FAIL: isAttester(DEPLOYER) is $DEPLOYER_STILL_ATTESTER, expected false"
    ;;
esac
```

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
| `README.md` | 310 | Names `RWA_ATTESTATION_CONTRACT`. No address literal. |
| `rwa_score/api/settings.py` | 26 | `attestation_contract: str = ""` |
| `rwa_score/api/settings.py` | 54 | Reads `RWA_ATTESTATION_CONTRACT` from the environment. |
| `rwa_score/api/verify.py` | 97 | Same env var, default `""`. The on-chain JSON field is `attested_at`, not `timestamp`. |
| `rwa_score/api/app.py` | 330 | Returns `cfg.attestation_contract or None`. |
| `scripts/verify_attestation.py` | 7 | Example uses `$RWA_ATTESTATION_CONTRACT`. |
| `contracts/script/Attest.s.sol` | 19 | `vm.envAddress("ATTESTATION_CONTRACT")`. |
| `contracts/script/DeployScoreAttestation.s.sol` | 90 | `ATTESTER_ADDRESS` defaults to the zero address (`NO_ATTESTER`). |
| `render.yaml` | API service | Lists `RWA_ATTESTATION_CONTRACT` on `rwa-transparency-score-api` with `sync: false`. The address is not in the file. Set it in the dashboard. |
| ABI JSON | — | No ABI JSON file in the repo. |

Test-only addresses in `contracts/test/ScoreAttestation.t.sol` are not a production allowlist: `0xA11CE` (line 10), `0xB0B` (line 11), `0xBEEF` (lines 184, 368, and 420), `0x0A1E` (lines 271, 292, 307), `0xCA11` (line 349). Foundry's default script sender `0x1804c8AB1F12E6bbf3894d4083f33e07309d1f38` is not in the test source. Read-only `eth_getCode` on Base Sepolia (`https://sepolia.base.org`, chain 84532, block 47422561) returned `0x` and codesize 0 for that address and for each test address above. They are not contracts and not attesters on that chain.

## 7. Retire the previous contract

**Spencer only; agents never broadcast.**

This repo does not commit the old address, so fill `OLD_SCORE_ATTESTATION` locally after you read it from your own notes. Confirm code exists before any send:

```bash
cast codesize "$OLD_SCORE_ATTESTATION" --rpc-url "$BASE_SEPOLIA_RPC_URL"
cast call "$OLD_SCORE_ATTESTATION" "owner()(address)" --rpc-url "$BASE_SEPOLIA_RPC_URL"
cast call "$OLD_SCORE_ATTESTATION" "isAttester(address)(bool)" "$OLD_ATTESTER" --rpc-url "$BASE_SEPOLIA_RPC_URL"
```

No ScoreAttestation address is committed in this repo, so deployed bytecode was not read here. Match the send to the ABI you actually deployed:

- Source on `main` at `a5d0c36` (PR #58) has `setAttester(address,bool)`, `transferOwnership(address)`, and `acceptOwnership()`. It has no `renounceOwnership`. `transferOwnership(address(0))` reverts `ZeroAttester`. The owner stays authorized after `setAttester(owner, false)` because `authorized` is `who == owner || isAttester[who]`.
- Source before PR #58 (`b20f9a7`) has `setAttester(address,bool)` only. It has no `transferOwnership`, no `acceptOwnership`, and no `renounceOwnership`. A `transferOwnership` send to that bytecode reverts.

Retirement for either ABI is: revoke each extra attester with `setAttester` from the current owner, stop pointing clients at the address, and do not call `attest` from the owner key. The send below calls only `setAttester`, which both ABIs have. It does not call `transferOwnership`.

`OLD_OWNER` must equal `owner()` before the send. A mismatch skips the send and does not close the shell. `OLD_OWNER_ACCOUNT` is the keystore for `OLD_OWNER`. `OLD_ATTESTER` is one extra attester on that contract, not `ATTESTER_ADDRESS` from the new deploy.

```bash
ONCHAIN_OWNER="$(cast call "$OLD_SCORE_ATTESTATION" "owner()(address)" --rpc-url "$BASE_SEPOLIA_RPC_URL")"
if [ "$(printf '%s' "$ONCHAIN_OWNER" | tr '[:upper:]' '[:lower:]')" = "$(printf '%s' "$OLD_OWNER" | tr '[:upper:]' '[:lower:]')" ]; then
  if [ "$(cast chain-id --rpc-url "$BASE_SEPOLIA_RPC_URL")" = "84532" ]; then
    cast send "$OLD_SCORE_ATTESTATION" "setAttester(address,bool)" "$OLD_ATTESTER" false --rpc-url "$BASE_SEPOLIA_RPC_URL" --account "$OLD_OWNER_ACCOUNT" --sender "$OLD_OWNER"
  else
    echo "skip send: chain is not Base Sepolia 84532"
  fi
else
  echo "skip send: owner() is $ONCHAIN_OWNER, signer is $OLD_OWNER"
fi
```

## 8. Indexer note

Runbook note: the `ScoreAttested` event signature changed to `ScoreAttested(string, bytes32, uint256 attestedAt, uint256 claimedAt, address)`. Its topic0 is `keccak256` of `ScoreAttested(string,bytes32,uint256,uint256,address)`, which is `0x4d014d26aa0eac7b6be31e154921d63abec4b12a45265145127a4498a6284f07`. The previous signature `ScoreAttested(string,bytes32,uint256,address)` hashed to `0xf4b6b46a4c639075d3bbd06f60034c9db88b0fed6be99bef95720175b43a27c3`. Any indexer or log filter on the old topic0 must be updated. `rwa_score/api/verify.py` JSON output changed the trusted-time key from `timestamp` to `attested_at`. Claimed time is separate (`claimedAt` on the record) and is not that JSON field.

Compared with ScoreAttestation on `main` (`a5d0c36`, PR #58). If the deployed bytecode is the pre-#58 source (`b20f9a7`), it also lacks the ownership events below.

- `ScoreAttested` signature changed. Old: `ScoreAttested(string,bytes32,uint256,address)` and that uint256 was the caller-supplied timestamp. New: `ScoreAttested(string, bytes32, uint256 attestedAt, uint256 claimedAt, address)`. The first uint256 is `attestedAt` (`block.timestamp`). The second is `claimedAt` (the old caller timestamp). Topic0 changes, as in the runbook note above. The address is still not indexed.
- `Record` / `getAttestation` changed. Old tuple: `(bytes32 scoreHash, string ticker, uint256 timestamp, address attester)`. New tuple: `(bytes32 scoreHash, string ticker, uint256 attestedAt, address attester, uint256 claimedAt)`. The third word is still a uint256 in the same position, but the value is chain time, not the caller timestamp. `claimedAt` is appended after `attester`. `attester` stays the fourth word.
- `verify(bytes32,string)` is still `(bool,uint256,address)`. That uint256 is `attestedAt`, not `claimedAt`. The Python helper in `rwa_score/api/verify.py` labels it `attested_at` (the JSON key was `timestamp`). Claimed time stays on the record as `claimedAt` and is not this field.
- New events: `FeeUpdated(uint256 oldFee, uint256 newFee)` from `setFee` (not from the constructor). `Withdrawn(address indexed to, uint256 amount)` after a successful `withdraw`.
- Unchanged events that already exist on `a5d0c36`: `AttesterUpdated(address indexed,bool)`, `OwnershipTransferStarted(address indexed,address indexed)`, `OwnershipTransferred(address indexed,address indexed)`. Those three ownership-related events are absent from `b20f9a7`.
- `transferOwnership(address(0))` on this contract reverts `ZeroOwner()`. On `a5d0c36` that call reverts `ZeroAttester()`. `setAttester(address(0))` and `withdraw(address(0))` still revert `ZeroAttester()`. New error: `FeeTooHigh(uint256,uint256)` when the fee is above `MAX_FEE` (0.1 ether). `EmptyTimestamp()` and `FutureTimestamp()` from `a5d0c36` are gone; claimed time is stored and not checked.
