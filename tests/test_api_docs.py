"""README / CI surface for the paid API and Base Sepolia-only attestation."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


def test_deploy_script_holds_mainnet() -> None:
    script = (ROOT / "contracts/script/DeploySepolia.s.sol").read_text(encoding="utf-8")
    assert "84532" in script
    assert "mainnet held" in script
    assert "8453" not in script or "BASE_SEPOLIA" in script
    # No mainnet deploy script in this PR.
    names = {p.name for p in (ROOT / "contracts/script").glob("*.sol")}
    assert "DeployMainnet.s.sol" not in names
