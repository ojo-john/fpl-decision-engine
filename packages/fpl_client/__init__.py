"""Thin, resilient HTTP adapter over the public FPL API.

Covers bootstrap-static/fixtures/event-status plus the manager-facing
endpoints (entry, history, picks, transfers) added in Milestone 4 Layer 1.
"""

from packages.fpl_client.client import FPLClient
from packages.fpl_client.models import FetchResult

__all__ = ["FPLClient", "FetchResult"]
