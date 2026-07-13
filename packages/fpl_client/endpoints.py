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
