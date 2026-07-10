"""Thin, resilient HTTP adapter over the public FPL API (bootstrap-static, fixtures, event-status)."""

from packages.fpl_client.client import FPLClient
from packages.fpl_client.models import FetchResult

__all__ = ["FPLClient", "FetchResult"]
