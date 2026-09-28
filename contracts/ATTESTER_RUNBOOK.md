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

## What makes `/v1` reachable

Recommended: a **second** free web service, `rwa-transparency-api`, specified
in `render.yaml`. This repo does not create it and does not deploy. You apply
the blueprint, or you create the service in the dashboard with these fields:

| Field | Value |
|---|---|
| Type | Web service |
| Name | `rwa-transparency-api` |
| Runtime | Python |
| Plan | Free |
| Branch | `main` |
| Build command | `pip install -r requirements.txt` |
| Start command | `python -m rwa_score.api` |
| Health check path | `/health` |

`python -m rwa_score.api` runs uvicorn on `0.0.0.0:$PORT` (`PORT` is set by
Render). The worker thread starts in that process. Put the attester key on
**this** service only, not on the public scorecard.

Free-plan tradeoffs of the second service: it spins down after about 15
minutes idle, so the first `/v1` call after sleep is a cold start. The sqlite
file (`RWA_API_DB_PATH`) is on ephemeral disk. Spin-down deletes stored
payloads, the pending queue, and API keys that exist only in that file. The
in-process worker stops with the process, so a sleeping service does not
send. A Render background worker, or a paid plan with a persistent disk,
would keep the queue across sleep. That service is not added here.

Alternative: mount FastAPI on the existing scorecard process, the way Tornado
already serves `/health` ahead of the Streamlit catch-all. That is not
implemented. Until those routes are registered on that process, `/v1` stays
HTML. Doing the mount later would put the attester key in the public website
process and would still lose the sqlite file on the same free-plan spin-down.
Keep the scorecard start command as it is.

After the API service is up, `GET /health` on **its** URL is JSON and
includes `"api": true`. The scorecard URL does not.

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

`GET /v1/attest/{ticker}` scores again on every call. Do not poll it.
`GET /v1/attest/{ticker}/status` reads the latest stored job and does not
score or enqueue.

## Render settings (API service)

Set these on `rwa-transparency-api`. Example shapes are not real values.
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
| `RWA_ATTEST_VALUE_CAP_WEI` | Max wei attached to `attest`. Default `0` | `0` | no |
| `RWA_ATTEST_GAS_LIMIT` | Gas cap. Default `300000` | `300000` | no |
| `RWA_ATTEST_MAX_ATTEMPTS` | Retries before the job is failed. Default `5` | `5` | no |
| `RWA_ATTEST_BACKOFF_SECONDS` | Base delay between retries. Default `2.0` | `2.0` | no |
| `RWA_API_DB_PATH` | SQLite file for payloads, jobs, and keys. Default `data/rat_api.sqlite` | `data/rat_api.sqlite` | no |
| `RWA_API_BOOTSTRAP_KEY` | Paid key recreated on boot so `/v1/attest` works after spin-down wipes sqlite | `rat_` plus a long random token | yes |
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
| `RWA_API_DATABASE_URL` | Read and ignored. Store does not open Postgres. Leave unset | `postgresql://user:pass@host/db` | yes |
| `DATABASE_URL` | Same unused hook. Leave unset | same | yes |
| `HOST` | Bind address. Default `0.0.0.0`. Leave unset on Render | `0.0.0.0` | no |
| `PORT` | Render injects this. Do not set it | `10000` | no |
| `RENDER_GIT_COMMIT` | Render injects the full git SHA. `scorer_version` reads it first | 40 hex characters | no |

`SOURCE_VERSION` and `GIT_COMMIT` do not change `scorer_version`. `/v1/score`
and `/v1/attest` do not read `XAI_API_KEY`.

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

## 6. Live end-to-end check (Spencer only, Base Sepolia)

Do this after steps 1–3, with the API service up, `isAttester` true, and
`attestationFee` `0`. Run it in one sitting, before the free service spins
down. Spin-down deletes the sqlite file, and `verify` then reports nothing
stored even if the transaction already landed.

Agents do not run this section. It sends a transaction from the key you set
on Render.

```bash
export API_BASE="https://<rwa-transparency-api host>"
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

### 6.3 Enqueue one attest, then read `on_chain`

Call attest **once**:

```bash
curl -sS -H "X-API-Key: $API_KEY" "$API_BASE/v1/attest/NVDA" -o /tmp/nvda-attest.json
python3 - <<'PY'
import json
body = json.load(open("/tmp/nvda-attest.json"))
oc = body["on_chain"]
assert body["stored"] is True
assert body["chain_id"] == 84532
assert body["contract"] == "0x2F073a3628D498d92956e7eFE2b26633eDa75b00"
assert oc["worker"] == "enabled"
print(body["score_hash"], oc)
PY
```

Expected first body: `on_chain.worker` is `enabled`. `attested` may still be
`false`, `tx` `null`, `attestedAt` `null`, `status` `pending`, because the
send is off the response. `score_hash` is `0x` plus 64 hex characters.

Poll status (this does not score again):

```bash
python3 - <<'PY'
import json, os, time, urllib.request
url = os.environ["API_BASE"].rstrip("/") + "/v1/attest/NVDA/status"
req = urllib.request.Request(url, headers={"X-API-Key": os.environ["API_KEY"]})
for i in range(12):
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.load(resp)
    oc = body["on_chain"]
    print(i, oc)
    if oc.get("attested") is True and oc.get("tx") and oc.get("attestedAt") is not None:
        json.dump(body, open("/tmp/nvda-status.json", "w"))
        raise SystemExit(0)
    time.sleep(5)
raise SystemExit("on_chain did not confirm")
PY
```

Expected: the script exits 0. The last printed dict has `"attested": true`,
`"tx"` of `0x` plus 64 hex characters, `"attestedAt"` an integer,
`"worker": "enabled"`, and `"status": "confirmed"`. Save the tx:

```bash
export TX="$(python3 -c 'import json; print(json.load(open("/tmp/nvda-status.json"))["on_chain"]["tx"])')"
export SCORE_HASH="$(python3 -c 'import json; print(json.load(open("/tmp/nvda-status.json"))["score_hash"])')"
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

### 6.5 `verify.py` against the stored payload

The stored bytes are on the API service disk, not on your laptop. Open the
Render shell for `rwa-transparency-api` (same instance, before spin-down)
and run:

```bash
python -m rwa_score.api.verify NVDA --json \
  --contract 0x2F073a3628D498d92956e7eFE2b26633eDa75b00 \
  --rpc-url "$BASE_SEPOLIA_RPC_URL"
```

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

`match` is `true` only when the stored bytes recompute to that hash and
on-chain `verify` returns ok. A laptop run against an empty local sqlite
file prints `"stored": false` and `"match": null`. That is the wrong machine.
