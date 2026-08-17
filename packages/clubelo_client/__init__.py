"""Thin, resilient HTTP adapter over the public ClubElo.com ratings API."""

from packages.clubelo_client.client import ClubEloClient, ClubEloFetchResult

__all__ = ["ClubEloClient", "ClubEloFetchResult"]
