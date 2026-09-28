# Attester runbook (Spencer only)

Agents never broadcast, never deploy, and never read or commit a private key.
Every `cast send` below is **Spencer only**. Use `cast send --account`. Do not
pass `--private-key`. Create the attester key on your own machine, not on an
agent machine, and put it in the Render dashboard yourself.

Live contract (Base Sepolia, chain id **84532** only):

`RWA_ATTESTATION_CONTRACT=0x2F073a3628D498d92956e7eFE2b26633eDa75b00`

Owner that calls `setAttester` and `setFee`:

`0x714b8546E5F006E0E74ec23FbafcF8e7F33a081f`

## Render today

One web service, `rwa-transparency-score`, free plan, branch `main`, start
command `python -m rwa_score.health` (Streamlit). `GET /health` is JSON from
that Tornado app. `GET /v1/attest/NVDA`, `GET /v1/verify/NVDA`, `/docs`, and
`/openapi.json` return the Streamlit HTML shell. FastAPI is not served.
`rwa_score/api/app.py` defines `/v1/attest`. There is no FastAPI `/v1/verify`
route; verify is the CLI `rwa_score.api.verify`.

This runbook does not change that start command and does not add a service
to `render.yaml`. The worker runs inside the API process
(`python -m rwa_score.api`). It does nothing on the current Streamlit service
until you run the API yourself.

## What the worker does

`GET /v1/attest/{ticker}` stores the canonical payload and enqueues
`attest(scoreHash, ticker, as_of)` off the HTTP response. One thread holds
the nonce. It reads `eth_chainId` and refuses anything other than 84532. It
calls `verify` first. `AlreadyAttested` on a retry counts as success. It pays
`attestationFee()` and will not send if that fee is above
`RWA_ATTEST_VALUE_CAP_WEI` (default `0`, which matches `setFee(0)`). Gas is
capped by `RWA_ATTEST_GAS_LIMIT` (default `300000`). A Foundry gas report on
this contract showed `attest` median **208274** and max **210495** (24 calls;
the minimum includes reverts).

The queue is sqlite (`attest_jobs` on `RWA_API_DB_PATH`). A process restart
keeps those rows when the file is still there. Render's free disk is
ephemeral: spin-down deletes it, so the queue does **not** survive a free-tier
spin-down. A Render background worker or a paid plan with a persistent disk
is the option if you want the queue to outlive sleep. This repo does not add
that service.

If `RWA_ATTESTER_PRIVATE_KEY`, `RWA_ATTESTATION_CONTRACT`, or
`BASE_SEPOLIA_RPC_URL` is unset, the worker is disabled and `/v1/attest`
says so. The API does not crash and does not send.

## 1. Create a new attester key (Spencer, off agent machines)

Do this on your own computer. Do not paste the key into git, a PR, chat, or
an agent.

```bash
cast wallet new
cast wallet address --account "<new-keystore-name>"
```

Export the address only:

```bash
export ATTESTER_ADDRESS="<address from the new keystore>"
export ATTESTER_ACCOUNT="<new-keystore-name>"
```

Then set `RWA_ATTESTER_PRIVATE_KEY` in the Render dashboard yourself (env
only, not in git). Also set:

```bash
# names only — values go in the dashboard, not in this file
RWA_ATTESTATION_CONTRACT=0x2F073a3628D498d92956e7eFE2b26633eDa75b00
BASE_SEPOLIA_RPC_URL=<your Base Sepolia RPC>
RWA_ATTEST_VALUE_CAP_WEI=0
RWA_ATTEST_GAS_LIMIT=300000
```

## 2. Owner calls (Spencer only)

Fee is circular when the attester is us: `setFee(0)` so the worker does not
pay itself. The value cap stays `0`.

```bash
export BASE_SEPOLIA_RPC_URL="https://sepolia.base.org"
export OWNER="0x714b8546E5F006E0E74ec23FbafcF8e7F33a081f"
export OWNER_ACCOUNT="<keystore name for the owner>"
export SCORE_ATTESTATION="0x2F073a3628D498d92956e7eFE2b26633eDa75b00"
export ATTESTER_ADDRESS="<address from step 1>"
```

Chain-id gate. Do not `exit` the shell.

```bash
if [ "$(cast chain-id --rpc-url "$BASE_SEPOLIA_RPC_URL")" = "84532" ]; then
  echo "Base Sepolia 84532"
else
  echo "skip send: chain is not Base Sepolia 84532"
fi
```

**Spencer only. Agents never broadcast.**

```bash
if [ "$(cast chain-id --rpc-url "$BASE_SEPOLIA_RPC_URL")" = "84532" ]; then
  cast send "$SCORE_ATTESTATION" "setAttester(address,bool)" "$ATTESTER_ADDRESS" true \
    --rpc-url "$BASE_SEPOLIA_RPC_URL" \
    --account "$OWNER_ACCOUNT" \
    --sender "$OWNER"
  cast send "$SCORE_ATTESTATION" "setFee(uint256)" 0 \
    --rpc-url "$BASE_SEPOLIA_RPC_URL" \
    --account "$OWNER_ACCOUNT" \
    --sender "$OWNER"
else
  echo "skip send: chain is not Base Sepolia 84532"
fi
```

## 3. Read-only checks

No `cast send`.

```bash
cast call "$SCORE_ATTESTATION" "owner()(address)" --rpc-url "$BASE_SEPOLIA_RPC_URL"
cast call "$SCORE_ATTESTATION" "isAttester(address)(bool)" "$ATTESTER_ADDRESS" --rpc-url "$BASE_SEPOLIA_RPC_URL"
cast call "$SCORE_ATTESTATION" "attestationFee()(uint256)" --rpc-url "$BASE_SEPOLIA_RPC_URL"
cast balance "$ATTESTER_ADDRESS" --rpc-url "$BASE_SEPOLIA_RPC_URL"
```

Expect owner `0x714b8546E5F006E0E74ec23FbafcF8e7F33a081f`, `isAttester` true,
and fee `0`.

## 4. Keep the attester balance small

`setFee(0)` means attest does not move value. The key still pays gas. Keep
only enough for a few transactions. Check with `cast balance` above.

**Spencer only. Agents never broadcast.** Top up from the owner keystore:

```bash
if [ "$(cast chain-id --rpc-url "$BASE_SEPOLIA_RPC_URL")" = "84532" ]; then
  cast send "$ATTESTER_ADDRESS" --value 0.002ether \
    --rpc-url "$BASE_SEPOLIA_RPC_URL" \
    --account "$OWNER_ACCOUNT" \
    --sender "$OWNER"
else
  echo "skip send: chain is not Base Sepolia 84532"
fi
```

## 5. Revoke (Spencer only)

```bash
if [ "$(cast chain-id --rpc-url "$BASE_SEPOLIA_RPC_URL")" = "84532" ]; then
  cast send "$SCORE_ATTESTATION" "setAttester(address,bool)" "$ATTESTER_ADDRESS" false \
    --rpc-url "$BASE_SEPOLIA_RPC_URL" \
    --account "$OWNER_ACCOUNT" \
    --sender "$OWNER"
else
  echo "skip send: chain is not Base Sepolia 84532"
fi
```

Read-only follow-up: `isAttester` should be false. The owner stays authorized
even if removed from the attester list. Revoking the dedicated key does not
remove the owner.
