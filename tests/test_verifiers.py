"""Unit tests for attestation / PoR verifiers (mocked HTTP / JSON-RPC)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from rwa_score.chainlink_por import (
    BACKED_POR_FEEDS,
    LATEST_ROUND_DATA_SELECTOR,
    TOTAL_SUPPLY_SELECTOR,
    decode_latest_round,
    resolve_por_feed,
    scale_answer,
)
from rwa_score.verifiers import (
    DINARI_DSHARES_URL,
    BackedVerifier,
    DinariVerifier,
    RobinhoodVerifier,
    VerificationLevel,
    por_score_from_ratio,
    resolve_verifier_id,
)


def encode_uint256(value: int) -> str:
    if value < 0:
        value = (1 << 256) + value
    return f"{value:064x}"


def encode_latest_round(
    *,
    round_id: int = 7,
    answer: int,
    started_at: int = 1,
    updated_at: int = 1_700_000_000,
    answered_in_round: int = 7,
) -> str:
    return "0x" + "".join(
        encode_uint256(n)
        for n in (round_id, answer, started_at, updated_at, answered_in_round)
    )


def encode_uint(value: int) -> str:
    return "0x" + encode_uint256(value)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict[str, Any] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("no json")
        return dict(self._payload)


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse] | list[FakeResponse]) -> None:
        if isinstance(responses, list):
            self._queue = list(responses)
            self._by_url: dict[str, FakeResponse] = {}
        else:
            self._queue = []
            self._by_url = dict(responses)
        self.calls: list[str] = []

    def get(self, url: str, timeout: float | None = None, headers: dict | None = None):
        self.calls.append(url)
        if self._by_url:
            if url in self._by_url:
                return self._by_url[url]
            lower = url.lower()
            for key, resp in self._by_url.items():
                if lower == key.lower() or lower.endswith(key.lower().rsplit("/", 1)[-1]):
                    if url.rsplit("/", 1)[-1] == key.rsplit("/", 1)[-1]:
                        return resp
            for key, resp in self._by_url.items():
                if lower == key.lower() or url.rsplit("/", 1)[-1].lower() == key.rsplit("/", 1)[-1].lower():
                    return resp
            raise AssertionError(f"unexpected URL {url}")
        if not self._queue:
            raise AssertionError(f"no queued response for {url}")
        return self._queue.pop(0)


class FakeRpcSession:
    """JSON-RPC ``eth_call`` mock keyed by contract ``to`` address."""

    def __init__(self, results: dict[str, str] | Exception) -> None:
        self._results = results
        self.calls: list[tuple[str, str, str]] = []

    def post(self, url: str, json: dict | None = None, timeout: float | None = None, headers: dict | None = None):
        payload = json or {}
        params = (payload.get("params") or [{}])[0]
        to = str(params.get("to") or "")
        data = str(params.get("data") or "")
        self.calls.append((url, to, data))
        if isinstance(self._results, Exception):
            raise self._results
        key = to.lower()
        if key not in self._results:
            raise AssertionError(f"unexpected eth_call to {to} data={data}")
        return FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "result": self._results[key]})


def _bnvda_rpc(*, reserves: int = 1000, circulating: int = 1000) -> FakeRpcSession:
    feed = resolve_por_feed("NVDA")
    assert feed is not None
    return FakeRpcSession(
        {
            feed.proxy.lower(): encode_latest_round(answer=reserves * 10**feed.decimals),
            (feed.token_address or "").lower(): encode_uint(circulating * 10**feed.token_decimals),
        }
    )


@pytest.mark.parametrize(
    "ratio,expected",
    [(1.0, 95.0), (0.999, 95.0), (0.995, 80.0), (0.99, 80.0), (0.96, 60.0), (0.95, 60.0), (0.94, 30.0)],
)
def test_por_score_thresholds(ratio: float, expected: float) -> None:
    assert por_score_from_ratio(ratio) == expected


def test_resolve_verifier_keywords() -> None:
    assert resolve_verifier_id("Backed Finance") == "backed"
    assert resolve_verifier_id("xStocks") == "backed"
    assert resolve_verifier_id("Dinari Securities") == "dinari"
    assert resolve_verifier_id("Robinhood") == "robinhood"
    assert resolve_verifier_id("Robinhood Assets") == "robinhood"
    assert resolve_verifier_id("NoteVault Demo Issuer") is None


def test_resolve_por_feed_aliases() -> None:
    feed = resolve_por_feed("NVDA")
    assert feed is not None
    assert feed.symbol == "bNVDA"
    assert resolve_por_feed("NVDAx") == feed
    assert resolve_por_feed("bNVDA") == feed
    assert resolve_por_feed("AAPL") is None
    assert resolve_por_feed("TSLA") is None


def test_decode_latest_round_and_scale() -> None:
    raw = encode_latest_round(round_id=3, answer=1_000 * 10**8, updated_at=99)
    decoded = decode_latest_round(raw)
    assert decoded.round_id == 3
    assert decoded.answer == 1_000 * 10**8
    assert decoded.updated_at == 99
    assert scale_answer(decoded.answer, 8) == 1000.0


def test_backed_verifier_high_ratio() -> None:
    session = _bnvda_rpc(reserves=1000, circulating=1000)
    v = BackedVerifier(session=session, cache_ttl=3600)
    result = v.verify_reserves(ticker="NVDA", issuer_name="Backed Finance")
    assert result.ok is True
    assert result.level == VerificationLevel.ON_CHAIN_POR
    assert result.source == "chainlink_por"
    assert result.score == 95.0
    assert "Chainlink PoR" in result.evidence
    assert "collateralization_ratio" in result.evidence
    assert result.meta["collateralization_ratio"] == pytest.approx(1.0)
    assert result.meta["symbol"] == "bNVDA"
    assert any("Chainlink" in n for n in result.notes)


def test_backed_verifier_fallback_on_rpc_error() -> None:
    session = FakeRpcSession(RuntimeError("rpc timeout"))
    v = BackedVerifier(session=session)
    result = v.verify_reserves(ticker="NVDA", issuer_name="Backed Finance")
    assert result.level == VerificationLevel.SELF_REPORTED
    assert result.source == "heuristic_fallback"
    assert any("heuristic fallback" in n for n in result.notes)
    assert result.error
    assert result.score == 90.0  # Backed Finance is on AUDITED list


def test_backed_verifier_fallback_when_no_feed() -> None:
    v = BackedVerifier(session=FakeRpcSession({}))
    result = v.verify_reserves(ticker="AAPL", issuer_name="xStocks")
    assert result.source == "heuristic_fallback"
    assert result.ok is True
    assert result.error is None
    assert "No published Chainlink PoR feed" in result.evidence
    assert any("heuristic fallback" in n for n in result.notes)


def test_backed_verifier_caches_for_one_hour() -> None:
    session = _bnvda_rpc()
    v = BackedVerifier(session=session, cache_ttl=3600)
    first = v.verify_reserves(ticker="NVDA", issuer_name="xStocks")
    second = v.verify_reserves(ticker="NVDA", issuer_name="xStocks")
    assert first.score == second.score == 95.0
    # latestRoundData + optional totalSupply, once (cached on the second call).
    assert len(session.calls) == 2
    assert all(LATEST_ROUND_DATA_SELECTOR in data or TOTAL_SUPPLY_SELECTOR in data for _, _, data in session.calls)


def test_backed_verifier_reserves_only_when_supply_missing() -> None:
    feed = resolve_por_feed("CSPX")
    assert feed is not None
    session = FakeRpcSession(
        {feed.proxy.lower(): encode_latest_round(answer=500 * 10**feed.decimals)}
    )
    v = BackedVerifier(session=session)
    result = v.verify_reserves(ticker="CSPX", issuer_name="Backed Finance")
    assert result.ok is True
    assert result.level == VerificationLevel.ON_CHAIN_POR
    assert result.score == 90.0
    assert result.meta["circulating_supply"] is None
    assert "Chainlink PoR" in result.evidence


def test_dinari_verifier_attestation_pending() -> None:
    html = """
    <html><body>
    <p>dShares are backed 1:1 by the underlying securities.</p>
    <p>Custody lives at Alpaca Securities LLC.</p>
    <p>Reserve audits are performed by an independent Big 4 accounting firm.</p>
    </body></html>
    """
    session = FakeSession({DINARI_DSHARES_URL: FakeResponse(200, text=html)})
    v = DinariVerifier(session=session)
    result = v.verify_reserves(ticker="AAPL", issuer_name="Dinari")
    assert result.ok is True
    assert result.level == VerificationLevel.ATTESTED
    assert result.score == 85.0
    assert any("attestation pending" in n for n in result.notes)
    assert "Alpaca" in result.evidence


def test_dinari_verifier_fallback_when_incomplete() -> None:
    html = "<html><body><p>Welcome to Dinari.</p></body></html>"
    session = FakeSession({DINARI_DSHARES_URL: FakeResponse(200, text=html)})
    v = DinariVerifier(session=session)
    result = v.verify_backing(ticker="AAPL", issuer_name="Dinari")
    assert result.source == "heuristic_fallback"
    assert any("heuristic fallback" in n for n in result.notes)
    assert result.error


def test_resolve_rejects_adversarial_backed_names() -> None:
    from rwa_score.verifiers import resolve_verifier_id

    assert resolve_verifier_id("Not Backed At All") is None
    assert resolve_verifier_id("UNBACKED Holdings") is None
    assert resolve_verifier_id("Anti-Backed Finance") is None
    assert resolve_verifier_id("feedbacked") is None
    assert resolve_verifier_id("Backed Finance") == "backed"
    assert resolve_verifier_id("xStocks") == "backed"
    assert resolve_verifier_id("Dinari") == "dinari"
    assert resolve_verifier_id("Robinhood") == "robinhood"
    assert resolve_verifier_id("Anti-Robinhood") is None
    assert resolve_verifier_id("robinhoodie") is None


def test_robinhood_verifier_self_reported_scores() -> None:
    v = RobinhoodVerifier()
    backing = v.verify_backing(ticker="AAPL", issuer_name="Robinhood")
    reserves = v.verify_reserves(ticker="AAPL", issuer_name="Robinhood")
    redemption = v.verify_redemption(ticker="AAPL", issuer_name="Robinhood")
    assert backing.score == 55.0
    assert reserves.score == 40.0
    assert redemption.score == 35.0
    assert backing.level == VerificationLevel.SELF_REPORTED
    assert reserves.level == VerificationLevel.SELF_REPORTED
    assert redemption.level == VerificationLevel.SELF_REPORTED
    assert backing.evidence == "Robinhood 1:1 claim, no public PoR"
    assert reserves.evidence == "no independent attestation"
    assert redemption.evidence == "debt wrapper, creditor claim only"
    assert backing.ok and reserves.ok and redemption.ok


def test_backed_feeds_are_public_polygon_proxies() -> None:
    assert {f.symbol for f in BACKED_POR_FEEDS} >= {"bNVDA", "bIB01", "bCSPX"}
    for feed in BACKED_POR_FEEDS:
        assert feed.chain == "polygon"
        assert feed.proxy.startswith("0x")
        assert len(feed.proxy) == 42
