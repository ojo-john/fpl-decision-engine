"""URL constants for the FPL public API.

``https://fantasy.premierleague.com/help/rules`` is a client-rendered JS SPA
with no server-side rules content, so it is deliberately not among these
constants — see ``app/rules`` for the rationale and the structured-API-first
strategy that replaces HTML scraping.
"""

BASE_URL = "https://fantasy.premierleague.com/api"
BOOTSTRAP_STATIC_URL = f"{BASE_URL}/bootstrap-static/"
FIXTURES_URL = f"{BASE_URL}/fixtures/"
EVENT_STATUS_URL = f"{BASE_URL}/event-status/"

# Manager-facing endpoints (Milestone 4 Layer 1) — fully public, no
# authentication needed, verified live against real manager ids during
# planning. ``{manager_id}``/``{event}`` are filled in by the caller.
MANAGER_ENTRY_URL_TEMPLATE = BASE_URL + "/entry/{manager_id}/"
MANAGER_HISTORY_URL_TEMPLATE = BASE_URL + "/entry/{manager_id}/history/"
MANAGER_PICKS_URL_TEMPLATE = BASE_URL + "/entry/{manager_id}/event/{event}/picks/"
MANAGER_TRANSFERS_URL_TEMPLATE = BASE_URL + "/entry/{manager_id}/transfers/"

# H2H league endpoints (Milestone 5 Layer 1) — fully public, no authentication
# needed, verified live against the user's real league (759345, "Pro Clubs
# 26") during planning. ``{league_id}`` is filled in by the caller; both are
# paginated (``page_standings=N`` / ``page=N``) with a ``has_next`` flag in
# the response body, same shape as every other paginated FPL list endpoint.
LEAGUE_H2H_STANDINGS_URL_TEMPLATE = BASE_URL + "/leagues-h2h/{league_id}/standings/"
LEAGUE_H2H_MATCHES_URL_TEMPLATE = BASE_URL + "/leagues-h2h-matches/league/{league_id}/"

# Per-gameweek "live" stats (Milestone 7 Layer 0) — fully public, no
# authentication needed, verified live against the real 2025-26 season's
# already-finished gameweeks (event 38 was the last) during planning.
# Returns real per-player actual stats for one gameweek:
# ``{"elements": [{"id": <element_id>, "stats": {...}, "explain": [...]}]}``.
# ``{event_id}`` is filled in by the caller.
EVENT_LIVE_URL_TEMPLATE = BASE_URL + "/event/{event_id}/live/"
