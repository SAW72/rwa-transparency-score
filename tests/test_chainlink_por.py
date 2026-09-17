"""Unit tests for Chainlink PoR helpers (no live RPC)."""

from __future__ import annotations

import pytest

from rwa_score.chainlink_por import (
    HEALTH_POR_FEED,
    XSTOCKS_DATALINK_POR,
    XSTOCKS_POR_FEEDS,
    decode_int256,
    decode_latest_round,
    decode_uint256,
    eth_call_payload,
    parse_rpc_result,
    resolve_por_feed,
    rpc_urls_for_chain,
    scale_answer,
    xstocks_datalink_por,
)


def test_rpc_urls_prefer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYGON_RPC_URL", "https://polygon.example/rpc")
    urls = rpc_urls_for_chain("polygon")
    assert urls[0] == "https://polygon.example/rpc"
    assert "https://polygon-bor-rpc.publicnode.com" in urls


def test_rpc_urls_base_and_eth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASE_RPC_URL", "https://base.example/rpc")
    monkeypatch.setenv("ETH_RPC_URL", "https://eth.example/rpc")
    assert rpc_urls_for_chain("base")[0] == "https://base.example/rpc"
    assert rpc_urls_for_chain("ethereum")[0] == "https://eth.example/rpc"


def test_parse_rpc_error() -> None:
    with pytest.raises(RuntimeError, match="RPC error"):
        parse_rpc_result({"error": {"code": -32000, "message": "boom"}})
    with pytest.raises(RuntimeError, match="empty"):
        parse_rpc_result({"result": "0x"})


def test_decode_int256_negative() -> None:
    # ABI-encoded -1 as int256.
    assert decode_int256("0x" + "f" * 64) == -1
    assert decode_uint256("0x" + "0" * 63 + "2") == 2


def test_health_feed_is_backed_bib01() -> None:
    assert HEALTH_POR_FEED.symbol == "bIB01"
    assert HEALTH_POR_FEED.chain == "polygon"
    assert resolve_por_feed("IB01") == HEALTH_POR_FEED
    body = eth_call_payload(HEALTH_POR_FEED.proxy, "0xfeaf968c")
    assert body["params"][0]["to"] == HEALTH_POR_FEED.proxy


def test_xstocks_have_no_published_aggregator_proxy() -> None:
    """A2: DataLink stream names exist; AggregatorV3 proxies do not. Do not invent."""
    assert XSTOCKS_POR_FEEDS == ()
    for symbol in ("TSLA", "TSLAx", "AAPL", "AAPLx", "META", "METAx", "GOOGL", "COIN"):
        assert resolve_por_feed(symbol) is None
    # Catalog is names-only — empty proxy string, never a 0x address.
    assert XSTOCKS_DATALINK_POR
    for row in XSTOCKS_DATALINK_POR:
        assert row["proxy"] == ""
        assert "0x" not in row["proxy"]
        assert "Datalink" in row["stream"] or "Data" in row["channel"]
    tsla = xstocks_datalink_por("TSLAx")
    assert tsla is not None
    assert tsla["proxy"] == ""
    assert resolve_por_feed("TSLAx") is None


def test_nvda_alias_stays_on_published_btoken_not_datalink() -> None:
    """NVDAx still maps to the Backed bNVDA Polygon proxy — not an invented xStock feed."""
    feed = resolve_por_feed("NVDAx")
    assert feed is not None
    assert feed.symbol == "bNVDA"
    assert feed.proxy.startswith("0x")
    datalink = xstocks_datalink_por("NVDAx")
    assert datalink is not None
    assert datalink["proxy"] == ""
    assert datalink["proxy"] != feed.proxy


def test_scale_and_round_trip() -> None:
    raw = (
        "0x"
        + f"{9:064x}"
        + f"{250 * 10**8:064x}"
        + f"{1:064x}"
        + f"{1_700_000_001:064x}"
        + f"{9:064x}"
    )
    decoded = decode_latest_round(raw)
    assert decoded.round_id == 9
    assert scale_answer(decoded.answer, 8) == 250.0
    assert decoded.updated_at == 1_700_000_001
