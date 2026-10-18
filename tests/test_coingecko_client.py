"""CoinGecko allowlist client. No live network. Transport is monkeypatched."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

from rwa_score.coingecko_client import (
    CMC_KEYLESS_QUOTES_URL,
    COINGECKO_BASE,
    COOLDOWN_SECONDS,
    DEMO_KEY_HEADER,
    KEYLESS_NOTE,
    MARKETS_PATH,
    NOT_AVAILABLE,
    CoinGeckoClient,
    assess_market_row,
    load_allowlist,
    parse_retry_after,
)
from rwa_score.scorer import DEFAULT_PRICE_SCORE, TransparencyScorer

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "cg-sentinel-do-not-leak-7f3a"
PACKAGE = ROOT / "rwa_score"


def _market(
    cg_id: str,
    symbol: str,
    *,
    price: float = 10.0,
    market_cap: float = 1_000_000.0,
    volume: float = 50_000.0,
    supply: float | None = None,
    change: float = 1.0,
) -> dict:
    if supply is None:
        supply = market_cap / price
    return {
        "id": cg_id,
        "symbol": symbol.lower(),
        "name": cg_id,
        "current_price": price,
        "market_cap": market_cap,
        "total_volume": volume,
        "circulating_supply": supply,
        "price_change_percentage_24h_in_currency": change,
        "last_updated": "2026-10-18T02:00:00.000Z",
    }


def _cmc_quote(symbol: str, *, price: float, market_cap: float, volume: float) -> dict:
    return {
        "data": [
            {
                "symbol": symbol,
                "quote": [
                    {
                        "symbol": "USD",
                        "price": price,
                        "market_cap": market_cap,
                        "volume_24h": volume,
                    }
                ],
            }
        ]
    }


class FakeResponse:
    def __init__(self, status_code: int, payload=None, headers=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return json.loads(json.dumps(self._payload))


class FakeSession:
    def __init__(self, routes: list, default: FakeResponse | None = None):
        self.routes = list(routes)
        self.calls: list[dict] = []
        self.default = default

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "params": dict(params or {}), "headers": dict(headers or {}), "timeout": timeout}
        )
        if not self.routes:
            if self.default is not None:
                return self.default
            raise AssertionError(f"unexpected GET {url}")
        item = self.routes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _allowlist(tmp_path: Path) -> Path:
    rows = [
        {
            "rwa_id": 1,
            "name": "PAX Gold",
            "symbol": "PAXG",
            "chain": "ethereum",
            "contract_address": None,
            "coingecko_id": "pax-gold",
            "asset_class": "unknown",
        },
        {
            "rwa_id": 8,
            "name": "Figure Heloc",
            "symbol": "FIGR_HELOC",
            "chain": None,
            "contract_address": None,
            "coingecko_id": "figure-heloc",
            "asset_class": "unknown",
        },
    ]
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps({"assets": rows}), encoding="utf-8")
    return path


def _client(tmp_path: Path, session: FakeSession, **kwargs) -> CoinGeckoClient:
    return CoinGeckoClient(
        session=session,
        allowlist_path=_allowlist(tmp_path),
        sleeper=kwargs.pop("sleeper", lambda _s: None),
        api_key=kwargs.pop("api_key", ""),
        **kwargs,
    )


def test_allowlist_ids_are_hand_kept_and_unclassified() -> None:
    rows = load_allowlist()
    ids = [row["coingecko_id"] for row in rows]
    assert "pax-gold" in ids
    assert "figure-heloc" in ids
    assert "chainlink" not in ids
    assert "stellar" not in ids
    assert all(row["asset_class"] == "unknown" for row in rows)
    blob = (ROOT / "rwa_score" / "data" / "rwa_allowlist.json").read_text(encoding="utf-8")
    assert "real-world-assets-rwa" not in blob
    assert "category" not in blob


def test_markets_parse_into_existing_shapes(tmp_path: Path) -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                [
                    _market("pax-gold", "paxg", price=100.0, market_cap=1_000_000.0),
                    _market("figure-heloc", "figr_heloc", price=1.0, market_cap=100.0, supply=100.0),
                ],
            )
        ]
    )
    client = _client(tmp_path, session)
    quotes = client.rwa_quotes(symbol="PAXG")
    assert quotes["symbol"] == "PAXG"
    assert quotes["average_tokenized_price"] == 100.0
    assert quotes["tokenized_market_cap"] == 1_000_000.0
    assert quotes["tokenized_volume_24h"] == 50_000.0
    assert quotes["tokens"] == []
    assert quotes["tradfi_markets"] == []
    assert quotes["tradfi_markets_status"] == NOT_AVAILABLE
    assert quotes["rwa_rank"] is None
    assert quotes["rwa_rank_status"] == NOT_AVAILABLE
    assert quotes["market_source"] == "coingecko"
    params = session.calls[0]["params"]
    assert params["vs_currency"] == "usd"
    assert "pax-gold" in params["ids"]
    assert "category" not in params
    assert params["price_change_percentage"] == "24h"
    assert session.calls[0]["url"] == f"{COINGECKO_BASE}{MARKETS_PATH}"
    info = client.rwa_info(1)
    assert info["cik"] is None
    assert info["cik_status"] == NOT_AVAILABLE
    assert info["holders_status"] == NOT_AVAILABLE
    assert info["issuer_backing_status"] == NOT_AVAILABLE
    pairs = client.market_pairs(symbol="PAXG")
    assert pairs["market_pairs"] == []
    assert pairs["unavailable_reason"] == NOT_AVAILABLE
    assert pairs["plan_blocked"] is False
    listed = client.assets_list()
    assert {row["symbol"] for row in listed["rwa_assets"]} == {"PAXG", "FIGR_HELOC"}
    assert all(row["rwa_rank"] is None for row in listed["rwa_assets"])


def test_cache_hit_expiry_and_stale(tmp_path: Path) -> None:
    clock = {"t": 1_000.0}
    good = FakeResponse(
        200,
        [
            _market("pax-gold", "paxg", price=10.0, market_cap=1000.0, supply=100.0),
            _market("figure-heloc", "figr_heloc", price=1.0, market_cap=10.0, supply=10.0),
        ],
    )
    session = FakeSession([good, FakeResponse(500), _cmc_boom()])
    client = _client(
        tmp_path,
        session,
        clock=lambda: clock["t"],
        quote_ttl=600.0,
        max_retries=0,
        now_iso=lambda: "2026-10-18T02:00:00Z",
    )
    first = client.rwa_quotes(symbol="PAXG")
    second = client.rwa_quotes(symbol="PAXG")
    assert first["average_tokenized_price"] == second["average_tokenized_price"] == 10.0
    assert len(session.calls) == 1
    clock["t"] = 1_700.0
    stale = client.rwa_quotes(symbol="PAXG")
    assert stale["stale"] is True
    assert stale["average_tokenized_price"] == 10.0
    assert "as of" in client.provider_notice()
    assert stale["as_of"]


def _cmc_boom() -> FakeResponse:
    return FakeResponse(503)


def test_retry_after_parser_cases() -> None:
    now = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc)
    assert parse_retry_after("2") == 2.0
    assert parse_retry_after(2) == 2.0
    assert parse_retry_after("1.5") is None
    assert parse_retry_after("-3") is None
    assert parse_retry_after(-3) is None
    assert parse_retry_after(["2"]) is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("") is None
    future = format_datetime(now + timedelta(seconds=4), usegmt=True)
    parsed = parse_retry_after(future, now=now)
    assert parsed is not None and 3.0 <= parsed <= 5.0
    naive = "Sunday, 18-Oct-26 03:00:05"
    assert parse_retry_after(naive, now=now) is None or parse_retry_after("not a date") is None


def test_429_digits_http_date_garbage_and_cap(tmp_path: Path) -> None:
    slept: list[float] = []
    markets = [
        _market("pax-gold", "paxg", price=10.0, market_cap=1000.0, supply=100.0),
        _market("figure-heloc", "figr_heloc", price=1.0, market_cap=10.0, supply=10.0),
    ]
    session = FakeSession(
        [
            FakeResponse(429, {"error": "slow"}, headers={"Retry-After": "2"}),
            FakeResponse(200, markets),
        ]
    )
    client = _client(tmp_path, session, sleeper=slept.append)
    assert client.rwa_quotes(symbol="PAXG")["average_tokenized_price"] == 10.0
    assert slept == [2.0]

    slept.clear()
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=3), usegmt=True)
    session = FakeSession(
        [
            FakeResponse(429, {}, headers={"Retry-After": when}),
            FakeResponse(200, markets),
        ]
    )
    client = _client(tmp_path, session, sleeper=slept.append)
    client.rwa_quotes(symbol="PAXG")
    assert slept and slept[0] <= 10.0

    for bad in ("1.5", "-4", "nope"):
        slept.clear()
        session = FakeSession(
            [
                FakeResponse(429, {}, headers={"Retry-After": bad}),
                FakeResponse(200, markets),
            ]
        )
        client = _client(tmp_path, session, sleeper=slept.append)
        client.rwa_quotes(symbol="PAXG")
        assert slept == [1.0]

    slept.clear()
    session = FakeSession(
        [FakeResponse(429, {}, headers={"Retry-After": ["9"]}), FakeResponse(200, markets)]
    )
    client = _client(tmp_path, session, sleeper=slept.append)
    client.rwa_quotes(symbol="PAXG")
    assert slept == [1.0]

    slept.clear()
    session = FakeSession(
        [
            FakeResponse(429, {}, headers={"Retry-After": "100"}),
            FakeResponse(429, {}, headers={"Retry-After": "100"}),
        ],
        default=FakeResponse(503),
    )
    client = _client(tmp_path, session, sleeper=slept.append, max_retries=3)
    rows = client.assets_list()
    assert slept
    assert slept[0] == 10.0
    assert all(item <= 10.0 for item in slept)
    assert 100.0 not in slept
    assert rows["rwa_assets"]
    assert all(row["unverified"] for row in rows["rwa_assets"])


def test_page_does_not_crash_on_429_5xx_or_network(tmp_path: Path) -> None:
    import requests

    for failure in (
        FakeResponse(429, {}, headers={"Retry-After": "1"}),
        FakeResponse(502),
        requests.ConnectionError("down"),
    ):
        session = FakeSession([failure, failure, failure, failure, _cmc_boom(), _cmc_boom(), _cmc_boom(), _cmc_boom()])
        client = _client(tmp_path, session, sleeper=lambda _s: None, max_retries=1)
        quotes = client.rwa_quotes(symbol="PAXG")
        assert quotes["symbol"] == "PAXG"
        assert quotes["unverified"] is True


def test_cmc_fallback_sends_no_key_header(tmp_path: Path) -> None:
    session = FakeSession(
        [
            FakeResponse(503),
            FakeResponse(503),
            FakeResponse(
                200,
                {
                    "data": [
                        {
                            "symbol": "PAXG",
                            "quote": [
                                {"symbol": "USD", "price": 20.0, "market_cap": 2000.0, "volume_24h": 5.0}
                            ],
                        },
                        {
                            "symbol": "FIGR_HELOC",
                            "quote": [
                                {"symbol": "USD", "price": 1.0, "market_cap": 10.0, "volume_24h": 1.0}
                            ],
                        },
                    ]
                },
            ),
        ]
    )
    client = _client(tmp_path, session, api_key=SENTINEL, max_retries=1, sleeper=lambda _s: None)
    quotes = client.rwa_quotes(symbol="PAXG")
    assert quotes["market_source"] == "cmc_keyless"
    assert quotes["average_tokenized_price"] == 20.0
    cmc_calls = [call for call in session.calls if call["url"] == CMC_KEYLESS_QUOTES_URL]
    assert cmc_calls
    for call in cmc_calls:
        headers = {key.lower(): value for key, value in call["headers"].items()}
        assert "x-cmc_pro_api_key" not in headers
        assert DEMO_KEY_HEADER not in headers
        assert SENTINEL not in json.dumps(call)
        assert "api_key" not in call["params"]
    cg_calls = [call for call in session.calls if "coingecko" in call["url"]]
    assert cg_calls
    assert cg_calls[0]["headers"][DEMO_KEY_HEADER] == SENTINEL


def test_missing_key_is_keyless_with_visible_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COINGECKO_DEMO_API_KEY", raising=False)
    session = FakeSession(
        [
            FakeResponse(
                200,
                [
                    _market("pax-gold", "paxg", price=3.0, market_cap=300.0, supply=100.0),
                    _market("figure-heloc", "figr_heloc", price=1.0, market_cap=10.0, supply=10.0),
                ],
            )
        ]
    )
    client = CoinGeckoClient(
        session=session,
        allowlist_path=_allowlist(tmp_path),
        sleeper=lambda _s: None,
    )
    assert client.keyless is True
    assert KEYLESS_NOTE in client.provider_notice()
    client.rwa_quotes(symbol="PAXG")
    headers = {key.lower(): value for key, value in session.calls[0]["headers"].items()}
    assert DEMO_KEY_HEADER not in headers


def test_key_never_in_logs_exceptions_repr_or_notice(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session = FakeSession([FakeResponse(400, {"echo": SENTINEL})])
    client = _client(tmp_path, session, api_key=SENTINEL, max_retries=0)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as excinfo:
            client._request("https://api.coingecko.com/api/v3/coins/markets", {}, host="coingecko")
    blob = " ".join(
        [
            str(excinfo.value),
            repr(excinfo.value),
            repr(client),
            client.provider_notice(),
            caplog.text,
        ]
    )
    assert SENTINEL not in blob
    assert SENTINEL not in client.last_error


def test_sanity_rules_and_figure_heloc_fixture() -> None:
    tiny_total = 1_000_000.0
    assert "market_cap_missing_or_non_positive" in assess_market_row(
        {"market_cap": None, "price": 1.0}, allowlist_total=tiny_total
    )
    assert "market_cap_missing_or_non_positive" in assess_market_row(
        {"market_cap": 0, "price": 1.0}, allowlist_total=tiny_total
    )
    assert "market_cap_missing_or_non_positive" in assess_market_row(
        {"market_cap": -5, "price": 1.0}, allowlist_total=tiny_total
    )
    assert "price_missing_or_non_positive" in assess_market_row(
        {"market_cap": 100.0, "price": 0}, allowlist_total=tiny_total
    )
    assert "supply_inconsistent" in assess_market_row(
        {"market_cap": 100.0, "price": 10.0, "circulating_supply": 2.0},
        allowlist_total=10_000.0,
    )
    assert "market_cap_above_ceiling" in assess_market_row(
        {"market_cap": 25_600_000_000.0, "price": 1.067, "circulating_supply": 24_010_566_736.0},
        allowlist_total=39_240_000_000.0,
    )
    reasons = assess_market_row(
        {"market_cap": 25_600_000_000.0, "price": 1.067, "circulating_supply": 24_010_566_736.0},
        allowlist_total=30_000_000_000.0,
    )
    assert "market_cap_above_ceiling" in reasons
    assert "allowlist_share_implausible" in reasons
    assert "supply_inconsistent" not in reasons
    assert "source_disagreement" in assess_market_row(
        {"market_cap": 100.0, "price": 10.0, "circulating_supply": 10.0},
        allowlist_total=10_000.0,
        other_market_cap=200.0,
    )
    clean = assess_market_row(
        {"market_cap": 100.0, "price": 10.0, "circulating_supply": 10.0},
        allowlist_total=10_000.0,
        other_market_cap=105.0,
    )
    assert clean == []


def test_figure_heloc_row_is_unverified_and_not_scored_as_pass(tmp_path: Path) -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                [
                    _market("pax-gold", "paxg", price=100.0, market_cap=1_000_000.0, supply=10_000.0),
                    _market(
                        "figure-heloc",
                        "figr_heloc",
                        price=1.067,
                        market_cap=25_600_000_000.0,
                        supply=24_010_566_736.0,
                        volume=1_000_000.0,
                    ),
                ],
            )
        ]
    )
    client = _client(tmp_path, session)
    listed = client.assets_list()
    figure = next(row for row in listed["rwa_assets"] if row["symbol"] == "FIGR_HELOC")
    paxg = next(row for row in listed["rwa_assets"] if row["symbol"] == "PAXG")
    assert figure["unverified"] is True
    assert "market_cap_above_ceiling" in figure["unverified_reasons"]
    assert paxg["unverified"] is False
    assert client.score_feeding_market_cap_total() == pytest.approx(1_000_000.0)
    report = TransparencyScorer(client).score("FIGR_HELOC")
    assert report["price"]["unverified"] is True
    assert report["price"]["available"] is False
    assert report["subscores"]["price"] == DEFAULT_PRICE_SCORE
    assert "not a pass" in report["explanations"]["price"].lower()
    info_line = report["explanations"]["disclosure"]
    assert "not available" in info_line.lower()


def test_no_keyed_cmc_left_in_package_source() -> None:
    blob = "\n".join(path.read_text(encoding="utf-8") for path in PACKAGE.rglob("*.py"))
    assert "X-CMC_PRO_API_KEY" not in blob
    assert "CMC_API_KEY" not in blob
    assert CMC_KEYLESS_QUOTES_URL in blob
    lowered = blob.lower()
    assert "x-cmc_pro_api_key" not in lowered
    # The only coinmarketcap host in package code is the keyless public quotes URL.
    assert lowered.count("pro-api.coinmarketcap.com") == lowered.count(
        "pro-api.coinmarketcap.com/public-api/v3/cryptocurrency/quotes/latest"
    )


def test_render_and_env_example_have_no_cmc_key_name() -> None:
    render = (ROOT / "render.yaml").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "CMC_API_KEY" not in render
    assert "CMC_API_KEY" not in example
    assert "COINGECKO_DEMO_API_KEY" in render
    assert "COINGECKO_DEMO_API_KEY" in example
    assert "plan: free" in render


# --- Fix 1: Retry-After accepts ASCII digits only ---------------------------


@pytest.mark.parametrize(
    "raw",
    ["²", "٣", "１２", "³", "", " ", "3.5", "5, 10", "+5", "-1"],
)
def test_retry_after_rejects_non_ascii_digits_and_junk_without_raising(raw: str) -> None:
    assert parse_retry_after(raw) is None


def test_retry_after_still_accepts_ascii_digits_and_http_date() -> None:
    now = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc)
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after(" 7 ") == 7.0
    assert parse_retry_after("0") == 0.0
    when = format_datetime(now + timedelta(seconds=4), usegmt=True)
    assert parse_retry_after(when, now=now) == 4.0


def test_429_with_superscript_retry_after_does_not_raise_out_of_client(tmp_path: Path) -> None:
    clock = _FakeClock()
    session = _HostSession(
        lambda host: FakeResponse(429, {}, headers={"Retry-After": "²"})
        if host == "coingecko"
        else FakeResponse(200, _cmc_quote("PAXG", price=10.0, market_cap=1000.0, volume=5.0))
    )
    client = _client(tmp_path, session, sleeper=clock.sleep, clock=clock)
    quotes = client.rwa_quotes(symbol="PAXG")
    assert quotes["market_source"] == "cmc_keyless"
    assert quotes["average_tokenized_price"] == 10.0
    # The bad header falls to the 1s / 2s / 4s schedule, not to zero or a crash.
    assert clock.slept and clock.slept[0] == 1.0


# --- Fix 2: cool-down after total failure -----------------------------------


class _FakeClock:
    """Injectable monotonic clock. Sleeping advances it, like real time."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _HostSession:
    """Fake transport. ``respond(host)`` returns a FakeResponse and may be swapped."""

    def __init__(self, respond) -> None:
        self.respond = respond
        self.calls: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        host = "cmc" if "coinmarketcap" in url else "coingecko"
        self.calls.append(host)
        return self.respond(host)


def _both_429(secret: str = ""):
    def respond(_host: str) -> FakeResponse:
        return FakeResponse(429, {"echo": secret}, headers={"Retry-After": "9999"}, text=secret)

    return respond


def test_cooldown_constant_is_45_seconds() -> None:
    assert COOLDOWN_SECONDS == 45


def test_outage_burst_makes_zero_calls_and_zero_sleep_inside_cooldown(tmp_path: Path) -> None:
    clock = _FakeClock()
    session = _HostSession(_both_429())
    client = _client(tmp_path, session, sleeper=clock.sleep, clock=clock)
    rows = client._allowlist_rows()
    first = client._quote_for(rows[0])
    assert first["unverified"] is True
    first_calls = len(session.calls)
    first_sleep = sum(clock.slept)
    assert 0 < first_calls
    assert first_sleep <= 20.0
    for i in range(10):
        row = rows[i % len(rows)]
        if i % 3 == 0:
            client._quote_for(row)
        elif i % 3 == 1:
            client.crypto_quote(row["rwa_id"])
        else:
            client.rwa_quotes(symbol=row["symbol"])
    assert len(session.calls) == first_calls
    assert sum(clock.slept) == first_sleep
    assert sum(clock.slept) <= 20.0


def test_cooldown_expires_after_45_seconds_and_retries(tmp_path: Path) -> None:
    clock = _FakeClock()
    session = _HostSession(_both_429())
    client = _client(tmp_path, session, sleeper=clock.sleep, clock=clock)
    row = client._allowlist_rows()[0]
    client._quote_for(row)
    after_first = len(session.calls)
    clock.advance(COOLDOWN_SECONDS - 1)
    client._quote_for(row)
    assert len(session.calls) == after_first
    clock.advance(1)
    client._quote_for(row)
    assert len(session.calls) > after_first


def test_success_from_either_host_clears_cooldown(tmp_path: Path) -> None:
    clock = _FakeClock()
    markets = [_market("pax-gold", "paxg"), _market("figure-heloc", "figr_heloc")]
    mode = {"cg": "fail"}

    def respond(host: str) -> FakeResponse:
        if host == "coingecko":
            if mode["cg"] == "ok":
                return FakeResponse(200, markets)
            return FakeResponse(429, {}, headers={"Retry-After": "9999"})
        return FakeResponse(429, {}, headers={"Retry-After": "9999"})

    session = _HostSession(respond)
    client = _client(tmp_path, session, sleeper=clock.sleep, clock=clock)
    row = client._allowlist_rows()[0]
    client._quote_for(row)
    assert client._total_failure_at is not None
    clock.advance(COOLDOWN_SECONDS)
    mode["cg"] = "ok"
    found = client._quote_for(row)
    assert found["market_source"] == "coingecko" and found["stale"] is False
    assert client._total_failure_at is None

    # Same for the CMC fallback host.
    clock2 = _FakeClock()
    cmc_ok = {"on": False}

    def respond2(host: str) -> FakeResponse:
        if host == "cmc" and cmc_ok["on"]:
            return FakeResponse(
                200,
                {
                    "data": [
                        {
                            "symbol": "PAXG",
                            "quote": [{"symbol": "USD", "price": 10.0, "market_cap": 1000.0, "volume_24h": 5.0}],
                        }
                    ]
                },
            )
        return FakeResponse(429, {}, headers={"Retry-After": "9999"})

    client2 = _client(tmp_path, _HostSession(respond2), sleeper=clock2.sleep, clock=clock2)
    client2._quote_for(client2._allowlist_rows()[0])
    assert client2._total_failure_at is not None
    clock2.advance(COOLDOWN_SECONDS)
    cmc_ok["on"] = True
    assert client2._quote_for(client2._allowlist_rows()[0])["market_source"] == "cmc_keyless"
    assert client2._total_failure_at is None


def test_stale_data_still_served_with_stale_flag_during_cooldown(tmp_path: Path) -> None:
    clock = _FakeClock()
    markets = [_market("pax-gold", "paxg", price=10.0, market_cap=1000.0, supply=100.0), _market("figure-heloc", "figr_heloc", price=1.0, market_cap=10.0, supply=10.0)]
    state = {"up": True}

    def respond(_host: str) -> FakeResponse:
        if state["up"]:
            return FakeResponse(200, markets)
        return FakeResponse(429, {}, headers={"Retry-After": "9999"})

    session = _HostSession(respond)
    client = _client(tmp_path, session, sleeper=clock.sleep, clock=clock, now_iso=lambda: "2026-10-18T03:00:00Z")
    row = client._allowlist_rows()[0]
    assert client._quote_for(row)["stale"] is False
    state["up"] = False
    clock.advance(client.quote_ttl + 1)
    during = client._quote_for(row)  # total failure, stale cache, starts cool-down
    assert during["stale"] is True and during["price"] == 10.0
    calls = len(session.calls)
    slept = sum(clock.slept)
    for _ in range(5):
        again = client.crypto_quote(row["rwa_id"])
        assert again["stale"] is True
        assert again["quote"]["USD"]["price"] == 10.0
        assert again["as_of"] == "2026-10-18T03:00:00Z"
    assert len(session.calls) == calls
    assert sum(clock.slept) == slept
    assert "as of 2026-10-18T03:00:00Z" in client.provider_notice()


def test_key_never_in_errors_or_repr_during_cooldown(tmp_path: Path) -> None:
    clock = _FakeClock()
    session = _HostSession(_both_429(SENTINEL))
    client = _client(tmp_path, session, sleeper=clock.sleep, clock=clock, api_key=SENTINEL)
    rows = client._allowlist_rows()
    client._quote_for(rows[0])
    calls = len(session.calls)
    blobs = []
    for row in rows:
        blobs.append(repr(client._quote_for(row)))
        blobs.append(repr(client.crypto_quote(row["rwa_id"])))
    blobs += [client.last_error, repr(client), client.provider_notice()]
    assert len(session.calls) == calls
    assert SENTINEL not in " ".join(blobs)
    for call in session.calls:
        assert call in {"cmc", "coingecko"}
