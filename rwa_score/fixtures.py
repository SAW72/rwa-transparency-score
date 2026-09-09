"""Offline CoinMarketCap client backed by canned RWA API envelopes.

The bundle in ``rwa_score/data/bundle.json`` mirrors the public CMC response
shapes so the Streamlit demo and pytest suite work without ``CMC_API_KEY``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .client import CMCError

BUNDLE_PATH = Path(__file__).resolve().parent / "data" / "bundle.json"


@lru_cache(maxsize=1)
def load_bundle(path: Path | None = None) -> dict[str, Any]:
    target = path or BUNDLE_PATH
    with target.open(encoding="utf-8") as fh:
        return json.load(fh)


class FixtureClient:
    """Duck-types :class:`rwa_score.client.CMCClient` with zero network I/O."""

    source = "fixtures"

    def __init__(self, bundle: dict[str, Any] | None = None) -> None:
        self._bundle = bundle if bundle is not None else load_bundle()

    def rwa_map(self, symbol: str | None = None) -> list[dict[str, Any]]:
        assets = list(self._bundle["rwa_map"]["data"]["rwa_assets"])
        if not symbol:
            return assets
        wanted = {s.strip().upper() for s in symbol.split(",") if s.strip()}
        return [a for a in assets if (a.get("symbol") or "").upper() in wanted]

    def rwa_info(self, rwa_id: int) -> dict[str, Any]:
        envelope = self._bundle["rwa_info"].get(str(rwa_id))
        if not envelope:
            return {}
        assets = envelope.get("data", {}).get("rwa_assets", [])
        return assets[0] if assets else {}

    def issuers_list(self) -> list[dict[str, Any]]:
        return list(self._bundle["issuers_list"]["data"]["issuers"])

    def issuer(self, issuer_id: str) -> dict[str, Any]:
        envelope = self._bundle["issuers"].get(issuer_id)
        if not envelope:
            raise CMCError(f"fixture issuer not found: {issuer_id}")
        return envelope.get("data", {})

    def crypto_quote(self, crypto_id: int) -> dict[str, Any]:
        envelope = self._bundle["quotes"].get(str(crypto_id))
        if not envelope:
            return {}
        return envelope.get("data", {}).get(str(crypto_id), {})
