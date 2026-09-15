"""README / CI surface for the paid API and Base Sepolia-only attestation."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from rwa_score.api.app import BREAKDOWN_KEYS, create_app
from rwa_score.api.settings import ApiSettings
from rwa_score.api.store import Store
from rwa_score.scorer import TransparencyScorer

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_JSON = ROOT / "docs/examples/v1_score_NVDA.fixture.json"
FRICTION_DOC = ROOT / "docs/JUDGE_FRICTION.md"
EVIDENCE_DOC = ROOT / "docs/API_EVIDENCE.md"
DISCLAIMER_PREFIX = (
    "Informational and educational hackathon demo only. Not financial, investment, "
)


def test_readme_documents_pricing_and_api_keys() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "50 calls" in text
    assert "$20" in text and "$50" in text
    assert "X-API-Key" in text
    assert "python -m rwa_score.api.keys create" in text
    assert "python -m rwa_score.api" in text
    assert "Base Sepolia" in text
    assert "mainnet" in text.lower()


def test_ci_runs_forge_test() -> None:
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "forge test" in text
    assert "foundry" in text.lower()


def test_attest_uses_msg_sender_not_calldata() -> None:
    src = (ROOT / "contracts/src/ScoreAttestation.sol").read_text()
    block = src.split("function attest(", 1)[1].split(")", 1)[0]
    assert "bytes32 scoreHash" in block
    assert "string" in block
    assert "uint256 timestamp" in block
    assert "address attester" not in block
    assert "address attester = msg.sender" in src
    assert "NotAttester" in src
    assert "isAttester" in src
    assert "function authorized" in src


def test_webhook_poster_disables_redirects() -> None:
    src = (ROOT / "rwa_score/api/webhooks.py").read_text(encoding="utf-8")
    assert "allow_redirects=False" in src


def test_readme_documents_tenant_scoped_webhooks() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "per tenant" in text
    assert "never fires key B" in text
    assert "does not follow HTTP redirects" in text
    assert "authorized attester" in text


def test_deploy_script_holds_mainnet() -> None:
    script = (ROOT / "contracts/script/DeploySepolia.s.sol").read_text(encoding="utf-8")
    assert "84532" in script
    assert "mainnet held" in script
    assert "8453" not in script or "BASE_SEPOLIA" in script
    # No mainnet deploy script in this PR.
    names = {p.name for p in (ROOT / "contracts/script").glob("*.sol")}
    assert "DeployMainnet.s.sol" not in names


def test_readme_links_judge_docs_and_keeps_disclaimer() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/JUDGE_FRICTION.md" in text
    assert "docs/API_EVIDENCE.md" in text
    assert "docs/examples/v1_score_NVDA.fixture.json" in text
    assert "X-API-Key: <REDACTED>" in text
    assert DISCLAIMER_PREFIX in text
    assert "## Disclaimer" in text


def test_judge_friction_covers_rate_limits_and_fixtures() -> None:
    text = FRICTION_DOC.read_text(encoding="utf-8")
    assert FRICTION_DOC.is_file()
    assert "429" in text
    assert "1008" in text
    assert "RWA_USE_FIXTURES=1" in text
    assert "rwa-transparency-score.onrender.com" in text
    assert "/health" in text
    assert "DoraHacks Startup" in text
    assert "cold start" in text.lower()
    assert "X-API-Key: <REDACTED>" in text


def test_api_evidence_is_redacted_and_fixture_labeled() -> None:
    text = EVIDENCE_DOC.read_text(encoding="utf-8")
    assert "GET /v1/score/NVDA" in text
    assert "X-API-Key: <REDACTED>" in text
    assert "fixture-backed" in text.lower()
    assert "not live" in text.lower()
    assert '"data_source": "fixture"' in text
    assert "X-API-Key: rat_" not in text
    assert re.search(r"CMC_API_KEY\s*=\s*\S+", text) is None


def test_docs_do_not_embed_secrets() -> None:
    blobs = [
        (ROOT / "README.md").read_text(encoding="utf-8"),
        FRICTION_DOC.read_text(encoding="utf-8"),
        EVIDENCE_DOC.read_text(encoding="utf-8"),
        EVIDENCE_JSON.read_text(encoding="utf-8"),
    ]
    for blob in blobs:
        assert re.search(r"X-API-Key:\s*rat_[A-Za-z0-9_-]+", blob) is None
        assert "RWA_API_BOOTSTRAP_KEY=" not in blob
        assert re.search(r"sk-[A-Za-z0-9]{10,}", blob) is None


def test_nvda_evidence_sample_matches_fixture_api(
    tmp_path: Path,
    fixture_scorer: TransparencyScorer,
) -> None:
    sample = json.loads(EVIDENCE_JSON.read_text(encoding="utf-8"))
    assert sample["ticker"] == "NVDA"
    assert sample["data_source"] == "fixture"
    for key in BREAKDOWN_KEYS:
        assert key in sample

    settings = ApiSettings(db_path=tmp_path / "api.sqlite")
    store = Store(settings.db_path)
    app = create_app(settings=settings, store=store, scorer=fixture_scorer)
    raw = store.create_key(name="docs-evidence", tier="paid")
    body = TestClient(app).get("/v1/score/NVDA", headers={"X-API-Key": raw}).json()
    assert body == sample

    # Markdown paste stays in lockstep with the captured file.
    md = EVIDENCE_DOC.read_text(encoding="utf-8")
    fence = md.split("```json", 1)[1].split("```", 1)[0]
    assert json.loads(fence) == sample


def test_render_blueprint_does_not_host_paid_api() -> None:
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "python -m rwa_score.health" in text
    assert "python -m rwa_score.api" not in text
    assert "plan: free" in text
    assert "RWA_USE_FIXTURES" in text
    assert 'value: "0"' in text
