"""Paid API: auth, quotas, parity with TransparencyScorer, webhooks."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rwa_score.api.app import BREAKDOWN_KEYS, create_app
from rwa_score.api.attest import attestation_payload, canonical_bytes, hash_canonical, inputs_bytes
from rwa_score.api.settings import ApiSettings
from rwa_score.api.store import Store
from rwa_score.scorer import ScoreError, TransparencyScorer


def _settings(tmp_path: Path, **overrides: Any) -> ApiSettings:
    base = ApiSettings(free_daily_limit=50)
    if not overrides:
        return base
    data = base.__dict__.copy()
    data.update(overrides)
    return ApiSettings(**data)


def _client(
    tmp_path: Path,
    scorer: TransparencyScorer,
    *,
    poster=None,
    **setting_overrides: Any,
) -> tuple[TestClient, Store]:
    settings = _settings(tmp_path, **setting_overrides)
    store = Store()
    app = create_app(settings=settings, store=store, scorer=scorer, poster=poster)
    return TestClient(app), store


def _headers(raw: str) -> dict[str, str]:
    return {"X-API-Key": raw}


def _minimal_report(ticker: str, score: float, band: str) -> dict[str, Any]:
    return {
        "ticker": ticker.upper(),
        "rwa_id": 1,
        "issuer": "Test Issuer",
        "score": score,
        "band": band,
        "band_label": band,
        "subscores": {
            "backing": 50.0,
            "reserves": 50.0,
            "redemption": 50.0,
            "price": 50.0,
            "disclosure": 50.0,
            "basis": 50.0,
        },
        "weights": {
            "backing": 0.20,
            "reserves": 0.20,
            "redemption": 0.15,
            "price": 0.15,
            "disclosure": 0.15,
            "basis": 0.15,
        },
        "pillars": {},
        "explanations": {"backing": "x"},
        "verification": {
            "backing": {"score": 50.0, "level": "self-reported", "source": "test"},
            "reserves": {"score": 50.0, "level": "self-reported", "source": "test"},
            "redemption": {"score": 50.0, "level": "self-reported", "source": "test"},
            "price": {"score": 50.0, "level": "self-reported", "source": "test"},
            "disclosure": {"score": 50.0, "level": "self-reported", "source": "test"},
            "basis": {"score": 50.0, "level": "self-reported", "source": "test"},
        },
        "flags": [],
        "notes": [],
        "heuristics": {},
        "data_source": "fixture",
        "price": {},
        "cik": None,
        "issuer_note": "",
        "summary": f"{ticker} test",
    }


class SequenceScorer:
    def __init__(self, reports: list[dict[str, Any]]) -> None:
        self.reports = list(reports)

    def score(self, ticker: str) -> dict[str, Any]:
        if not self.reports:
            raise ScoreError(f"{ticker} not found in RWA map")
        return dict(self.reports.pop(0))


def test_health_does_not_need_a_key(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    client, _ = _client(tmp_path, fixture_scorer)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["api"] is True
    assert "fixtures" in body
    assert body["process"] == "ok"
    assert body["chain_id"] == 84532
    assert body["rpc"] in {"ok", "down", "unset", "unknown"}
    assert isinstance(body["queue_depth"], int)
    assert "database" not in body
    assert body["attester"] in {"enabled", "disabled"}


def test_control_character_ticker_is_400(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    client, store = _client(tmp_path, fixture_scorer)
    raw = store.create_key(name="paid", tier="paid")
    headers = _headers(raw)
    score = client.get("/v1/score/NVDA%00", headers=headers)
    attest = client.post("/v1/attest/NV%0ADA", headers=headers)
    status = client.get("/v1/attest/NV%00DA/status", headers=headers)
    for resp in (score, attest, status):
        assert resp.status_code == 400
        assert resp.json()["error"] == "bad_ticker"
        assert resp.status_code < 500


def test_watchlist_history_and_webhooks_are_not_stored(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    client, store = _client(tmp_path, fixture_scorer)
    raw = store.create_key(name="paid", tier="paid")
    headers = _headers(raw)
    for method, path in (
        ("get", "/v1/watchlist"),
        ("put", "/v1/watchlist"),
        ("get", "/v1/history/NVDA"),
        ("post", "/v1/webhooks"),
        ("get", "/v1/webhooks"),
    ):
        resp = getattr(client, method)(path, headers=headers)
        assert resp.status_code == 404


def test_webhook_url_allowlist_still_rejects_ssrf() -> None:
    from rwa_score.api.webhooks import assert_public_https_url

    for url in (
        "http://hooks.example.com/hook",
        "https://localhost/hook",
        "https://127.0.0.1/hook",
        "https://192.168.0.10/hook",
        "https://10.1.2.3/hook",
        "https://169.254.169.254/latest/meta-data",
    ):
        with pytest.raises(ValueError):
            assert_public_https_url(url)
    assert assert_public_https_url("https://example.com/hook", resolve=False) == "https://example.com/hook"


def test_attest_endpoint_returns_hash_not_for_chain_storage_of_score(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    client, store = _client(tmp_path, fixture_scorer)
    raw = store.create_key(name="paid", tier="paid")
    resp = client.post("/v1/attest/NVDA", headers=_headers(raw))
    assert resp.status_code == 200
    body = resp.json()
    assert body["algo"] == "sha256"
    assert body["payload"]["score"] == fixture_scorer.score("NVDA")["score"]
    assert body["score"] == body["payload"]["score"]
    assert body["as_of"] == body["payload"]["as_of"]
    assert body["chain"] == "base-sepolia"
    assert body["chain_id"] == 84532
    assert "stores only the hash" in body["note"].lower()
    assert body["contract"] is None
    assert body["status"] == "disabled"
    assert body["tx_hash"] is None
    import base64

    raw_bytes = base64.b64decode(body["canonical_b64"])
    assert raw_bytes == canonical_bytes(body["payload"])
    assert hash_canonical(raw_bytes) == body["score_hash"]
    assert store.queue_depth() == 0
    saved_file = tmp_path / "nvda.payload.json"
    saved_file.write_text(json.dumps(body), encoding="utf-8")
    from rwa_score.api.verify import main as verify_main

    assert verify_main(["NVDA", "--offline", "--json", "--payload-file", str(saved_file)]) == 0
    fresh = Store()
    assert fresh.queue_depth() == 0
    assert verify_main(["NVDA", "--offline", "--json", "--payload-file", str(saved_file)]) == 0


def test_nan_in_live_report_score_200_attest_422(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    """Score sanitizes non-finite numbers. Attest refuses them before any write."""
    from rwa_score.api.attest import canonical_bytes
    from rwa_score.api.auto_attest import AttesterSettings

    report = fixture_scorer.score("NVDA")
    report = dict(report)
    report["data_source"] = "live"
    report["subscores"] = dict(report["subscores"])
    report["score"] = float("nan")
    report["subscores"]["price"] = float("inf")

    with pytest.raises(ValueError):
        canonical_bytes({"score": float("nan")})

    class _NanScorer:
        def score(self, ticker: str) -> dict[str, Any]:
            return report

    settings = _settings(tmp_path)
    store = Store()
    attester = AttesterSettings(
        private_key="0x" + "11" * 32,
        contract="0x" + "ab" * 20,
        rpc_url="http://127.0.0.1:8545",
        attest_enabled=True,
    )
    app = create_app(
        settings=settings,
        store=store,
        scorer=_NanScorer(),  # type: ignore[arg-type]
        attester=attester,
        start_worker=False,
    )
    client = TestClient(app)
    raw = store.create_key(name="paid", tier="paid")
    attested = client.post("/v1/attest/NVDA", headers=_headers(raw))
    assert attested.status_code == 422
    body = attested.json()
    assert body["error"] == "data_unavailable"
    assert body["data_unavailable"] == ["score", "price"]
    assert body["note"] == "information not available for: score, price"
    assert store.queue_depth() == 0

    scored = client.get("/v1/score/NVDA", headers=_headers(raw))
    assert scored.status_code == 200
    scored_body = scored.json()
    assert scored_body["score"] is None
    assert scored_body["subscores"]["price"] is None
    assert scored_body["data_unavailable"] == ["score", "price"]
    assert scored_body["note"] == "information not available for: score, price"
    assert "NaN" not in scored.text
    assert "Infinity" not in scored.text


@pytest.mark.parametrize("bad", [float("nan"), None, "", float("inf")])
def test_blank_price_and_volume_score_200_attest_422(
    tmp_path: Path, fixture_scorer: TransparencyScorer, bad: object
) -> None:
    """Missing, blank, NaN, and Inf price or volume stay null and are not posted."""
    import copy
    from unittest.mock import Mock

    from rwa_score.api.auto_attest import AttesterSettings

    report = copy.deepcopy(fixture_scorer.score("NVDA"))
    report["price"]["price"] = bad
    report["price"]["volume_24h"] = bad

    class _BadScorer:
        def score(self, ticker: str) -> dict[str, Any]:
            return report

    chain = Mock()
    chain.attest.side_effect = AssertionError("no tx")
    settings = _settings(tmp_path)
    store = Store()
    attester = AttesterSettings(
        private_key="0x" + "22" * 32,
        contract="0x" + "ab" * 20,
        rpc_url="http://127.0.0.1:8545",
        attest_enabled=True,
    )
    app = create_app(
        settings=settings,
        store=store,
        scorer=_BadScorer(),  # type: ignore[arg-type]
        attester=attester,
        chain=chain,
        start_worker=False,
    )
    client = TestClient(app)
    raw = store.create_key(name="paid", tier="paid")
    attested = client.post("/v1/attest/NVDA", headers=_headers(raw))
    assert attested.status_code == 422
    body = attested.json()
    assert body["data_unavailable"] == ["price", "volume"]
    assert body["note"] == "information not available for: price, volume"
    assert store.queue_depth() == 0
    chain.attest.assert_not_called()

    scored = client.get("/v1/score/NVDA", headers=_headers(raw))
    assert scored.status_code == 200
    scored_body = scored.json()
    assert scored_body["price"]["price"] is None
    assert scored_body["price"]["volume_24h"] is None
    assert scored_body["data_unavailable"] == ["price", "volume"]
    assert scored_body["note"] == "information not available for: price, volume"
    assert scored_body["ticker"] == "NVDA"
    assert scored_body["subscores"]["backing"] == report["subscores"]["backing"]
    assert "NaN" not in scored.text
    assert "Infinity" not in scored.text


def test_attest_status_does_not_score(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    from unittest.mock import Mock

    from rwa_score.api.auto_attest import AttesterSettings

    settings = _settings(tmp_path)
    store = Store()
    scorer = Mock()
    scorer.score.side_effect = AssertionError("status must not score")
    attester = AttesterSettings(
        private_key="0x" + "11" * 32,
        contract="0x" + "ab" * 20,
        rpc_url="http://127.0.0.1:8545",
        attest_enabled=True,
    )
    digest = "0x" + "cd" * 32
    chain = Mock()
    chain.hashes_for_ticker.return_value = []
    chain.attested.return_value = True
    chain.get_attestation.return_value = {
        "ticker": "NVDA",
        "attested_at": 1_700_000_000,
        "claimed_at": 0,
        "attester": "0x" + "11" * 20,
        "score_hash": digest,
    }
    chain.verify.return_value = (True, 1_700_000_000, "0x" + "11" * 20)
    app = create_app(
        settings=settings,
        store=store,
        scorer=scorer,
        attester=attester,
        chain=chain,
        start_worker=False,
    )
    client = TestClient(app)
    raw = store.create_key(name="paid", tier="paid")
    empty = client.get("/v1/attest/NVDA/status", headers=_headers(raw))
    assert empty.status_code == 200
    assert empty.json()["on_chain"]["source"] == "chain"
    assert empty.json()["on_chain"]["attested"] is False
    assert "stored" not in empty.json()
    body = client.get(
        "/v1/attest/NVDA/status",
        headers=_headers(raw),
        params={"score_hash": digest},
    ).json()
    assert body["on_chain"]["attested"] is True
    assert body["on_chain"]["attestedAt"] == 1_700_000_000
    assert body["on_chain"]["status"] == "confirmed"
    assert body["score_hash"] == digest
    scorer.score.assert_not_called()
    store.close()


def _case_sensitive_chain(stored: str, digest: str):
    """Chain reads that match ScoreAttestation: exact ticker bytes, not casefold."""
    from unittest.mock import Mock

    attester = "0x" + "11" * 20
    tx = "0x" + "ab" * 32

    def hashes_for_ticker(ticker: str) -> list[str]:
        return [digest] if ticker == stored else []

    def verify(score_hash: str, ticker: str):
        if score_hash == digest and ticker == stored:
            return (True, 1_700_000_000, attester)
        return (False, 0, "0x" + "00" * 20)

    def get_attestation(score_hash: str) -> dict[str, Any]:
        ticker = stored if score_hash == digest else ""
        return {
            "ticker": ticker,
            "attested_at": 1_700_000_000,
            "claimed_at": 1_699_000_000,
            "attester": attester,
            "score_hash": score_hash,
        }

    chain = Mock()
    chain.hashes_for_ticker.side_effect = hashes_for_ticker
    chain.verify.side_effect = verify
    chain.get_attestation.side_effect = get_attestation
    chain.attested.side_effect = lambda score_hash: score_hash == digest
    chain.receipt_event.side_effect = lambda tx_hash: (
        {
            "ticker": stored,
            "score_hash": digest,
            "attested_at": 1_700_000_000,
            "claimed_at": 1_699_000_000,
            "attester": attester,
        }
        if tx_hash == tx
        else None
    )
    return chain, tx


@pytest.mark.parametrize(
    ("query", "stored", "canonical"),
    [
        ("bNVDA", "bNVDA", "bNVDA"),
        ("BNVDA", "bNVDA", "bNVDA"),
        ("AAPL", "AAPL", "AAPL"),
        ("aapl", "AAPL", "AAPL"),
    ],
)
def test_attest_status_matches_stored_ticker_case(
    tmp_path: Path, query: str, stored: str, canonical: str
) -> None:
    """bNVDA stays bNVDA. An all-caps ticker still matches its uppercased record."""
    from unittest.mock import Mock

    from rwa_score.api.auto_attest import AttesterSettings

    digest = "0x" + "cd" * 32
    chain, tx = _case_sensitive_chain(stored, digest)
    settings = _settings(tmp_path)
    store = Store()
    scorer = Mock()
    scorer.score.side_effect = AssertionError("status must not score")
    attester = AttesterSettings(
        private_key="0x" + "11" * 32,
        contract="0x" + "ab" * 20,
        rpc_url="http://127.0.0.1:8545",
        attest_enabled=True,
    )
    app = create_app(
        settings=settings,
        store=store,
        scorer=scorer,
        attester=attester,
        chain=chain,
        start_worker=False,
    )
    client = TestClient(app)
    raw = store.create_key(name="paid", tier="paid")
    headers = _headers(raw)

    by_ticker = client.get(f"/v1/attest/{query}/status", headers=headers)
    assert by_ticker.status_code == 200
    body = by_ticker.json()
    assert body["ticker"] == canonical
    assert body["status"] == "confirmed"
    assert body["on_chain"]["ticker"] == canonical
    assert body["on_chain"]["attested"] is True
    assert body["on_chain"]["status"] == "confirmed"
    assert body["score_hash"] == digest

    by_hash = client.get(
        f"/v1/attest/{query}/status",
        headers=headers,
        params={"score_hash": digest},
    )
    assert by_hash.status_code == 200
    hashed = by_hash.json()
    assert hashed["ticker"] == canonical
    assert hashed["on_chain"]["attested"] is True
    assert hashed["status"] == "confirmed"

    by_tx = client.get(
        f"/v1/attest/{query}/status",
        headers=headers,
        params={"tx_hash": tx},
    )
    assert by_tx.status_code == 200
    receipt = by_tx.json()
    assert receipt["ticker"] == canonical
    assert receipt["on_chain"]["attested"] is True
    assert receipt["on_chain"]["event"]["ticker"] == stored
    assert receipt["tx_hash"] == tx

    assert [call.args for call in chain.hashes_for_ticker.call_args_list] == [(canonical,)]
    assert all(call.args == (digest, canonical) for call in chain.verify.call_args_list)
    assert chain.verify.call_args_list
    scorer.score.assert_not_called()
    store.close()


def test_attest_status_does_not_treat_nvda_as_bnvda(tmp_path: Path) -> None:
    """NVDA and bNVDA are different chain keys. Uppercasing must not collapse them."""
    from unittest.mock import Mock

    from rwa_score.api.auto_attest import AttesterSettings

    digest = "0x" + "ef" * 32
    chain, tx = _case_sensitive_chain("bNVDA", digest)
    settings = _settings(tmp_path)
    store = Store()
    scorer = Mock()
    scorer.score.side_effect = AssertionError("status must not score")
    attester = AttesterSettings(
        private_key="0x" + "11" * 32,
        contract="0x" + "ab" * 20,
        rpc_url="http://127.0.0.1:8545",
        attest_enabled=True,
    )
    app = create_app(
        settings=settings,
        store=store,
        scorer=scorer,
        attester=attester,
        chain=chain,
        start_worker=False,
    )
    client = TestClient(app)
    raw = store.create_key(name="paid", tier="paid")
    headers = _headers(raw)
    missed = client.get("/v1/attest/NVDA/status", headers=headers)
    assert missed.status_code == 200
    assert missed.json()["ticker"] == "NVDA"
    assert missed.json()["on_chain"]["attested"] is False
    assert missed.json()["on_chain"]["status"] == "absent"
    assert [call.args for call in chain.hashes_for_ticker.call_args_list] == [("NVDA",)]

    crossed = client.get(
        "/v1/attest/NVDA/status",
        headers=headers,
        params={"tx_hash": tx},
    )
    assert crossed.status_code == 200
    assert crossed.json()["on_chain"]["attested"] is False
    assert crossed.json()["on_chain"]["message"] == "Receipt event ticker does not match."
    scorer.score.assert_not_called()
    store.close()


def _status_client(tmp_path: Path, chain: Any):
    from unittest.mock import Mock

    from rwa_score.api.auto_attest import AttesterSettings

    settings = _settings(tmp_path)
    store = Store()
    scorer = Mock()
    scorer.score.side_effect = AssertionError("status must not score")
    attester = AttesterSettings(
        private_key="0x" + "11" * 32,
        contract="0x" + "ab" * 20,
        rpc_url="http://127.0.0.1:8545",
        attest_enabled=True,
    )
    app = create_app(
        settings=settings,
        store=store,
        scorer=scorer,
        attester=attester,
        chain=chain,
        start_worker=False,
    )
    return TestClient(app), store, scorer


def test_attest_status_requires_verify_for_caller_hash(tmp_path: Path) -> None:
    """A caller-supplied hash must not confirm a ticker when verify fails."""
    from unittest.mock import Mock

    digest = "0x" + "ab" * 32
    chain = Mock()
    chain.hashes_for_ticker.return_value = []
    chain.attested.return_value = True
    chain.get_attestation.side_effect = RuntimeError("getAttestation down")
    chain.verify.side_effect = RuntimeError("verify down")
    client, store, scorer = _status_client(tmp_path, chain)
    raw = store.create_key(name="paid", tier="paid")
    response = client.get(
        "/v1/attest/ZZZZ/status",
        headers=_headers(raw),
        params={"score_hash": digest},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["on_chain"]["status"] == "unavailable"
    assert body["on_chain"]["attested"] is False
    assert body["on_chain"]["ticker"] == "ZZZZ"
    assert body["score_hash"] == digest
    assert "verify down" not in response.text
    assert "getAttestation down" not in response.text
    chain.verify.assert_called_once_with(digest, "ZZZZ")
    scorer.score.assert_not_called()
    store.close()


def test_attest_status_confirms_verified_ticker(tmp_path: Path) -> None:
    """attested plus verify for this ticker still shows confirmed."""
    from unittest.mock import Mock

    digest = "0x" + "cd" * 32
    chain = Mock()
    chain.hashes_for_ticker.return_value = []
    chain.attested.return_value = True
    chain.get_attestation.return_value = {
        "ticker": "NVDA",
        "attested_at": 1_700_000_000,
        "claimed_at": 1_700_000_000,
        "attester": "0x" + "11" * 20,
        "score_hash": digest,
    }
    chain.verify.return_value = (True, 1_700_000_000, "0x" + "11" * 20)
    client, store, scorer = _status_client(tmp_path, chain)
    raw = store.create_key(name="paid", tier="paid")
    body = client.get(
        "/v1/attest/NVDA/status",
        headers=_headers(raw),
        params={"score_hash": digest},
    ).json()
    assert body["status"] == "confirmed"
    assert body["on_chain"]["status"] == "confirmed"
    assert body["on_chain"]["attested"] is True
    assert body["on_chain"]["ticker"] == "NVDA"
    assert body["on_chain"]["attestedAt"] == 1_700_000_000
    assert body["score_hash"] == digest
    chain.verify.assert_called_once_with(digest, "NVDA")
    scorer.score.assert_not_called()
    store.close()


def test_attest_status_rejects_empty_record_ticker(tmp_path: Path) -> None:
    from unittest.mock import Mock

    digest = "0x" + "ef" * 32
    chain = Mock()
    chain.attested.return_value = True
    chain.get_attestation.return_value = {
        "ticker": "",
        "attested_at": 1_700_000_000,
        "claimed_at": 0,
        "attester": "0x" + "22" * 20,
        "score_hash": digest,
    }
    chain.verify.return_value = (True, 1_700_000_000, "0x" + "22" * 20)
    client, store, scorer = _status_client(tmp_path, chain)
    raw = store.create_key(name="paid", tier="paid")
    body = client.get(
        "/v1/attest/NVDA/status",
        headers=_headers(raw),
        params={"score_hash": digest},
    ).json()
    assert body["status"] == "absent"
    assert body["on_chain"]["status"] == "absent"
    assert body["on_chain"]["attested"] is False
    assert body["on_chain"]["ticker"] == "NVDA"
    scorer.score.assert_not_called()
    store.close()


def test_attest_status_receipt_without_hash_is_not_confirmed(tmp_path: Path) -> None:
    from unittest.mock import Mock

    chain = Mock()
    chain.receipt_event.return_value = {
        "ticker": "NVDA",
        "attested_at": 1_700_000_111,
        "claimed_at": 0,
        "attester": "0x" + "33" * 20,
    }
    chain.hashes_for_ticker.return_value = []
    chain.attested.side_effect = AssertionError("no digest to attest")
    chain.verify.side_effect = AssertionError("no digest to verify")
    chain.get_attestation.side_effect = AssertionError("no digest to read")
    client, store, _scorer = _status_client(tmp_path, chain)
    raw = store.create_key(name="paid", tier="paid")
    body = client.get(
        "/v1/attest/NVDA/status",
        headers=_headers(raw),
        params={"tx_hash": "0x" + "12" * 32},
    ).json()
    assert body["status"] == "absent"
    assert body["on_chain"]["status"] == "absent"
    assert body["on_chain"]["attested"] is False
    chain.attested.assert_not_called()
    chain.verify.assert_not_called()
    store.close()


def _get_status(tmp_path: Path, chain: Any, ticker: str, **params: str) -> dict[str, Any]:
    client, store, scorer = _status_client(tmp_path, chain)
    raw = store.create_key(name="paid", tier="paid")
    response = client.get(
        f"/v1/attest/{ticker}/status",
        headers=_headers(raw),
        params=params,
    )
    assert response.status_code == 200
    body = response.json()
    scorer.score.assert_not_called()
    store.close()
    return body


def _matching_record(ticker: str, digest: str) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "attested_at": 1_700_000_000,
        "claimed_at": 1_700_000_000,
        "attester": "0x" + "11" * 20,
        "score_hash": digest,
    }


def test_attest_status_attested_with_verify_false_is_absent(tmp_path: Path) -> None:
    """S04. attested alone must not confirm when verify returns False."""
    from unittest.mock import Mock

    digest = "0x" + "a4" * 32
    chain = Mock()
    chain.attested.return_value = True
    chain.get_attestation.return_value = _matching_record("NVDA", digest)
    chain.verify.return_value = (False, 0, "0x" + "00" * 20)
    body = _get_status(tmp_path, chain, "NVDA", score_hash=digest)
    assert body["status"] == "absent"
    assert body["on_chain"]["status"] == "absent"
    assert body["on_chain"]["attested"] is False
    chain.verify.assert_called_once_with(digest, "NVDA")


def test_attest_status_attested_with_verify_none_is_absent(tmp_path: Path) -> None:
    """S05. A verify result of None is not a successful verify."""
    from unittest.mock import Mock

    digest = "0x" + "a5" * 32
    chain = Mock()
    chain.attested.return_value = True
    chain.get_attestation.return_value = _matching_record("NVDA", digest)
    chain.verify.return_value = None
    body = _get_status(tmp_path, chain, "NVDA", score_hash=digest)
    assert body["status"] == "absent"
    assert body["on_chain"]["status"] == "absent"
    assert body["on_chain"]["attested"] is False


def test_attest_status_truthy_verify_is_not_confirmed(tmp_path: Path) -> None:
    """S06. Only verify returning exactly True can confirm."""
    from unittest.mock import Mock

    digest = "0x" + "a6" * 32
    chain = Mock()
    chain.attested.return_value = True
    chain.get_attestation.return_value = _matching_record("NVDA", digest)
    chain.verify.return_value = (1, 1_700_000_000, "0x" + "11" * 20)
    body = _get_status(tmp_path, chain, "NVDA", score_hash=digest)
    assert body["status"] == "absent"
    assert body["on_chain"]["status"] == "absent"
    assert body["on_chain"]["attested"] is False


def test_attest_status_stale_attested_flag_is_absent(tmp_path: Path) -> None:
    """S09. verify True with attested False is a stale flag, not a confirm."""
    from unittest.mock import Mock

    digest = "0x" + "a9" * 32
    chain = Mock()
    chain.attested.return_value = False
    chain.get_attestation.return_value = _matching_record("NVDA", digest)
    chain.verify.return_value = (True, 1_700_000_000, "0x" + "11" * 20)
    body = _get_status(tmp_path, chain, "NVDA", score_hash=digest)
    assert body["status"] == "absent"
    assert body["on_chain"]["status"] == "absent"
    assert body["on_chain"]["attested"] is False


def test_attest_status_tx_event_without_verify_ok_is_absent(tmp_path: Path) -> None:
    """S17. A ScoreAttested log does not confirm when verify is not exactly True."""
    from unittest.mock import Mock

    digest = "0x" + "b7" * 32
    tx_hash = "0x" + "c7" * 32
    chain = Mock()
    chain.receipt_event.return_value = {
        "ticker": "NVDA",
        "score_hash": digest,
        "attested_at": 1_700_000_111,
        "claimed_at": 1_700_000_000,
        "attester": "0x" + "33" * 20,
    }
    chain.attested.return_value = True
    chain.get_attestation.return_value = _matching_record("NVDA", digest)
    chain.verify.return_value = (False, 0, "0x" + "00" * 20)
    body = _get_status(tmp_path, chain, "NVDA", tx_hash=tx_hash, score_hash=digest)
    assert body["status"] == "absent"
    assert body["on_chain"]["status"] == "absent"
    assert body["on_chain"]["attested"] is False

    stale = Mock()
    stale.receipt_event.return_value = {
        "ticker": "NVDA",
        "score_hash": digest,
        "attested_at": 1_700_000_111,
        "claimed_at": 1_700_000_000,
        "attester": "0x" + "33" * 20,
    }
    stale.attested.return_value = False
    stale.get_attestation.return_value = _matching_record("NVDA", digest)
    stale.verify.return_value = (True, 1_700_000_000, "0x" + "11" * 20)
    stale_body = _get_status(tmp_path, stale, "NVDA", tx_hash=tx_hash, score_hash=digest)
    assert stale_body["status"] == "absent"
    assert stale_body["on_chain"]["status"] == "absent"
    assert stale_body["on_chain"]["attested"] is False


def test_attest_status_chain_without_verify_is_absent(tmp_path: Path) -> None:
    """S20. A chain object with no verify method cannot confirm."""
    digest = "0x" + "c0" * 32

    class _AttestedOnly:
        def attested(self, _digest: str) -> bool:
            return True

        def get_attestation(self, _digest: str) -> dict[str, Any]:
            return _matching_record("NVDA", digest)

        def hashes_for_ticker(self, _symbol: str) -> list[str]:
            return []

    body = _get_status(tmp_path, _AttestedOnly(), "NVDA", score_hash=digest)
    assert body["status"] == "absent"
    assert body["on_chain"]["status"] == "absent"
    assert body["on_chain"]["attested"] is False
    assert not hasattr(_AttestedOnly(), "verify")


def test_attest_status_ticker_path_unverified_is_not_confirmed(tmp_path: Path) -> None:
    """Ticker lookup keeps the stored case and still requires a successful verify.

    No score_hash is supplied, so the digest comes from hashesForTicker.
    Querying BNVDA must read bNVDA, and attested() alone must not confirm.
    """
    from unittest.mock import Mock

    digest = "0x" + "b1" * 32
    stored = "bNVDA"

    def hashes_for_ticker(ticker: str) -> list[str]:
        return [digest] if ticker == stored else []

    chain = Mock()
    chain.hashes_for_ticker.side_effect = hashes_for_ticker
    chain.attested.return_value = True
    chain.get_attestation.return_value = _matching_record(stored, digest)
    chain.verify.return_value = (False, 0, "0x" + "00" * 20)
    body = _get_status(tmp_path, chain, "BNVDA")
    assert body["ticker"] == stored
    assert body["score_hash"] == digest
    assert body["status"] == "absent"
    assert body["status"] != "confirmed"
    assert body["on_chain"]["ticker"] == stored
    assert body["on_chain"]["status"] == "absent"
    assert body["on_chain"]["attested"] is False
    chain.hashes_for_ticker.assert_called_once_with(stored)
    chain.verify.assert_called_once_with(digest, stored)

    raised = Mock()
    raised.hashes_for_ticker.side_effect = hashes_for_ticker
    raised.attested.return_value = True
    raised.get_attestation.return_value = _matching_record(stored, digest)
    raised.verify.side_effect = RuntimeError("verify down")
    failed = _get_status(tmp_path, raised, "bNVDA")
    assert failed["ticker"] == stored
    assert failed["status"] == "unavailable"
    assert failed["status"] != "confirmed"
    assert failed["on_chain"]["attested"] is False
    assert failed["on_chain"]["ticker"] == stored
    assert "verify down" not in str(failed)
    raised.hashes_for_ticker.assert_called_once_with(stored)
    raised.verify.assert_called_once_with(digest, stored)


def _post_attest(tmp_path: Path, chain: Any, scorer: TransparencyScorer):
    from rwa_score.api.auto_attest import PINNED_ATTESTATION_CONTRACT, AttesterSettings

    settings = _settings(tmp_path)
    store = Store()
    attester = AttesterSettings(
        private_key="0x" + "11" * 32,
        contract=PINNED_ATTESTATION_CONTRACT,
        rpc_url="http://127.0.0.1:8545",
        attest_enabled=True,
        min_balance_wei=0,
        value_cap_wei=0,
        min_interval_seconds=0,
    )
    app = create_app(
        settings=settings,
        store=store,
        scorer=scorer,
        attester=attester,
        chain=chain,
        start_worker=False,
    )
    client = TestClient(app)
    raw = store.create_key(name="paid", tier="paid")
    response = client.post("/v1/attest/NVDA", headers=_headers(raw))
    return response, store


def test_post_already_attested_verify_false_is_not_confirmed(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    """U04. AlreadyAttested plus verify False must not say confirmed."""
    from unittest.mock import Mock

    from rwa_score.api.auto_attest import AlreadyAttestedError

    chain = Mock()
    chain.chain_id.return_value = 84532
    chain.fee_wei.return_value = 0
    chain.verify.return_value = (False, 0, "0x" + "00" * 20)
    chain.attest.side_effect = AlreadyAttestedError("AlreadyAttested")
    response, store = _post_attest(tmp_path, chain, fixture_scorer)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["status"] != "confirmed"
    assert body.get("attested_at") is None
    store.close()


def test_post_receipt_status_one_verify_false_is_pending(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    """U07. A mined receipt with verify not True stays pending."""
    from unittest.mock import Mock

    tx_hash = "0x" + "e7" * 32
    chain = Mock()
    chain.chain_id.return_value = 84532
    chain.fee_wei.return_value = 0
    chain.verify.return_value = (False, 0, "0x" + "00" * 20)
    chain.get_receipt.return_value = {"status": 1}

    def _attest(*_args: object, **kwargs: object) -> str:
        on_submitted = kwargs.get("on_submitted")
        if callable(on_submitted):
            on_submitted(tx_hash, 1)
        return tx_hash

    chain.attest.side_effect = _attest
    response, store = _post_attest(tmp_path, chain, fixture_scorer)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["status"] != "confirmed"
    assert body["tx_hash"] == tx_hash
    store.close()


def test_me_quota(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    client, store = _client(tmp_path, fixture_scorer, free_daily_limit=50)
    raw = store.create_key(name="free", tier="free")
    client.get("/v1/score/NVDA", headers=_headers(raw))
    me = client.get("/v1/me", headers=_headers(raw)).json()
    assert me["tier"] == "free"
    assert me["used"] >= 1
    assert me["remaining"] == 50 - me["used"]


def test_unknown_ticker_404(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    client, store = _client(tmp_path, fixture_scorer)
    raw = store.create_key(name="x", tier="free")
    resp = client.get("/v1/score/NOTATICKER", headers=_headers(raw))
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_keys_cli_create_is_not_kept(capsys: pytest.CaptureFixture[str]) -> None:
    from rwa_score.api.keys import main as keys_main

    assert keys_main(["create", "--name", "acme", "--tier", "paid"]) == 0
    captured = capsys.readouterr()
    raw = captured.out.strip().splitlines()[-1]
    assert raw.startswith("rat_")
    assert "RWA_API_BOOTSTRAP_KEY" in captured.err
    assert keys_main(["list"]) == 0
    listed = capsys.readouterr().out
    assert "acme" not in listed
    assert "no keys" in listed


def test_verify_client_fixtures_json(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    fixture_scorer: TransparencyScorer,
) -> None:
    from rwa_score.api.verify import main as verify_main

    from rwa_score.api.verify import export_payload

    report = fixture_scorer.score("NVDA")
    raw = canonical_bytes(attestation_payload(report))
    inputs = inputs_bytes(report)
    path = tmp_path / "verify.payload.json"
    path.write_text(
        json.dumps(
            export_payload(
                ticker="NVDA",
                score_hash=hash_canonical(raw),
                canonical=raw,
                inputs=inputs,
            )
        ),
        encoding="utf-8",
    )
    assert verify_main(["NVDA", "--fixtures", "--offline", "--json", "--payload-file", str(path)]) == 0
    captured = capsys.readouterr()
    assert "obsolete" in captured.err.lower()
    payload = json.loads(captured.out)
    assert payload["ticker"] == "NVDA"
    assert payload["score_hash"].startswith("0x")
    assert payload["payload"]["subscores"]
    assert payload["on_chain"] is None
    assert payload["stored"] is True


def test_post_attest_hash_is_the_canonical_bytes_from_that_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One canonical build. A later clock reading does not fork the returned hash."""
    import base64

    monkeypatch.delenv("RWA_USE_FIXTURES", raising=False)
    ticks = {"n": 0}

    def clock() -> float:
        ticks["n"] += 1
        return 1_700_000_000.0 + ticks["n"] * 5

    monkeypatch.setattr("rwa_score.api.attest.time.time", clock)
    client, store = _client(tmp_path, _live_scorer())
    raw = store.create_key(name="paid", tier="paid")
    first = client.post("/v1/attest/NVDA", headers=_headers(raw))
    second = client.post("/v1/attest/NVDA", headers=_headers(raw))
    assert first.status_code == 200
    assert second.status_code == 200
    for body in (first.json(), second.json()):
        raw_bytes = base64.b64decode(body["canonical_b64"])
        assert hash_canonical(raw_bytes) == body["score_hash"]
        assert json.loads(raw_bytes.decode("utf-8")) == body["payload"]
        assert body["as_of"] == body["payload"]["as_of"]
        assert store.queue_depth() == 0
    assert first.json()["score_hash"] != second.json()["score_hash"]
    assert first.json()["as_of"] != second.json()["as_of"]


def _live_scorer():
    from rwa_score.scorer import TransparencyScorer
    from tests.conftest import RecordingClient

    inner = TransparencyScorer(RecordingClient(), use_live_verifiers=False)

    class _Flip:
        def __init__(self) -> None:
            self.n = 0

        def score(self, ticker: str) -> dict[str, Any]:
            report = dict(inner.score(ticker))
            assert report["data_source"] == "live"
            price = dict(report.get("price") or {})
            # This stub quote has no tape. The hash check needs a real volume
            # so the request is not the unavailable-input path.
            if price.get("volume_24h") is None:
                price["volume_24h"] = 1.0
            report["price"] = price
            self.n += 1
            if self.n % 2 == 1:
                report["score"] = 90.0
                report["band"] = "GREEN"
            else:
                report["score"] = 12.0
                report["band"] = "RED"
            return report

    return _Flip()
