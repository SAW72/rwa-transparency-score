"""Paid API: auth, quotas, parity with TransparencyScorer, webhooks."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rwa_score.api.app import BREAKDOWN_KEYS, create_app
from rwa_score.api.settings import ApiSettings
from rwa_score.api.store import Store
from rwa_score.api.webhooks import notify_crossings
from rwa_score.scorer import ScoreError, TransparencyScorer


def _settings(tmp_path: Path, **overrides: Any) -> ApiSettings:
    base = ApiSettings(db_path=tmp_path / "api.sqlite", free_daily_limit=50)
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
    store = Store(settings.db_path)
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


def test_missing_and_invalid_key(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    client, _ = _client(tmp_path, fixture_scorer)
    assert client.get("/v1/score/NVDA").status_code == 401
    assert client.get("/v1/score/NVDA", headers=_headers("rat_nope")).status_code == 401


@pytest.mark.parametrize("ticker", ["NVDA", "TSLA", "AAPL"])
def test_api_breakdown_matches_scorer_byte_for_byte(
    tmp_path: Path,
    fixture_scorer: TransparencyScorer,
    ticker: str,
) -> None:
    client, store = _client(tmp_path, fixture_scorer)
    raw = store.create_key(name="parity", tier="paid")
    direct = fixture_scorer.score(ticker)
    via_api = client.get(f"/v1/score/{ticker}", headers=_headers(raw))
    assert via_api.status_code == 200
    body = via_api.json()
    for key in BREAKDOWN_KEYS:
        assert key in body
        assert body[key] == direct[key], key
    assert body["confidence"]["label"] in {"high", "medium", "low"}
    assert body["attestation"]["score_hash"].startswith("0x")
    assert len(body["attestation"]["score_hash"]) == 66


def test_compare_side_by_side(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    client, store = _client(tmp_path, fixture_scorer)
    raw = store.create_key(name="cmp", tier="free")
    resp = client.get("/v1/compare", params={"tickers": "nvda,tsla"}, headers=_headers(raw))
    assert resp.status_code == 200
    body = resp.json()
    assert body["tickers"] == ["NVDA", "TSLA"]
    assert body["scores"][0]["subscores"] == fixture_scorer.score("NVDA")["subscores"]
    assert body["scores"][1]["band"] == fixture_scorer.score("TSLA")["band"]


def test_free_tier_enforces_sliding_window(tmp_path: Path) -> None:
    scorer = SequenceScorer([_minimal_report("NVDA", 80.0, "GREEN") for _ in range(8)])
    client, store = _client(tmp_path, scorer, free_daily_limit=2, rate_window_seconds=86_400.0)
    raw = store.create_key(name="free", tier="free")
    assert client.get("/v1/score/NVDA", headers=_headers(raw)).status_code == 200
    assert client.get("/v1/score/NVDA", headers=_headers(raw)).status_code == 200
    limited = client.get("/v1/score/NVDA", headers=_headers(raw))
    assert limited.status_code == 429
    detail = limited.json()
    assert detail["error"] == "rate_limit"
    assert detail["limit"] == 2
    assert detail["tier"] == "free"


def test_paid_tier_bypasses_free_cap(tmp_path: Path) -> None:
    scorer = SequenceScorer([_minimal_report("NVDA", 80.0, "GREEN") for _ in range(6)])
    client, store = _client(tmp_path, scorer, free_daily_limit=2)
    raw = store.create_key(name="paid", tier="paid")
    for _ in range(5):
        resp = client.get("/v1/score/NVDA", headers=_headers(raw))
        assert resp.status_code == 200, resp.text


def test_bearer_auth_works(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    client, store = _client(tmp_path, fixture_scorer)
    raw = store.create_key(name="bearer", tier="free")
    resp = client.get("/v1/score/NVDA", headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 200
    assert resp.json()["ticker"] == "NVDA"


def test_watchlist_bulk_score(tmp_path: Path, fixture_scorer: TransparencyScorer) -> None:
    client, store = _client(tmp_path, fixture_scorer)
    raw = store.create_key(name="wl", tier="free")
    put = client.put("/v1/watchlist", headers=_headers(raw), json={"tickers": ["nvda", "aapl"]})
    assert put.status_code == 200
    assert put.json()["tickers"] == ["NVDA", "AAPL"]
    got = client.get("/v1/watchlist", headers=_headers(raw))
    assert got.status_code == 200
    assert [row["ticker"] for row in got.json()["scores"]] == ["AAPL", "NVDA"]
    client.delete("/v1/watchlist/AAPL", headers=_headers(raw))
    assert client.get("/v1/watchlist", headers=_headers(raw)).json()["tickers"] == ["NVDA"]


def test_webhooks_and_history_are_paid_only(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    client, store = _client(tmp_path, fixture_scorer)
    free = store.create_key(name="free", tier="free")
    assert client.post(
        "/v1/webhooks",
        headers=_headers(free),
        json={"url": "https://example.test/hook"},
    ).status_code == 403
    assert client.get("/v1/history/NVDA", headers=_headers(free)).status_code == 403
    assert client.get("/v1/attest/NVDA", headers=_headers(free)).status_code == 403


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/hook",
        "https://localhost/hook",
        "https://127.0.0.1/hook",
        "https://192.168.0.10/hook",
        "https://10.1.2.3/hook",
        "https://169.254.169.254/latest/meta-data",
    ],
)
def test_webhook_url_allowlist_rejects_ssrf(
    tmp_path: Path, fixture_scorer: TransparencyScorer, url: str
) -> None:
    client, store = _client(tmp_path, fixture_scorer)
    raw = store.create_key(name="paid", tier="paid")
    resp = client.post(
        "/v1/webhooks",
        headers=_headers(raw),
        json={"url": url, "trigger": "band_cross"},
    )
    assert resp.status_code in {400, 422}


def test_webhook_url_allowlist_accepts_public_https(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    client, store = _client(tmp_path, fixture_scorer)
    raw = store.create_key(name="paid", tier="paid")
    resp = client.post(
        "/v1/webhooks",
        headers=_headers(raw),
        json={"url": "https://example.test/hook", "trigger": "band_cross"},
    )
    assert resp.status_code == 200, resp.text


def test_webhook_fires_on_band_cross_same_cycle(tmp_path: Path) -> None:
    posted: list[tuple[str, str, dict[str, str]]] = []

    def poster(url: str, body: str, headers: dict[str, str]) -> tuple[int, bool]:
        posted.append((url, body, headers))
        return 204, True

    scorer = SequenceScorer(
        [
            _minimal_report("NVDA", 80.0, "GREEN"),
            _minimal_report("NVDA", 20.0, "RED"),
        ]
    )
    client, store = _client(tmp_path, scorer, poster=poster)
    raw = store.create_key(name="paid", tier="paid")
    created = client.post(
        "/v1/webhooks",
        headers=_headers(raw),
        json={"url": "https://example.test/hook", "secret": "s3cret", "trigger": "band_cross"},
    )
    assert created.status_code == 200
    assert client.get("/v1/score/NVDA", headers=_headers(raw)).status_code == 200
    assert posted == []
    second = client.get("/v1/score/NVDA", headers=_headers(raw))
    assert second.status_code == 200
    assert len(posted) == 1
    url, body, headers = posted[0]
    assert url == "https://example.test/hook"
    payload = json.loads(body)
    assert payload["event"] == "band_cross"
    assert payload["from_band"] == "GREEN"
    assert payload["to_band"] == "RED"
    assert payload["ticker"] == "NVDA"
    assert headers["X-RAT-Signature"].startswith("sha256=")

    history = client.get("/v1/history/NVDA", headers=_headers(raw))
    assert history.status_code == 200
    assert len(history.json()["history"]) == 2


def test_score_under_key_a_never_fires_key_b_webhook(tmp_path: Path) -> None:
    posted: list[str] = []

    def poster(url: str, body: str, headers: dict[str, str]) -> tuple[int, bool]:
        posted.append(url)
        return 200, True

    scorer = SequenceScorer(
        [
            _minimal_report("NVDA", 80.0, "GREEN"),
            _minimal_report("NVDA", 20.0, "RED"),
        ]
    )
    client, store = _client(tmp_path, scorer, poster=poster)
    key_a = store.create_key(name="tenant-a", tier="paid")
    key_b = store.create_key(name="tenant-b", tier="paid")
    rec_a = store.lookup_key(key_a)
    rec_b = store.lookup_key(key_b)
    assert rec_a is not None and rec_b is not None

    created = client.post(
        "/v1/webhooks",
        headers=_headers(key_b),
        json={"url": "https://b.example.test/hook", "secret": "b", "trigger": "band_cross"},
    )
    assert created.status_code == 200
    assert client.get("/v1/score/NVDA", headers=_headers(key_b)).status_code == 200
    assert posted == []
    assert store.get_last_band(rec_b.id, "NVDA") == ("GREEN", 80.0)

    posted.clear()
    assert client.get("/v1/score/NVDA", headers=_headers(key_a)).status_code == 200
    assert posted == []
    assert store.get_last_band(rec_a.id, "NVDA") == ("RED", 20.0)
    assert store.get_last_band(rec_b.id, "NVDA") == ("GREEN", 80.0)


def test_last_bands_and_webhooks_isolated_per_tenant(tmp_path: Path) -> None:
    posted: list[tuple[str, str]] = []

    def poster(url: str, body: str, headers: dict[str, str]) -> tuple[int, bool]:
        posted.append((url, json.loads(body)["event"]))
        return 204, True

    scorer = SequenceScorer(
        [
            _minimal_report("NVDA", 80.0, "GREEN"),
            _minimal_report("NVDA", 20.0, "RED"),
            _minimal_report("NVDA", 80.0, "GREEN"),
            _minimal_report("NVDA", 20.0, "RED"),
        ]
    )
    client, store = _client(tmp_path, scorer, poster=poster)
    key_a = store.create_key(name="tenant-a", tier="paid")
    key_b = store.create_key(name="tenant-b", tier="paid")
    rec_a = store.lookup_key(key_a)
    rec_b = store.lookup_key(key_b)
    assert rec_a is not None and rec_b is not None

    assert client.post(
        "/v1/webhooks",
        headers=_headers(key_a),
        json={"url": "https://a.example.test/hook", "secret": "a", "trigger": "band_cross"},
    ).status_code == 200
    assert client.post(
        "/v1/webhooks",
        headers=_headers(key_b),
        json={"url": "https://b.example.test/hook", "secret": "b", "trigger": "band_cross"},
    ).status_code == 200

    assert client.get("/v1/score/NVDA", headers=_headers(key_a)).status_code == 200
    assert posted == []
    assert client.get("/v1/score/NVDA", headers=_headers(key_a)).status_code == 200
    assert posted == [("https://a.example.test/hook", "band_cross")]

    posted.clear()
    assert client.get("/v1/score/NVDA", headers=_headers(key_b)).status_code == 200
    assert posted == []
    assert store.get_last_band(rec_b.id, "NVDA") == ("GREEN", 80.0)
    assert client.get("/v1/score/NVDA", headers=_headers(key_b)).status_code == 200
    assert posted == [("https://b.example.test/hook", "band_cross")]
    assert store.get_last_band(rec_a.id, "NVDA") == ("RED", 20.0)
    assert store.get_last_band(rec_b.id, "NVDA") == ("RED", 20.0)


def test_notify_crossings_never_reads_other_tenant_hooks(tmp_path: Path) -> None:
    store = Store(tmp_path / "iso.sqlite")
    raw_a = store.create_key(name="a", tier="paid")
    raw_b = store.create_key(name="b", tier="paid")
    key_a = store.lookup_key(raw_a)
    key_b = store.lookup_key(raw_b)
    assert key_a is not None and key_b is not None
    store.add_webhook(key_a.id, url="https://a.example.test/h", secret="a")
    store.add_webhook(key_b.id, url="https://b.example.test/h", secret="b")
    store.set_last_band(key_a.id, "NVDA", "GREEN", 80.0)
    store.set_last_band(key_b.id, "NVDA", "GREEN", 80.0)
    posted: list[str] = []

    def poster(url: str, body: str, headers: dict[str, str]) -> tuple[int, bool]:
        posted.append(url)
        return 200, True

    report = _minimal_report("NVDA", 20.0, "RED")
    report["attestation"] = {"score_hash": "0xabc"}
    deliveries = notify_crossings(store, report, key_id=key_a.id, poster=poster)
    assert [d["ok"] for d in deliveries] == [True]
    assert posted == ["https://a.example.test/h"]
    assert store.get_last_band(key_b.id, "NVDA") == ("GREEN", 80.0)
    store.close()


def test_last_bands_migrates_off_unscoped_schema(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE last_bands (
            ticker TEXT PRIMARY KEY,
            band TEXT NOT NULL,
            score REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        INSERT INTO last_bands VALUES ('NVDA', 'GREEN', 80.0, 1.0);
        """
    )
    conn.commit()
    conn.close()

    store = Store(db)
    raw = store.create_key(name="tenant", tier="paid")
    rec = store.lookup_key(raw)
    assert rec is not None
    assert store.get_last_band(rec.id, "NVDA") is None
    store.set_last_band(rec.id, "NVDA", "YELLOW", 60.0)
    assert store.get_last_band(rec.id, "NVDA") == ("YELLOW", 60.0)
    store.close()


def test_below_orange_trigger_skips_yellow_to_orange(tmp_path: Path) -> None:
    posted: list[str] = []

    def poster(url: str, body: str, headers: dict[str, str]) -> tuple[int, bool]:
        posted.append(json.loads(body)["event"])
        return 200, True

    scorer = SequenceScorer(
        [
            _minimal_report("NVDA", 80.0, "GREEN"),
            _minimal_report("NVDA", 40.0, "ORANGE"),
            _minimal_report("NVDA", 10.0, "RED"),
        ]
    )
    client, store = _client(tmp_path, scorer, poster=poster)
    raw = store.create_key(name="paid", tier="paid")
    client.post(
        "/v1/webhooks",
        headers=_headers(raw),
        json={"url": "https://example.test/hook", "secret": "x", "trigger": "below_orange"},
    )
    client.get("/v1/score/NVDA", headers=_headers(raw))
    client.get("/v1/score/NVDA", headers=_headers(raw))
    assert posted == []
    client.get("/v1/score/NVDA", headers=_headers(raw))
    assert posted == ["below_orange"]


def test_attest_endpoint_returns_hash_not_for_chain_storage_of_score(
    tmp_path: Path, fixture_scorer: TransparencyScorer
) -> None:
    client, store = _client(tmp_path, fixture_scorer)
    raw = store.create_key(name="paid", tier="paid")
    resp = client.get("/v1/attest/NVDA", headers=_headers(raw))
    assert resp.status_code == 200
    body = resp.json()
    assert body["algo"] == "sha256"
    assert "score" not in body["payload"] or body["payload"]["score"] == fixture_scorer.score("NVDA")["score"]
    assert body["chain"] == "base-sepolia"
    assert body["chain_id"] == 84532
    assert "never the raw score" in body["note"].lower()
    assert body["contract"] is None


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


def test_keys_cli_create_list_revoke(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from rwa_score.api.keys import main as keys_main

    db = str(tmp_path / "keys.sqlite")
    assert keys_main(["--db", db, "create", "--name", "acme", "--tier", "paid"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    raw = out[-1]
    assert raw.startswith("rat_")
    assert keys_main(["--db", db, "list"]) == 0
    listed = capsys.readouterr().out
    assert "paid" in listed
    assert "acme" in listed
    prefix = raw[:12]
    assert keys_main(["--db", db, "revoke", "--prefix", prefix]) == 0
    store = Store(db)
    assert store.lookup_key(raw) is not None
    assert store.lookup_key(raw).revoked
    store.close()


def test_verify_client_fixtures_json(capsys: pytest.CaptureFixture[str]) -> None:
    from rwa_score.api.verify import main as verify_main

    assert verify_main(["NVDA", "--fixtures", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ticker"] == "NVDA"
    assert payload["score_hash"].startswith("0x")
    assert payload["payload"]["subscores"]
    assert payload["on_chain"] is None
