# Attester runbook (Spencer only)

Agents never broadcast, never deploy, and never read or commit a private key.
Every `cast send` below is **Spencer only**. Use `cast send --account` and a
keystore. Do not put the key on the command line. Create the attester key on
your own machine, not on an agent machine, and put it in the Render dashboard
yourself.

Live contract (Base Sepolia, chain id **84532** only):

`RWA_ATTESTATION_CONTRACT=0x2F073a3628D498d92956e7eFE2b26633eDa75b00`

Owner that calls `setAttester` and `setFee`:

`0x714b8546E5F006E0E74ec23FbafcF8e7F33a081f`

## Render today

One web service, `rwa-transparency-score`, free plan, branch `main`, start
command:

```bash
python -m rwa_score.health --server.port $PORT --server.address 0.0.0.0 --server.headless true
```

That process is Streamlit plus a Tornado `/health` route. `GET /health` is
JSON. `GET /v1/attest/NVDA`, `GET /v1/verify/NVDA`, `/docs`, and
`/openapi.json` return the Streamlit HTML shell (`content-type: text/html`).
FastAPI is not served there. There is no FastAPI route `/v1/verify`. Verify
is the CLI `python -m rwa_score.api.verify`.

Do not change that start command. The public scorecard stays on it.

## API service (`rwa-transparency-score-api`)

`/v1` is a second web service in `render.yaml`. The scorecard service above
is unchanged. `/v1` is not mounted on it. The attester thread and the
`on_chain` field run only in this API process.

`create_app` in `rwa_score/api/app.py` is the factory. Every argument has a
default, so uvicorn can call it with `--factory`. Start command:

```bash
uvicorn rwa_score.api.app:create_app --factory --host 0.0.0.0 --port $PORT
```

| Field | Value |
|---|---|
| Name | `rwa-transparency-score-api` |
| Runtime | Python |
| Plan | Free |
| Branch | `main` |
| Build command | `pip install -r requirements.txt` |
| Start command | the uvicorn line above |
| Health check path | `/health` |

Env vars on this service are `sync: false`. You type the values in the
dashboard after sync. A later sync will not overwrite them.

Free plan: the service spins down after about 15 minutes idle. There is
no score store, no disk, and no database. `POST /v1/attest/{ticker}`
computes the score live and returns the payload. Save that JSON. The only
in-memory attester state is a pending transaction (`tx_hash` and nonce)
until the receipt lands, plus API rate-limit counters and the attester
broadcast caps (hour, day, ticker interval, per API key). That state
is lost on restart. The broadcast caps are not shared across processes.
A startup hold waits while the pending nonce is
ahead of the latest nonce, and every send checks `attested` first. That
reduces the chance of a duplicate. A restart in the middle of a broadcast
can still cost one duplicate transaction that reverts or no-ops. A
background worker is not in `render.yaml`.

### Blueprint sync (Spencer)

This repo does not sync the blueprint and does not create the service.

1. Merge this branch into the branch your blueprint tracks (today that is
   `main`), or point the blueprint at this branch if you want the service
   before merge.
2. Render Dashboard → **Blueprints** → the blueprint for this repo.
3. **Manual Sync**. Render reads `render.yaml` and adds web service
   `rwa-transparency-score-api`. The existing `rwa-transparency-score`
   service stays on `python -m rwa_score.health`.
4. Open `rwa-transparency-score-api` → **Environment**. Each key from the
   blueprint is empty because `sync: false` stored no value.
5. Set the values from the table below. Save. Render redeploys **this**
   service. Do not put the attester key on the scorecard service.
6. Copy the service URL (the host Render shows, usually
   `https://rwa-transparency-score-api.onrender.com`). That URL is
   `API_BASE` in the end-to-end check.

`GET /health` on that URL is JSON and includes `"api": true`. The scorecard
URL does not.

## What the worker does

`POST /v1/attest/{ticker}` scores live, hashes the canonical bytes from
that request, checks `attested`, and broadcasts
`attest(scoreHash, ticker, as_of)`. The response includes the score,
`as_of`, the hash, the canonical bytes (base64 and parsed JSON),
`tx_hash`, and `pending` or `confirmed`. One thread holds the nonce. It
reads `eth_chainId` and refuses anything other than 84532. It calls
`verify` first. `AlreadyAttested` counts as success. It pays
`attestationFee()` and will not send if that fee is above
`RWA_ATTEST_VALUE_CAP_WEI` (default `0`). `ScoreAttestation`'s constructor
treats a fee argument of `0` as `DEFAULT_FEE`, which is `0.001 ether`
(`1000000000000000` wei). The live Base Sepolia contract was deployed that
way, so `attestationFee()` is `0.001 ETH` until the owner calls `setFee(0)`.
With the default value cap the worker refuses `attest_fee_cap` and sends
nothing until that call. Gas is capped by `RWA_ATTEST_GAS_LIMIT` (default
`300000`). A Foundry gas report on this contract showed `attest` median
**208274** and max **210495** (24 calls; the minimum includes reverts).

A separate local anvil measurement (not that Foundry report, not a fork,
and not a broadcast to Base Sepolia) recorded one `attest` receipt:

| | `attestationFee` still `0.001 ETH` | after `setFee(0)`, value `0` |
|---|---|---|
| `gasUsed` | `208298` (`0x32daa`) | `191198` (`0x2eade`) |
| status | `0x1` | `0x1` |
| `effectiveGasPrice` | `996817528` wei (`0x3b6a3a78`) | `989044397` wei (`0x3af39ead`) |

Tooling: anvil, forge, and cast 1.8.3, commit `cae51ad458f6abb64852b7709eb784352429825d`, build timestamp `2026-09-15T10:46:16.519267388Z`, profile dist. Local anvil `--host 127.0.0.1 --port 18545 --chain-id 84532`. Compiler from `contracts/foundry.toml`: solc 0.8.24, optimizer on, 200 runs, cancun. Deployer `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266`, derived at runtime with `cast wallet address --mnemonic` from Foundry's public test mnemonic. `forge create` of `src/ScoreAttestation.sol:ScoreAttestation` with `--constructor-args 0 --unlocked --from` that address. Deployed to `0x5FbDB2315678afecb367f032d93F642f64180aa3`. Inputs: `scoreHash` 32 bytes (`0x11` repeated, then `0x22` repeated), ticker `NVDA` (4 UTF-8 bytes), `timestamp` `1700000000`. No custom `--block-base-fee-per-gas`. The block after deploy had `baseFeePerGas` `1000000000` wei (1 gwei). The fee-path receipt landed in block 2 (`baseFeePerGas` `996817527`). The zero-fee receipt landed in block 4 (`baseFeePerGas` `989044396`). `cast send --unlocked` uses anvil's `eth_sendTransaction`. This is contract `gasUsed`, not a send through the API worker.

```bash
export PATH="$HOME/.foundry/bin:$PATH"
cd contracts
RPC=http://127.0.0.1:18545
anvil --host 127.0.0.1 --port 18545 --chain-id 84532 &
ADDR=$(cast wallet address --mnemonic "test test test test test test test test test test test junk")
forge create src/ScoreAttestation.sol:ScoreAttestation --rpc-url "$RPC" --broadcast --unlocked --from "$ADDR" --constructor-args 0
C=0x5FbDB2315678afecb367f032d93F642f64180aa3
H1=0x1111111111111111111111111111111111111111111111111111111111111111
H2=0x2222222222222222222222222222222222222222222222222222222222222222
FEE=$(cast call "$C" "attestationFee()(uint256)" --rpc-url "$RPC" | awk '{print $1}')
cast send "$C" "attest(bytes32,string,uint256)" "$H1" NVDA 1700000000 --value "$FEE" --unlocked --from "$ADDR" --rpc-url "$RPC" --json
cast send "$C" "setFee(uint256)" 0 --unlocked --from "$ADDR" --rpc-url "$RPC"
cast send "$C" "attest(bytes32,string,uint256)" "$H2" NVDA 1700000000 --value 0 --unlocked --from "$ADDR" --rpc-url "$RPC" --json
```

The worker path under the default value cap is the second call (`gasUsed` `191198`), because that cap refuses any positive `attestationFee`.

There is no score store. The moment `send` returns a hash, that hash and
its nonce are recorded before the receipt wait. A receipt timeout leaves
the entry pending. The worker does not send another transaction for that
hash. On startup, and in the worker loop, a reconciler polls that saved
hash's receipt and `attested` until it lands or
`RWA_ATTEST_BROADCAST_DEADLINE_SECONDS` (default `1800`) has passed. The
entry is dropped only when the deadline has passed, the receipt is still
missing, `attested` is false, and the account nonce has moved past the
saved nonce on a different transaction. A landed transaction is confirmed
with the hash from its receipt. `attested` is checked before every send.
A failed send's `reason` is the code `attest_failed`. The raw exception,
including the RPC URL, is not returned.

If `RWA_ATTESTER_PRIVATE_KEY`, `RWA_ATTESTATION_CONTRACT`, or
`BASE_SEPOLIA_RPC_URL` is unset, the worker is disabled and
`POST /v1/attest` still returns the score with status `disabled` (HTTP 200).
The API does not crash and does not send. `RWA_ATTEST_ENABLED` defaults off.
When the key, contract, and RPC are set and the switch is off,
`POST /v1/attest` returns HTTP 503 `attester_disabled` and does not score
and does not send. `GET /health` and `GET /v1/attest/{ticker}/status` report
`attester` and `attester_enabled`.

### Safety limits

Checks run cheapest first. A refusal does not sign and does not broadcast.

| Check | Env | Default | Over the limit |
|---|---|---|---|
| Kill switch | `RWA_ATTEST_ENABLED` | off (`0` / unset) | HTTP 503 `attester_disabled`. Worker thread does not start. |
| Fixtures | `RWA_USE_FIXTURES` with the switch on and the attester configured | must be `0` to send | HTTP 503 `fixtures_with_attester`. |
| Gas config | `RWA_ATTEST_MAX_FEE_GWEI` | `20` | HTTP 503 `attest_gas_fee_cap` when the value is outside `(0, 100]`. `100` gwei is a hard ceiling the config cannot exceed. |
| Hourly cap | `RWA_ATTEST_MAX_PER_HOUR` | `6` | HTTP 429 `attest_hourly_cap`. |
| Daily cap | `RWA_ATTEST_MAX_PER_DAY` | `24` | HTTP 429 `attest_daily_cap`. |
| Per-ticker interval | `RWA_ATTEST_MIN_INTERVAL_SECONDS` | `600` | HTTP 429 `attest_ticker_interval`. `0` disables this interval. |
| Per-API-key daily cap | `RWA_ATTEST_MAX_PER_KEY_PER_DAY` | `8` | HTTP 429 `attest_key_daily_cap`. Applies to `POST /v1/attest` (`api_key_id`). `0` refuses every keyed send. |
| Balance floor | `RWA_ATTEST_MIN_BALANCE_WEI` | `1000000000000000` (0.001 ETH) | HTTP 503 `attester_low_balance`. |
| Contract fee | `RWA_ATTEST_VALUE_CAP_WEI` | `0` | HTTP 503 `attest_fee_cap` when `attestationFee()` is above the cap. |
| Base fee | `RWA_ATTEST_MAX_FEE_GWEI` | `20` | HTTP 503 `attest_gas_fee_cap` when base fee plus priority would need more than the ceiling. The tx is not signed. `maxFeePerGas` is not clamped under the required base plus priority. |

`GET /health` (and the attest status payload) includes stable fields for ops:

| Field | Meaning |
|---|---|
| `attester_low_balance` | Boolean. `true` only when a known balance is below a positive floor. |
| `attester_balance_wei` | Integer wei, or `null` when the balance has not been read. |
| `attester_min_balance_wei` | The configured floor, in wei. |
| `attester_balance` | `low`, `ok`, or `unknown`. |

An unread balance is `unknown` and `attester_low_balance` is `false`. A failed balance read refuses the send.

The hourly, daily, per-ticker, and per-API-key counters live in this process. They are lost on restart and on free-plan spin-down. They are not shared across processes. Two API instances each allow the configured cap, so the real ceiling is the cap times the number of instances. Run one API instance. An in-flight identical hash is the existing dedup: the second POST sees the pending hash and does not spend another cap slot and does not send a second transaction. A cap of `0` refuses every send of that kind. Setting `RWA_ATTEST_MIN_BALANCE_WEI=0` turns the floor off. A negative gas ceiling is stored as the default `20` gwei. A value above `100` is refused.

### Enable on Base Sepolia

Leave `RWA_ATTEST_ENABLED` unset until all of these are true:

1. `RWA_USE_FIXTURES=0`. Fixtures plus an armed attester refuse every send.
2. Owner has called `setFee(0)` and `attestationFee()` reads `0`. Until then the default value cap refuses.
3. `cast balance` for the attester is at least `RWA_ATTEST_MIN_BALANCE_WEI`. Watch `attester_low_balance` on `GET /health`.
4. One API process. Caps reset when that process restarts.
5. Then set `RWA_ATTEST_ENABLED=1` in the dashboard. The key stays in the dashboard. Do not put it in git.

`POST /v1/attest/{ticker}` scores again on every call. Do not poll it.
`GET /v1/attest/{ticker}/status` reads the chain only, by `tx_hash`
(receipt plus `ScoreAttested`) or by `score_hash` (`attested`,
`getAttestation`, `verify`). It does not score and it does not read
process memory.

## Render settings (API service)

Set these on `rwa-transparency-score-api` after Blueprint sync. Example shapes are not real values.
`sync: false` in `render.yaml` means the dashboard holds the value.

The worker does not trust `RWA_ATTESTATION_CHAIN_ID` when it sends. It calls
`eth_chainId` on `BASE_SEPOLIA_RPC_URL` and refuses anything other than
`84532`. Set the env to `84532` so the JSON field matches that gate.

| Name | Purpose | Example format | Secret |
|---|---|---|---|
| `RWA_ATTESTER_PRIVATE_KEY` | Signs `attest`. Dedicated key from step 1. | `0x` plus 64 hex characters | yes |
| `RWA_ATTESTATION_CONTRACT` | ScoreAttestation address the worker calls | `0x` plus 40 hex characters | no |
| `BASE_SEPOLIA_RPC_URL` | RPC for `eth_chainId`, `verify`, and the send | `https://base-sepolia.example/v2/<key>` | yes |
| `RWA_ATTESTATION_CHAIN_ID` | `chain_id` in the API JSON. Send gate is still the RPC. | `84532` | no |
| `RWA_ATTESTATION_CHAIN` | `chain` label in the API JSON | `base-sepolia` | no |
| `RWA_ATTEST_ENABLED` | Kill switch. Default off. `1` allows sends when the key, contract, and RPC are set and fixtures are off | `0` | no |
| `RWA_ATTEST_VALUE_CAP_WEI` | Max wei attached to `attest`. Default `0`. Live fee is `0.001 ETH` until `setFee(0)` | `0` | no |
| `RWA_ATTEST_MIN_BALANCE_WEI` | Refuse when the attester balance is below this. Default `1000000000000000` (0.001 ETH). `0` disables the floor | `1000000000000000` | no |
| `RWA_ATTEST_MAX_PER_HOUR` | In-process broadcasts per hour. Default `6`. `0` refuses every send | `6` | no |
| `RWA_ATTEST_MAX_PER_DAY` | In-process broadcasts per day. Default `24`. `0` refuses every send | `24` | no |
| `RWA_ATTEST_MIN_INTERVAL_SECONDS` | Minimum seconds between broadcasts for one ticker. Default `600`. `0` disables the interval | `600` | no |
| `RWA_ATTEST_MAX_PER_KEY_PER_DAY` | Broadcasts per paid API key per day. Default `8`. `0` refuses every keyed send | `8` | no |
| `RWA_ATTEST_MAX_FEE_GWEI` | EIP-1559 ceiling in gwei. Default `20`. Must be in `(0, 100]`. Above `100` is refused | `20` | no |
| `RWA_ATTEST_GAS_LIMIT` | Gas cap. Default `300000` | `300000` | no |
| `RWA_ATTEST_MAX_ATTEMPTS` | Retries before the job is failed. Default `5` | `5` | no |
| `RWA_ATTEST_BACKOFF_SECONDS` | Base delay between retries. Default `2.0` | `2.0` | no |
| `RWA_ATTEST_BROADCAST_DEADLINE_SECONDS` | Seconds to poll a broadcast before the drop check. Default `1800` | `1800` | no |
| `RWA_ATTEST_WAIT_SECONDS` | How long POST waits for a receipt before returning `pending`. Default `0` (return as soon as the tx is broadcast) | `0` | no |
| `RWA_API_BOOTSTRAP_KEY` | Paid key recreated on boot. The key list is in memory and is lost on restart | `rat_` plus a long random token | yes |
| `RWA_API_BOOTSTRAP_TIER` | Tier of that key. `/v1/attest` requires `paid` | `paid` | no |
| `RWA_API_FREE_DAILY_LIMIT` | Daily cap for a free key. Default `50`. Attest does not accept a free key | `50` | no |
| `RWA_API_RATE_WINDOW_SECONDS` | Window for that cap. Default `86400` | `86400` | no |
| `RWA_USE_FIXTURES` | `0` scores live CMC. `1` is the offline fixture client | `0` | no |
| `CMC_API_KEY` | CoinMarketCap key the scorer reads for a live score | 32–64 character token | yes |
| `POLYGON_RPC_URL` | Optional Chainlink PoR RPC (polygon). Public fallback if unset | `https://polygon.example/v2/<key>` | yes if the URL embeds a key |
| `MATIC_RPC_URL` | Optional alias of `POLYGON_RPC_URL` | same | yes if the URL embeds a key |
| `BASE_RPC_URL` | Optional Chainlink PoR RPC (Base mainnet). Not the attester RPC | `https://mainnet.base.example` | yes if the URL embeds a key |
| `ETH_RPC_URL` | Optional Chainlink PoR RPC (Ethereum) | `https://ethereum.example/v2/<key>` | yes if the URL embeds a key |
| `ETHEREUM_RPC_URL` | Optional alias of `ETH_RPC_URL` | same | yes if the URL embeds a key |
| `HOST` | Bind address. Default `0.0.0.0`. Leave unset on Render | `0.0.0.0` | no |
| `PORT` | Render injects this. Do not set it | `10000` | no |
| `RENDER_GIT_COMMIT` | Render injects the full git SHA. `scorer_version` reads it first | 40 hex characters | no |

`SOURCE_VERSION` and `GIT_COMMIT` do not change `scorer_version`. `/v1/score`
and `/v1/attest` do not read `XAI_API_KEY`.

## No score store

Live calculation plus contract posting only. There is no database and no
score, payload, or history cache. The caller saves the JSON
`POST /v1/attest/{ticker}` returns and passes that file to
`verify --payload-file` later. The on-chain record is the hash only.

The only in-memory attester state is pending-tx state (`tx_hash` and
nonce) until the receipt lands, plus rate-limit counters and the attester
broadcast caps. It is in memory and is lost on restart and on free-plan
spin-down. Run a single API instance: each process enforces the cap on its
own, so N instances allow N times the configured cap. The startup hold plus the `attested` pre-check reduce the
chance of a duplicate. A restart in the middle of a broadcast can still
cost one duplicate transaction that reverts or no-ops. There is no Render
Postgres, no `DATABASE_URL`,
and no dump or restore step.

Future (out of scope): persistent DB only if we go mainnet or partner with a data provider like CoinMarketCap (API signups).

## 1. Create a new attester key (Spencer, off agent machines)

Do this on your own computer. Do not paste the key into git, a PR, chat, or
an agent.

```bash
cast wallet new "$HOME/.foundry/keystores" "<new-keystore-name>"
cast wallet address --account "<new-keystore-name>"
```

Export the address only:

```bash
export ATTESTER_ADDRESS="<address from the new keystore>"
export ATTESTER_ACCOUNT="<new-keystore-name>"
```

Then set the API service env in the Render dashboard yourself. Names and
shapes are in the next section. Never paste a real key into git, a PR, or chat.

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

Expect owner `0x714b8546E5F006E0E74ec23FbafcF8e7F33a081f` and `isAttester`
true. Expect fee `0` only after `setFee(0)`. Before that call the live
contract returns `1000000000000000` wei (`0.001 ETH`), because the
constructor maps a `0` fee argument to `DEFAULT_FEE`. The worker's default
value cap is `0`, so it refuses `attest_fee_cap` and sends nothing until
the fee reads `0`.

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

The worker refuses to sign if its address is `owner()`. Use a dedicated
attester. Keep that address's balance small (section 4): `setFee(0)` means
the only spend is gas.

## 6. Live end-to-end check (Spencer only, Base Sepolia)

Do this after steps 1–3, with the API service up, `isAttester` true, and
`attestationFee` `0`. Run it in one sitting. The API does not keep the
payload. Save the POST body before the service spins down if you still
want `verify` to see the bytes. The chain record stays. Pending-tx state
is in memory and is lost on restart.

Agents do not run this section. It sends a transaction from the key you set
on Render. `API_BASE` is the `rwa-transparency-score-api` URL from the
service page. Use the host Render shows if it is not the default below.

```bash
export API_BASE="https://rwa-transparency-score-api.onrender.com"
export API_KEY="rat_<the RWA_API_BOOTSTRAP_KEY you set>"
export BASE_SEPOLIA_RPC_URL="https://<your Base Sepolia RPC host>/<key>"
export SCORE_ATTESTATION="0x2F073a3628D498d92956e7eFE2b26633eDa75b00"
export ATTESTER_ADDRESS="<address from step 1>"
```

### 6.1 Score NVDA on the live scorecard

Open https://rwa-transparency-score.onrender.com and score `NVDA`.

Expected: an HTML scorecard with ticker `NVDA`, a numeric score, and a band
(`GREEN`, `YELLOW`, `ORANGE`, or `RED`). That page does not send `attest()`.

The same host still does not serve the API:

```bash
curl -sI "https://rwa-transparency-score.onrender.com/v1/attest/NVDA" | head -n 15
```

Expected: `HTTP/2 200` (or `HTTP/1.1 200`) and `content-type: text/html`.

### 6.2 Confirm the API service is the one that scores

```bash
curl -sS "$API_BASE/health"
```

Expected JSON includes `"api": true`, `"fixtures": false`, and
`"verifiers_live": true`. `git_sha` is a short SHA, not the word `unknown`,
once Render has set `RENDER_GIT_COMMIT`.

```bash
curl -sS -H "X-API-Key: $API_KEY" "$API_BASE/v1/score/NVDA" -o /tmp/nvda-score.json
python3 - <<'PY'
import json
body = json.load(open("/tmp/nvda-score.json"))
h = body["attestation"]["score_hash"]
assert body["ticker"] == "NVDA"
assert h.startswith("0x") and len(h) == 66
assert isinstance(body["score"], (int, float))
print("score", body["score"], body["band"], h)
PY
```

Expected print: `score <number> <BAND> 0x<64 hex>`. This hash is not the
attested hash. The next call scores again and `as_of` moves.

### 6.3 Post one attest, then read the chain

Call attest **once**. This is a POST. It scores live, hashes those bytes,
checks `attested`, and broadcasts. Save the body. The API does not keep it.

```bash
curl -sS -X POST -H "X-API-Key: $API_KEY" "$API_BASE/v1/attest/NVDA" -o /tmp/nvda-attest.json
python3 - <<'PY'
import json
body = json.load(open("/tmp/nvda-attest.json"))
assert body["ticker"] == "NVDA"
assert body["chain_id"] == 84532
assert body["contract"] == "0x2F073a3628D498d92956e7eFE2b26633eDa75b00"
assert body["algo"] == "sha256"
assert body["score_hash"].startswith("0x") and len(body["score_hash"]) == 66
assert body["canonical_b64"]
assert isinstance(body["payload"], dict)
assert body["status"] in {"pending", "confirmed"}
print(body["status"], body["score_hash"], body["tx_hash"])
PY
```

Expected: `status` is `pending` or `confirmed`. `tx_hash` is `0x` plus 64
hex characters when a transaction was broadcast. `score_hash` is the
SHA-256 of `canonical_b64`. Keep `/tmp/nvda-attest.json`.

Poll status by that tx hash. This reads the chain only. It does not score
and it does not read process memory:

```bash
python3 - <<'PY'
import json, os, time, urllib.parse, urllib.request
posted = json.load(open("/tmp/nvda-attest.json"))
tx = posted.get("tx_hash") or ""
query = urllib.parse.urlencode({"tx_hash": tx, "score_hash": posted["score_hash"]})
url = os.environ["API_BASE"].rstrip("/") + "/v1/attest/NVDA/status?" + query
req = urllib.request.Request(url, headers={"X-API-Key": os.environ["API_KEY"]})
for i in range(12):
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.load(resp)
    oc = body["on_chain"]
    print(i, body.get("status"), oc.get("attested"), oc.get("tx"))
    if oc.get("attested") is True and oc.get("attestedAt") is not None:
        json.dump(body, open("/tmp/nvda-status.json", "w"))
        raise SystemExit(0)
    time.sleep(5)
raise SystemExit("chain did not confirm")
PY
```

Expected: the script exits 0. `on_chain.attested` is true, `on_chain.tx` is
`0x` plus 64 hex characters, `on_chain.attestedAt` is an integer, and
`status` is `confirmed`. Save the tx:

```bash
export TX="$(python3 -c 'import json; print(json.load(open("/tmp/nvda-status.json"))["tx_hash"])')"
export SCORE_HASH="$(python3 -c 'import json; print(json.load(open("/tmp/nvda-attest.json"))["score_hash"])')"
echo "$TX"
```

Expected: `TX` is `0x` plus 64 hex characters.

### 6.4 See the transaction from the attester address

```bash
echo "https://sepolia.basescan.org/tx/$TX"
cast receipt "$TX" --rpc-url "$BASE_SEPOLIA_RPC_URL"
```

Expected on the basescan page: status **Success**, From = `$ATTESTER_ADDRESS`,
To = `0x2F073a3628D498d92956e7eFE2b26633eDa75b00`, method `attest`.

Expected `cast receipt` fields include:

```text
status               1
from                 <ATTESTER_ADDRESS>
to                   0x2F073a3628D498d92956e7eFE2b26633eDa75b00
```

### 6.5 `verify.py` against the saved payload

The live check is: `POST /v1/attest/NVDA` (section 6.3), save that JSON
to a file, then verify the file. The API does not keep a copy. Do not
POST again here. `/tmp/nvda-attest.json` is the response from 6.3.

```bash
python -m rwa_score.api.verify NVDA --json \
  --payload-file /tmp/nvda-attest.json \
  --rpc-url "$BASE_SEPOLIA_RPC_URL" \
  --attester "$ATTESTER_ADDRESS"
```

Expected process exit code: `0`. Expected JSON has `"match": true`.

A run with no `--payload-file` exits `1` and prints
`supply --payload-file (the JSON returned by POST /v1/attest)`.
It does not say that a stored payload is missing.

The command passes `--payload-file`, `--rpc-url`, and `--attester`. It
does not use `--offline`. The chain read uses `attested`,
`getAttestation`, `verify`, and the `ScoreAttested` log on `tx_hash`.
The digest is SHA-256 of the canonical bytes. The contract stores that
hash. It does not keccak the payload. `--contract` defaults to the
pinned address.

| Exit | Meaning |
| --- | --- |
| `0` | Canonical bytes match their hash and the chain read matched. `--offline` is also `0` when the local checks pass, and it prints that nothing was checked on-chain. |
| `1` | Ran without `--payload-file`. The message is `supply --payload-file (the JSON returned by POST /v1/attest)`. The same exit is used when the path does not exist or the file is not a bundle. |
| `3` | Tampered or malformed bytes, or inputs in the file do not recompute `inputs_digest`. |
| `4` | Ticker mismatch, chain id is not 84532, `attested` / `verify()` is false, the attester mismatches, or the receipt event does not match. |
| `5` | RPC / cast call failed. |
| `6` | A chain read was requested but `cast` is not on `PATH`. |
| `7` | No `--rpc-url` and `BASE_SEPOLIA_RPC_URL` unset, and `--offline` was not passed. |

Exit `2` (nothing stored) and exit `8` (inputs missing) are retired.

Expected JSON fields:

```json
{
  "ticker": "NVDA",
  "stored": true,
  "hash_ok": true,
  "score_hash": "0x<same 64 hex as /v1/attest/NVDA/status>",
  "on_chain": {
    "ok": true,
    "attested_at": "<same integer as on_chain.attestedAt>",
    "attester": "<ATTESTER_ADDRESS>"
  },
  "match": true
}
```

`match` is `true` only when the saved bytes recompute to that hash, the
chain id is 84532, and the on-chain attester is `$ATTESTER_ADDRESS`. A run
with no `--payload-file` exits `1`. Exit `2` is retired: there is no stored
row to miss. `"stored": true` in this JSON means the file loaded, not that
the API kept a copy.
