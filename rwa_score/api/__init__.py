"""Paid REST output layer. Scoring logic stays in ``rwa_score.scorer`` (MIT)."""

from .app import create_app

__all__ = ["create_app"]
