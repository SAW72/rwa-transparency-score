# RWA market data fix: reproduction

This file reproduces the two problems fixed on top of head `8fbe0746` and shows the result after the fix. It runs offline. It uses a fake clock, a fake sleeper, and a fake transport. It needs no network and no key.

1. `parse_retry_after` raised `ValueError` on a non-ASCII digit string such as `²`. It also read Arabic-Indic digits as numbers.
2. After CoinGecko and the CoinMarketCap fallback both failed, every page call tried both hosts again and slept again. A page that made 11 quote calls in an outage slept 220 seconds in the fake-clock run.

## Probe

Save as `probe.py`. Run it from the repository root with the repo's Python: `python - < probe.py`. Piping on stdin keeps the repository root on the import path.

```python
"""Offline probe for the CoinGecko client. Fake clock, fake sleeper, fake transport.
No network, no keys. Run from the repo root:  python - < probe.py
"""
import json, tempfile
from pathlib import Path
from rwa_score import coingecko_client as cg


class Clock:
    def __init__(self):
        self.now, self.slept = 1000.0, []
    def __call__(self):
        return self.now
    def sleep(self, s):
        self.slept.append(s)
        self.now += s


class Resp:
    status_code = 429
    headers = {"Retry-After": "9999"}
    text = ""
    def json(self):
        raise ValueError("not json")


class Session:
    def __init__(self):
        self.calls = []
    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append("cmc" if "coinmarketcap" in url else "coingecko")
        return Resp()


# Probe 1: Retry-After with a superscript two.
try:
    print("parse_retry_after('\u00b2') ->", repr(cg.parse_retry_after("\u00b2")))
except Exception as exc:
    print("parse_retry_after('\u00b2') raised", type(exc).__name__ + ":", exc)

# Probe 2: 11 page calls during a total outage (429 + Retry-After 9999, both hosts).
rows = [{"rwa_id": 1, "name": "PAX Gold", "symbol": "PAXG", "coingecko_id": "pax-gold", "asset_class": "unknown"}]
path = Path(tempfile.mkdtemp()) / "allowlist.json"
path.write_text(json.dumps({"assets": rows}))
clock, session = Clock(), Session()
client = cg.CoinGeckoClient(api_key="", session=session, allowlist_path=path, sleeper=clock.sleep, clock=clock)
row = client._allowlist_rows()[0]
for _ in range(11):
    client._quote_for(row)
print("11-call outage: transport calls =", len(session.calls), "| total sleep =", sum(clock.slept), "s")
print("cooldown constant:", getattr(cg, "COOLDOWN_SECONDS", "absent"))
```

## Output before the fix (head `8fbe0746`)

```text
parse_retry_after('²') raised ValueError: invalid literal for int() with base 10: '²'
11-call outage: transport calls = 44 | total sleep = 220.0 s
cooldown constant: absent
```

## Output after the fix

```text
parse_retry_after('²') -> None
11-call outage: transport calls = 4 | total sleep = 20.0 s
cooldown constant: 45.0
```

## What the numbers mean

| Check | Before | After |
|---|---|---|
| `parse_retry_after('²')` | raises `ValueError` | `None` (falls to the 1s / 2s / 4s schedule) |
| Transport calls for 11 calls in an outage | 44 | 4 (one failed cycle) |
| Total sleep for 11 calls in an outage | 220 s | 20 s (2 hosts x 10 s cap) |

The after run does one full failed cycle, then serves the stale or empty result for 45 seconds (`COOLDOWN_SECONDS`) with no network call and no sleep. After 45 seconds the next call tries again. A success from either host clears the cool-down.

The tests are in `tests/test_coingecko_client.py`: the `retry_after` tests and the `cooldown` tests.
