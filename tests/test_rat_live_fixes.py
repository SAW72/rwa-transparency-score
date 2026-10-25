"""F1 CMC 429 cache/dedup, F3 verified-vs-heuristic labels, F5 one-decimal scores."""

from __future__ import annotations

import threading

from rwa_score.client import ENDPOINT_INFO, ENDPOINT_ISSUERS, ENDPOINT_ISSUERS_LIST, ENDPOINT_MAP, ENDPOINT_MARKET_PAIRS
from rwa_score.scorer import TransparencyScorer
from rwa_score.verifiers import VerificationLevel
from tests.conftest import RecordingClient
from tests.test_client import FakeSession, _live, cmc_1006, cmc_429, cmc_ok


def _ondo_session() -> FakeSession:
    """Map has_more would page a full book. Symbol lookup must stay one call."""
    return FakeSession(
        [
            cmc_ok(
                {
                    "total_size": 9000,
                    "has_more": True,
                    "rwa_assets": [
                        {"symbol": "NVDA", "rwa_id": 2},
                        {"symbol": "AAPL", "rwa_id": 3},
                    ],
                }
            ),
            cmc_ok(
                {
                    "rwa_assets": [
                        {
                            "symbol": "NVDA",
                            "rwa_id": 2,
                            "cik": "0001045810",
                            "issuer": {"name": "Ondo Assets"},
                        }
                    ]
                }
            ),
            cmc_ok(
                {
                    "issuers": [
                        {"issuer_id": "other", "name": "Other Issuer"},
                        {"issuer_id": "ondo", "name": "Ondo"},
                        {"issuer_id": "extra", "name": "Extra Issuer"},
                    ]
                }
            ),
            cmc_ok(
                {
                    "name": "Ondo",
                    "tokens": [
                        {"rwa_id": 2, "crypto_id": 11},
                        {"rwa_id": 3, "crypto_id": 12},
                    ],
                }
            ),
            cmc_ok(
                {
                    "rwa_assets": [
                        {
                            "rwa_id": 2,
                            "symbol": "NVDA",
                            "average_tokenized_price": 10.0,
                            "tokenized_market_cap": 1000.0,
                            "tokenized_volume_24h": 100.0,
                            "tokens": [
                                {
                                    "symbol": "NVDAon",
                                    "price": 10.0,
                                    "crypto_id": 11,
                                    "issuer_name": "Ondo",
                                }
                            ],
                            "tradfi_markets": [],
                        }
                    ]
                }
            ),
            cmc_ok({"rwa_id": 2, "symbol": "NVDA", "num_market_pairs": 0, "market_pairs": []}),
            cmc_ok(
                {
                    "rwa_assets": [
                        {
                            "symbol": "AAPL",
                            "rwa_id": 3,
                            "cik": "0000320193",
                            "issuer": {"name": "Ondo"},
                        }
                    ]
                }
            ),
            cmc_ok(
                {
                    "rwa_assets": [
                        {
                            "rwa_id": 3,
                            "symbol": "AAPL",
                            "average_tokenized_price": 20.0,
                            "tokenized_market_cap": 2000.0,
                            "tokenized_volume_24h": 50.0,
                            "tokens": [
                                {
                                    "symbol": "AAPLon",
                                    "price": 20.0,
                                    "crypto_id": 12,
                                    "issuer_name": "Ondo",
                                }
                            ],
                            "tradfi_markets": [],
                        }
                    ]
                }
            ),
            cmc_ok({"rwa_id": 3, "symbol": "AAPL", "num_market_pairs": 0, "market_pairs": []}),
        ]
    )


def test_cold_cards_share_one_issuer_and_do_not_page_the_map() -> None:
    session = _ondo_session()
    client = _live(session)
    scorer = TransparencyScorer(client)
    scorer.prime_symbol_map(["NVDA", "AAPL"])
    nvda = scorer.score("NVDA")
    aapl = scorer.score("AAPL")
    assert nvda["issuer"] == "Ondo"
    assert aapl["issuer"] == "Ondo"
    paths = session.paths()
    assert paths.count(ENDPOINT_MAP) == 1
    assert session.calls[0][1]["symbol"] == "NVDA,AAPL"
    assert "start" not in (session.calls[0][1] or {})
    assert paths.count(ENDPOINT_ISSUERS_LIST) == 1
    assert paths.count(ENDPOINT_ISSUERS) == 1
    assert paths.count(ENDPOINT_INFO) == 2
    assert session.calls[3][1]["issuer_id"] == "ondo"


def test_info_429_is_retried_then_reused_from_cache() -> None:
    session = FakeSession(
        [
            cmc_429(retry_after=0),
            cmc_ok({"rwa_assets": [{"rwa_id": 2, "symbol": "NVDA", "cik": "0001045810"}]}),
        ]
    )
    client = _live(session)
    first = client.rwa_info(2)
    second = client.rwa_info(2)
    assert first["symbol"] == "NVDA"
    assert second["cik"] == "0001045810"
    assert session.paths().count(ENDPOINT_INFO) == 2


def test_parallel_info_callers_share_one_http_call() -> None:
    started = threading.Event()

    class _Slow(FakeSession):
        def get(self, url, params=None, timeout=None):
            started.set()
            return super().get(url, params=params, timeout=timeout)

    session = _Slow(
        [cmc_ok({"rwa_assets": [{"rwa_id": 2, "symbol": "NVDA", "cik": "1"}]})]
    )
    client = _live(session)
    results: list[dict] = []
    errors: list[BaseException] = []

    def _fetch() -> None:
        try:
            results.append(client.rwa_info(2))
        except BaseException as exc:  # noqa: BLE001 — surface the thread error
            errors.append(exc)

    threads = [threading.Thread(target=_fetch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert errors == []
    assert len(results) == 2
    assert results[0]["symbol"] == results[1]["symbol"] == "NVDA"
    assert session.paths().count(ENDPOINT_INFO) == 1


def test_failed_issuer_fetch_is_retried_for_the_hinted_issuer_only() -> None:
    client = RecordingClient(
        assets=[{"symbol": "NVDA", "rwa_id": 2}],
        info={2: {"symbol": "NVDA", "cik": "0001045810", "issuer": {"name": "Ondo Assets"}}},
        issuers=[
            {"issuer_id": "other", "name": "Other Issuer"},
            {"issuer_id": "ondo", "name": "Ondo"},
            {"issuer_id": "extra", "name": "Extra Issuer"},
        ],
        issuer_details={
            "ondo": {"name": "Ondo", "tokens": [{"rwa_id": 2, "crypto_id": 11}]},
            "other": {"name": "Other Issuer", "tokens": [{"rwa_id": 99, "crypto_id": 1}]},
            "extra": {"name": "Extra Issuer", "tokens": []},
        },
    )
    client._fails = 1
    real = client.issuer

    def _issuer(issuer_id: str) -> dict:
        if client._fails and issuer_id == "ondo":
            client._fails -= 1
            client.calls["issuer"] += 1
            raise RuntimeError("HTTP 429")
        return real(issuer_id)

    client.issuer = _issuer  # type: ignore[method-assign]
    scorer = TransparencyScorer(client)
    try:
        scorer.score("NVDA")
    except Exception as exc:
        assert "429" in str(exc)
    else:
        raise AssertionError("first score should fail the issuer fetch")
    report = scorer.score("NVDA")
    assert report["issuer"] == "Ondo"
    assert client.calls["issuer"] == 2
    assert client.calls["issuers_list"] == 1


def test_plan_blocked_market_pairs_are_not_refetched_per_card() -> None:
    session = FakeSession([cmc_1006()])
    client = _live(session)
    first = client.market_pairs(rwa_id=2)
    second = client.market_pairs(rwa_id=3)
    assert first["plan_blocked"] is True
    assert second["plan_blocked"] is True
    assert second["rwa_id"] == 3
    assert session.paths().count(ENDPOINT_MARKET_PAIRS) == 1


def test_card_label_says_heuristic_or_live_verified() -> None:
    import app as demo_app

    heuristic = {
        "ticker": "NVDA",
        "score": 90.2,
        "band": "GREEN",
        "issuer": "Ondo",
        "data_source": "live",
        "subscores": {"backing": 90, "reserves": 90, "redemption": 85},
        "verification": {
            key: {
                "level": VerificationLevel.SELF_REPORTED.value,
                "source": "heuristic_fallback",
                "evidence": "heuristic fallback",
            }
            for key in ("backing", "reserves", "redemption")
        },
    }
    verified = {
        "ticker": "bNVDA",
        "score": 88.0,
        "band": "GREEN",
        "issuer": "Backed",
        "data_source": "live",
        "verification": {
            "backing": {
                "level": "on-chain PoR",
                "source": "chainlink_por",
                "evidence": "published feed",
            },
            "reserves": {
                "level": "on-chain PoR",
                "source": "chainlink_por",
                "evidence": "published feed",
            },
            "redemption": {
                "level": "attested",
                "source": "issuer_docs",
                "evidence": "redemption docs",
            },
        },
    }
    assert demo_app.card_evidence_label(heuristic) == "HEURISTIC"
    assert demo_app.pillar_evidence_label(heuristic) == "HEURISTIC"
    assert "LIVE" not in demo_app.card_evidence_label(heuristic)
    assert demo_app.card_evidence_label(verified) == "LIVE-VERIFIED"
    lines = demo_app.selected_slot_verification_lines(heuristic)
    assert lines[0] == "HEURISTIC · strongest check: heuristic fallback"
    verified_lines = demo_app.selected_slot_verification_lines(verified)
    assert verified_lines[0] == "LIVE-VERIFIED · strongest check: on-chain PoR"
    diff = demo_app.compare_differentiation([heuristic, verified])
    high = demo_app.selected_slot_contrast_line("NVDA", diff)
    assert high.startswith("Highest in this row (90.2 GREEN, HEURISTIC)")
    low = demo_app.selected_slot_contrast_line("bNVDA", diff)
    assert "LIVE-VERIFIED" in low
    assert "GREEN" in low


def test_comparison_table_displays_one_decimal_and_sorts_on_numbers() -> None:
    import app as demo_app

    reports = [
        {
            "ticker": "NVDA",
            "issuer": "Ondo",
            "score": 90.2,
            "band": "GREEN",
            "subscores": {"backing": 90.2, "price": 96.8, "basis": None},
        },
        {
            "ticker": "TSLA",
            "issuer": "Ondo",
            "score": 72.3,
            "band": "YELLOW",
            "subscores": {"backing": 90.0, "price": 62.0, "basis": 15.0},
        },
    ]
    frame = demo_app.comparison_table_frame(reports)
    assert str(frame["score"].dtype).startswith("float")
    assert frame.loc[frame["ticker"] == "NVDA", "score"].iloc[0] == 90.2
    styled = demo_app.comparison_table(reports)
    html = styled.to_html()
    assert "90.2" in html
    assert "96.8" in html
    assert "72.3" in html
    assert "90.200000" not in html
    assert "96.800000" not in html
    assert "not available" in html
    ordered = frame.sort_values("price", ascending=True, na_position="last")
    assert list(ordered["ticker"]) == ["TSLA", "NVDA"]
