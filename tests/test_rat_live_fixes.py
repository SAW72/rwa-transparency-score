"""F1 CMC 429 cache/dedup, F3 verified-vs-heuristic labels, F5 one-decimal scores.

Round 2 pins the plan-block memo, every-pillar LIVE-VERIFIED, and base's
last-issuer choice. Scoring math is not under test here.
"""

from __future__ import annotations

import threading
import time

from rwa_score.client import (
    CMCClient,
    CMCError,
    ENDPOINT_INFO,
    ENDPOINT_ISSUERS,
    ENDPOINT_ISSUERS_LIST,
    ENDPOINT_MAP,
    ENDPOINT_MARKET_PAIRS,
)
from rwa_score.scorer import TransparencyScorer, _issuer_name_rank
from rwa_score.verifiers import VerificationLevel, VerificationResult
from tests.conftest import RecordingClient
from tests.test_client import FakeResponse, FakeSession, _live, cmc_1006, cmc_429, cmc_ok


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
            cmc_ok({"name": "Extra Issuer", "tokens": []}),
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
    # Directory order is other, ondo, extra. The winner is the last issuer
    # that lists the rwa_id, so extra is fetched, then ondo, and other is not.
    assert paths.count(ENDPOINT_ISSUERS) == 2
    assert paths.count(ENDPOINT_INFO) == 2
    assert session.calls[3][1]["issuer_id"] == "extra"
    assert session.calls[4][1]["issuer_id"] == "ondo"


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


def test_failed_issuer_fetch_is_retried_and_not_cached() -> None:
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
    # Reverse directory order fetches extra, then the failing ondo call, then
    # the retry of ondo. other is never reached.
    assert client.calls["issuer"] == 3
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
                "level": "on-chain PoR",
                "source": "chainlink_por",
                "evidence": "published feed",
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
    assert "90.20" not in html
    assert "not available" in html
    assert demo_app.format_display_score(90.2) == "90.2"
    assert demo_app.format_display_score(74.9) == "74.9"
    assert demo_app.format_display_score(74.94) == "74.9"
    assert demo_app.format_display_score(74.9) != "75.0"
    ordered = frame.sort_values("price", ascending=True, na_position="last")
    assert list(ordered["ticker"]) == ["TSLA", "NVDA"]


def _pairs_ok(rwa_id: int) -> FakeResponse:
    return cmc_ok(
        {
            "rwa_id": rwa_id,
            "symbol": "B",
            "num_market_pairs": 1,
            "market_pairs": [{"market_id": rwa_id}],
        }
    )


def test_plan_block_memo_is_only_documented_1006_and_expires() -> None:
    """C03: a non-403 HTTP error, 1002, HTML 403, and a 1006 substring do not stick."""
    cases = [
        FakeResponse(403, {"status": {"error_code": 1002, "error_message": "API key missing"}}),
        FakeResponse(403, text="<html>Forbidden</html>"),
        FakeResponse(403, text="Forbidden"),
        FakeResponse(502, text="<html>ray 8c1006ab</html>"),
        FakeResponse(500, text="upstream http failed"),
        FakeResponse(
            400,
            {"status": {"error_code": 400, "error_message": "see code 1006 in this text"}},
        ),
    ]
    for bad in cases:
        session = FakeSession([bad, _pairs_ok(15)])
        client = _live(session)
        if bad.status_code != 403:
            try:
                client.market_pairs(rwa_id=2)
            except CMCError:
                pass
            else:
                raise AssertionError(
                    f"HTTP {bad.status_code} was treated as a plan block"
                )
        else:
            first = client.market_pairs(rwa_id=2)
            assert first["plan_blocked"] is True
        second = client.market_pairs(rwa_id=15)
        assert second.get("plan_blocked") is not True
        assert second["rwa_id"] == 15
        assert session.paths().count(ENDPOINT_MARKET_PAIRS) == 2

    session = FakeSession([cmc_1006(), _pairs_ok(15)])
    client = _live(session, pairs_ttl=0.05)
    assert client.market_pairs(rwa_id=2)["plan_blocked"] is True
    stuck = client.market_pairs(rwa_id=15)
    assert stuck["plan_blocked"] is True
    assert session.paths().count(ENDPOINT_MARKET_PAIRS) == 1
    time.sleep(0.08)
    fresh = client.market_pairs(rwa_id=15)
    assert fresh.get("plan_blocked") is not True
    assert session.paths().count(ENDPOINT_MARKET_PAIRS) == 2


def test_plan_block_memo_does_not_cover_other_endpoints() -> None:
    session = FakeSession(
        [
            cmc_1006(),
            cmc_ok({"rwa_assets": [{"rwa_id": 2, "symbol": "NVDA", "cik": "1"}]}),
        ]
    )
    client = _live(session)
    assert client.market_pairs(rwa_id=2)["plan_blocked"] is True
    info = client.rwa_info(2)
    assert info["symbol"] == "NVDA"
    assert session.paths().count(ENDPOINT_INFO) == 1


def _wide_b_responses() -> list[FakeResponse]:
    high = 100.0 * 1.06 / 0.94
    return [
        cmc_ok({"rwa_assets": [{"symbol": "B", "rwa_id": 15}]}),
        cmc_ok(
            {
                "rwa_assets": [
                    {"symbol": "B", "rwa_id": 15, "cik": "0001045810", "name": "B"}
                ]
            }
        ),
        cmc_ok({"issuers": [{"issuer_id": "ondo", "name": "Ondo Assets"}]}),
        cmc_ok(
            {
                "name": "Ondo Assets",
                "tokens": [{"rwa_id": 15, "crypto_id": 11}],
            }
        ),
        cmc_ok(
            {
                "rwa_assets": [
                    {
                        "rwa_id": 15,
                        "symbol": "B",
                        "average_tokenized_price": 100.0,
                        "tokenized_volume_24h": 10.0,
                        "tokens": [],
                    }
                ]
            }
        ),
        cmc_ok(
            {
                "rwa_id": 15,
                "symbol": "B",
                "num_market_pairs": 2,
                "market_pairs": [
                    {
                        "market_pair_base": {"crypto_id": 11, "symbol": "B1"},
                        "quote": {"USD": {"price": 100.0}},
                    },
                    {
                        "market_pair_base": {"crypto_id": 12, "symbol": "B2"},
                        "quote": {"USD": {"price": high}},
                    },
                ],
            }
        ),
    ]


def test_asset_a_403_does_not_flip_asset_b_to_green() -> None:
    """Verifier GREEN-flip: A's 403 must leave B at 70.5 YELLOW."""
    session = FakeSession(
        [FakeResponse(403, text="<html>Forbidden</html>"), *_wide_b_responses()]
    )
    client = _live(session)
    blocked = client.market_pairs(rwa_id=2)
    assert blocked["plan_blocked"] is True
    report = TransparencyScorer(client, use_live_verifiers=False).score("B")
    assert report["subscores"]["basis"] == 15.0
    assert report["score"] == 70.5
    assert report["band"] == "YELLOW"
    assert session.paths().count(ENDPOINT_MARKET_PAIRS) == 2


def test_retry_after_is_capped_at_max_wait() -> None:
    """C07. Does not change the flight lock."""
    slept: list[float] = []
    session = FakeSession(
        [
            cmc_429(retry_after=3600),
            cmc_ok({"rwa_assets": [{"rwa_id": 2, "symbol": "NVDA"}]}),
        ]
    )
    client = CMCClient(
        api_key="test-not-a-real-key",
        session=session,
        sleeper=lambda delay: slept.append(delay),
        max_wait_seconds=1.0,
        max_retries=1,
    )
    info = client.rwa_info(2)
    assert info["symbol"] == "NVDA"
    assert slept
    assert max(slept) <= 1.0
    assert max(slept) < 3600


def test_batch_map_cache_keeps_each_symbols_rows() -> None:
    """C11: a batch prime must not store every row under every symbol."""
    session = FakeSession(
        [
            cmc_ok(
                {
                    "rwa_assets": [
                        {"symbol": "NVDA", "rwa_id": 2},
                        {"symbol": "TSLA", "rwa_id": 3},
                    ]
                }
            )
        ]
    )
    client = _live(session)
    client.rwa_map("NVDA,TSLA")
    rows = client.rwa_map("TSLA")
    assert [row["symbol"] for row in rows] == ["TSLA"]
    assert session.paths().count(ENDPOINT_MAP) == 1


def test_score_slots_primes_the_symbol_map() -> None:
    """M01: the compare row has to call prime_symbol_map."""
    import app as demo_app

    primed: list[list[str]] = []

    class _Scorer:
        def prime_symbol_map(self, symbols: list[str]) -> None:
            primed.append(list(symbols))

        def score(self, symbol: str) -> dict:
            return {"ticker": symbol, "score": 1.0, "band": "RED"}

    demo_app._score_slots(_Scorer(), ["nvda", "", "tsla"])
    assert primed == [["NVDA", "TSLA"]]


def test_flight_lock_holds_the_second_info_call() -> None:
    """M06: overlapping info calls share one HTTP request."""
    started = threading.Event()
    release = threading.Event()

    class _Gate(FakeSession):
        def __init__(self, responses: list[FakeResponse]) -> None:
            super().__init__(responses)
            self.entered = 0

        def get(self, url, params=None, timeout=None):
            self.entered += 1
            started.set()
            assert release.wait(3)
            return super().get(url, params=params, timeout=timeout)

    session = _Gate(
        [cmc_ok({"rwa_assets": [{"rwa_id": 2, "symbol": "NVDA", "cik": "1"}]})]
    )
    client = _live(session)
    errors: list[BaseException] = []

    def _fetch() -> None:
        try:
            client.rwa_info(2)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    first = threading.Thread(target=_fetch)
    first.start()
    assert started.wait(2)
    second = threading.Thread(target=_fetch)
    second.start()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and session.entered < 2:
        time.sleep(0.01)
    assert session.entered == 1
    release.set()
    first.join(2)
    second.join(2)
    assert errors == []
    assert session.paths().count(ENDPOINT_INFO) == 1


def test_map_lookup_requests_one_symbol() -> None:
    """M09: a miss must not page the full RWA book."""
    client = RecordingClient(
        assets=[
            {"symbol": "NVDA", "rwa_id": 2},
            {"symbol": "TSLA", "rwa_id": 3},
        ]
    )
    seen: list[str | None] = []
    real = client.rwa_map

    def _map(symbol=None, **kwargs):
        seen.append(symbol)
        return real(symbol, **kwargs)

    client.rwa_map = _map  # type: ignore[method-assign]
    scorer = TransparencyScorer(client)
    assert scorer._map_lookup("NVDA") == 2
    assert seen == ["NVDA"]


def test_failed_issuers_list_is_not_cached_empty() -> None:
    """M12: a failed issuers/list is retried instead of sticking as []."""
    client = RecordingClient(
        issuers=[{"issuer_id": "ondo", "name": "Ondo"}],
        issuer_details={"ondo": {"name": "Ondo", "tokens": []}},
    )
    state = {"fail": True}
    real = client.issuers_list

    def _list():
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("HTTP 500")
        return real()

    client.issuers_list = _list  # type: ignore[method-assign]
    scorer = TransparencyScorer(client)
    try:
        scorer._issuer_entries()
    except Exception as exc:
        assert "500" in str(exc)
    else:
        raise AssertionError("first issuers/list should fail")
    entries = scorer._issuer_entries()
    assert entries[0]["issuer_id"] == "ondo"
    assert client.calls["issuers_list"] == 1


def test_short_issuer_name_fragments_do_not_rank() -> None:
    """S02: a fragment shorter than 4 characters is not a name match."""
    assert _issuer_name_rank("Ondo Assets", "Ond") == 1
    assert _issuer_name_rank("Ondo Assets", "ON") == 1
    assert _issuer_name_rank("Ondo Assets", "Ondo") == 0
    assert _issuer_name_rank("Backed Assets", "Ondo") == 1


def test_absorb_prefers_detail_name_over_list_name() -> None:
    """S05: issuer_name comes from the issuer detail, not the directory row."""
    scorer = TransparencyScorer(RecordingClient())
    scorer._absorb_issuer(
        "ondo",
        {"name": "Ondo Assets", "tokens": [{"rwa_id": 2, "crypto_id": 11}]},
        "Listed Ondo",
    )
    assert scorer._issuer_index[2]["issuer_name"] == "Ondo Assets"


def test_multi_issuer_nvda_matches_base_last_issuer() -> None:
    """Q4: CMC's NVDA example (Backed then Ondo) keeps base's Ondo score.

    The info issuer name is a hint only and must not choose the issuer.
    """
    client = RecordingClient(
        assets=[{"symbol": "NVDA", "rwa_id": 2}],
        info={
            2: {
                "symbol": "NVDA",
                "cik": "0001045810",
                "issuer": {"name": "Backed Assets"},
            }
        },
        issuers=[
            {"issuer_id": "backed", "name": "Backed Assets"},
            {"issuer_id": "ondo", "name": "Ondo Assets"},
        ],
        issuer_details={
            "backed": {
                "name": "Backed Assets",
                "tokens": [{"rwa_id": 2, "crypto_id": 1}],
            },
            "ondo": {
                "name": "Ondo Assets",
                "tokens": [{"rwa_id": 2, "crypto_id": 2}],
            },
        },
        rwa_quotes={
            2: {
                "symbol": "NVDA",
                "average_tokenized_price": 100.0,
                "tokens": [],
            }
        },
        market_pairs={
            2: {
                "rwa_id": 2,
                "symbol": "NVDA",
                "num_market_pairs": 0,
                "market_pairs": [],
            }
        },
    )
    report = TransparencyScorer(client, use_live_verifiers=False).score("NVDA")
    assert report["issuer"] == "Ondo Assets"
    assert report["score"] == 75.8
    assert report["band"] == "GREEN"
    assert client.calls["issuer"] == 1


def _pillars(**levels: dict) -> dict:
    return {
        "ticker": "X",
        "score": 70.0,
        "band": "YELLOW",
        "data_source": "live",
        "verification": levels,
    }


def test_live_verified_requires_every_pillar() -> None:
    """A02, M17, and the partial / failed / capped / Dinari rows."""
    import app as demo_app

    signed = {
        "level": "attested",
        "source": "issuer_attestation",
        "evidence": "signed report https://example.test/nvda.pdf",
    }
    pending = {
        "level": "attested",
        "source": "dinari_dshares",
        "evidence": "attestation pending — no signed report URL yet.",
        "notes": ["attestation pending"],
    }
    por = {
        "level": "on-chain PoR",
        "source": "chainlink_por",
        "evidence": "published feed",
    }
    self_reported = {
        "level": "self-reported",
        "source": "issuer",
        "evidence": "issuer page",
    }
    failed = {
        "level": "not available",
        "source": "not_available",
        "evidence": "not available: live redemption check failed",
        "error": "live check failed",
    }

    all_signed = _pillars(backing=signed, reserves=signed, redemption=signed)
    assert demo_app.card_evidence_label(all_signed) == "LIVE-VERIFIED"

    all_self = _pillars(
        backing=self_reported, reserves=self_reported, redemption=self_reported
    )
    assert demo_app.card_evidence_label(all_self) == "SELF-REPORTED"
    assert "LIVE-VERIFIED" not in demo_app.card_evidence_label(all_self)

    partial = _pillars(backing=por, reserves=por, redemption=self_reported)
    assert demo_app.card_evidence_label(partial) == "PARTIAL"
    assert demo_app.pillar_evidence_label(partial) == "PARTIAL"

    failed_card = _pillars(backing=por, reserves=por, redemption=failed)
    assert demo_app.card_evidence_label(failed_card) == "PARTIAL"
    assert "LIVE-VERIFIED" not in demo_app.card_evidence_label(failed_card)

    capped = _pillars(backing=por, reserves=por, redemption=por)
    capped["capped"] = True
    capped["score"] = 74.9
    capped["band_label"] = (
        "YELLOW — not available: a live check failed, so this score is not GREEN"
    )
    assert demo_app.card_evidence_label(capped) != "LIVE-VERIFIED"

    dinari = _pillars(backing=pending, reserves=pending, redemption=pending)
    label = demo_app.card_evidence_label(dinari)
    assert label == "ATTESTATION PENDING"
    assert "LIVE-VERIFIED" not in label


def test_http_503_with_error_1006_is_not_a_plan_block() -> None:
    """Q1-M04: a 503 whose JSON body carries error 1006 raises. It is not a plan block."""
    session = FakeSession([cmc_1006(http_status=503), _pairs_ok(15)])
    client = _live(session)
    try:
        client.market_pairs(rwa_id=2)
    except CMCError:
        pass
    else:
        raise AssertionError("503 with error 1006 was treated as a plan block")
    second = client.market_pairs(rwa_id=15)
    assert second.get("plan_blocked") is not True
    assert second["rwa_id"] == 15
    assert session.paths().count(ENDPOINT_MARKET_PAIRS) == 2
    assert client._market_pairs_plan_block is None


def test_unsigned_attestation_is_not_verified() -> None:
    """Q3-M05: PoR + PoR + unsigned non-Dinari ATTESTED is PARTIAL, not LIVE-VERIFIED."""
    import app as demo_app

    por = {
        "level": "on-chain PoR",
        "source": "chainlink_por",
        "evidence": "published feed",
    }
    unsigned = {
        "level": "attested",
        "source": "issuer_attestation",
        "evidence": "issuer page mentions an attestation",
    }
    card = _pillars(backing=por, reserves=por, redemption=unsigned)
    assert demo_app._pillar_live_verified(unsigned) is False
    assert demo_app.pillar_evidence_label(card) == "PARTIAL"
    assert demo_app.card_evidence_label(card) == "PARTIAL"
    assert "LIVE-VERIFIED" not in demo_app.card_evidence_label(card)
    lines = demo_app.selected_slot_verification_lines(card)
    assert lines[0] == "PARTIAL · strongest check: on-chain PoR"


def test_unsigned_attestation_slot_line_does_not_say_attested() -> None:
    """Three unsigned attestations stay heuristic and do not say 'strongest check: attested'."""
    import app as demo_app

    unsigned = {
        "level": "attested",
        "source": "issuer_attestation",
        "evidence": "issuer page mentions an attestation",
    }
    card = _pillars(backing=unsigned, reserves=unsigned, redemption=unsigned)
    assert demo_app.card_evidence_label(card) == "HEURISTIC"
    lines = demo_app.selected_slot_verification_lines(card)
    assert lines[0] == "HEURISTIC · strongest check: unsigned attestation (heuristic)"
    assert "strongest check: attested" not in lines[0]

    signed = {
        "level": "attested",
        "source": "issuer_attestation",
        "evidence": "signed report https://example.test/nvda.pdf",
    }
    verified = _pillars(backing=signed, reserves=signed, redemption=signed)
    verified_lines = demo_app.selected_slot_verification_lines(verified)
    assert verified_lines[0] == "LIVE-VERIFIED · strongest check: attested"


def _order_directory_client() -> RecordingClient:
    """Alpha lists TSLA, then Ondo lists NVDA, then Backed lists NVDA."""
    quote = {
        "symbol": "NVDA",
        "average_tokenized_price": 100.0,
        "tokenized_volume_24h": 10.0,
        "tokens": [
            {
                "symbol": "NVDAX",
                "price": 100.0,
                "crypto_id": 11,
                "volume_24h": 10.0,
            }
        ],
    }
    return RecordingClient(
        assets=[
            {"symbol": "TSLA", "rwa_id": 15},
            {"symbol": "NVDA", "rwa_id": 2},
        ],
        info={
            15: {"symbol": "TSLA", "cik": "0001318605"},
            2: {"symbol": "NVDA", "cik": "0001045810"},
        },
        issuers=[
            {"issuer_id": "alpha", "name": "Alpha"},
            {"issuer_id": "ondo", "name": "Ondo"},
            {"issuer_id": "backed", "name": "Backed Finance"},
        ],
        issuer_details={
            "alpha": {
                "name": "Alpha",
                "tokens": [{"rwa_id": 15, "crypto_id": 30}],
            },
            "ondo": {
                "name": "Ondo",
                "tokens": [{"rwa_id": 2, "crypto_id": 21}],
            },
            "backed": {
                "name": "Backed Finance",
                "tokens": [{"rwa_id": 2, "crypto_id": 11}],
            },
        },
        quotes={
            30: {"quote": {"USD": {"percent_change_24h": 0.0, "price": 100.0}}},
        },
        rwa_quotes={2: quote, 15: dict(quote, symbol="TSLA")},
        market_pairs={
            2: {"rwa_id": 2, "symbol": "NVDA", "num_market_pairs": 0, "market_pairs": []},
            15: {"rwa_id": 15, "symbol": "TSLA", "num_market_pairs": 0, "market_pairs": []},
        },
    )


class _PorBackedVerifier:
    """Backed pillars at 95. With CIK 80, price 100, and one wrapper, NVDA is 87.5."""

    name = "backed"

    def _por(self) -> VerificationResult:
        return VerificationResult(
            score=95.0,
            level=VerificationLevel.ON_CHAIN_POR,
            evidence="Chainlink PoR ok",
            source="chainlink_por",
            ok=True,
        )

    def verify_backing(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        return self._por()

    def verify_reserves(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        return self._por()

    def verify_redemption(self, *, ticker: str, issuer_name: str) -> VerificationResult:
        return self._por()


def _score_order(order: list[str]) -> dict:
    scorer = TransparencyScorer(
        _order_directory_client(),
        verifiers={"backed": _PorBackedVerifier()},
        use_live_verifiers=True,
    )
    reports = {ticker: scorer.score(ticker) for ticker in order}
    return reports["NVDA"]


def test_shared_nvda_issuer_is_independent_of_card_order() -> None:
    """Q4: Alpha [TSLA], Ondo [NVDA], Backed [NVDA]. Both orders pick Backed 87.5 GREEN.

    Base ``4c23bfd`` keeps the last directory issuer. Revert the
    ``_absorb_issuer`` skip and TSLA-then-NVDA returns Ondo instead.
    """
    for order in (["TSLA", "NVDA"], ["NVDA", "TSLA"]):
        report = _score_order(order)
        assert report["issuer"] == "Backed Finance", order
        assert report["score"] == 87.5, order
        assert report["band"] == "GREEN", order
